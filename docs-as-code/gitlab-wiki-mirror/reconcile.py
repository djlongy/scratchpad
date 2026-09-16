"""Reconcile the wiki webhook and its pipeline trigger token from inside the job.

A wiki edit reaches a pipeline through a project webhook on wiki page events
whose URL is that project's pipeline trigger endpoint, with a trigger token in
the query string. Nothing in the repository records that wiring: it lives in the
GitLab project, and it is wrong whenever the project's URL changes. Move the
project to another group, rename it, or migrate the estate to a different server
name, and the hook keeps POSTing to an address that no longer resolves to this
project. The sync job goes on passing, because it is the schedule that runs it,
and a wiki edit silently stops reaching CI.

So the pipeline repairs its own wiring on every default-branch run instead of
depending on somebody remembering to re-run the bootstrap script. Reconciling
is idempotent by construction: it computes the URL the hook must have from the
job's own `CI_API_V4_URL`, `CI_PROJECT_ID` and `CI_DEFAULT_BRANCH`, compares,
and writes only on a difference.

    reconcile.py [--api-url U] [--project-id N] [--default-branch B]

The token comes from the environment and never from an argument: an argument is
readable in `ps`, and Python prints the whole argv back when a subprocess times
out. `WIKI_ADMIN_TOKEN` is used when set, `WIKI_TOKEN` otherwise.

What it owns, and how it recognises its own work:

  hook     the project webhook whose `name` is `wiki-sync`. `name` has been a
           webhook field since GitLab 17.1, it survives a URL change, and the
           bootstrap script has always written it, so an already-bootstrapped
           project is adopted rather than duplicated. Matching on the URL, which
           is what the bootstrap script used to do, cannot work here: the URL is
           the thing being repaired.
  trigger  a pipeline trigger token whose description is `wiki-sync` AND whose
           owner is the identity this job authenticates as. Ownership is not
           decoration. GitLab returns a trigger token's value in full only to
           the user who created it and shortens everybody else's to four
           characters, so a token created by a human operator is unusable here:
           the job cannot put a four-character stub into the hook URL. It
           creates its own instead, and leaves the operator's alone, because
           deleting another user's credential is not this job's call.

Failure semantics (section 10.1). The first call is a capability probe. An
authorization failure there is a fact about the token, not an outage: the run
says so, names the variable and the scope, and leaves the wiring untouched, so
a consumer upgrading with a narrow `write_repository` token is told rather than
broken. Once that probe has passed, every later API failure fails the job. There
is no `|| true` and no unconditional success.

Nothing here prints a token. The hook URL carries one, so even the URL is only
ever reported with the token elided.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from httpjson import HttpError, request  # noqa: E402

HOOK_NAME = "wiki-sync"
HOOK_DESCRIPTION = "wiki edits start a sync pipeline"
TRIGGER_DESCRIPTION = "wiki-sync"

# Everything the hook must be, beside its URL. push_events is off deliberately:
# a push already runs the sync through the pipeline it creates, and a hook that
# triggered on both would run it twice for every commit.
HOOK_SETTINGS = {
    "name": HOOK_NAME,
    "description": HOOK_DESCRIPTION,
    "wiki_page_events": True,
    "push_events": False,
    "enable_ssl_verification": True,
}


class ReconcileError(RuntimeError):
    """The API could not be made to answer, which is never a passing result."""


class NotPermitted(ReconcileError):
    """The token cannot manage this project's hooks. Reported, not fatal."""


def elide_token(url: str) -> str:
    """The hook URL with its trigger token replaced by a marker.

    GitLab 18.9 returns the hook URL verbatim, trigger token included, so this
    is the only form of it that may be printed or put in a job log.
    """
    head, separator, _ = url.partition("?token=")
    return head + separator + "<token>" if separator else url


class Project:
    """The three project APIs this needs, over one authenticated identity."""

    def __init__(self, api_url: str, project_id: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.project_id = str(project_id)
        self.headers = {"PRIVATE-TOKEN": token}

    def call(self, method: str, path: str, body: dict | None = None, expect=(200, 201)):
        url = f"{self.api_url}{path}"
        headers = dict(self.headers)
        payload = None
        if body is not None:
            # `urlencode` would render a Python bool as "True", which GitLab
            # reads as a string and not as the boolean it documents.
            form = {
                key: ("true" if value else "false") if isinstance(value, bool) else value
                for key, value in body.items()
            }
            payload = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = request(method, url, headers=headers, body=payload)
        except HttpError as error:
            raise ReconcileError(str(error)) from error
        if response.status in (401, 403):
            raise NotPermitted(f"{method} {path} returned HTTP {response.status}")
        if response.status not in expect:
            raise ReconcileError(
                f"{method} {path} returned HTTP {response.status}: {response.text[:300]}"
            )
        return response.json() if response.body else {}

    def identity(self) -> int:
        """The numeric user id this token authenticates as."""
        user = self.call("GET", "/user")
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise ReconcileError("GET /user did not return a user id")
        return user["id"]

    def hooks(self) -> list[dict]:
        return self.listing("GET", f"/projects/{self.project_id}/hooks")

    def triggers(self) -> list[dict]:
        return self.listing("GET", f"/projects/{self.project_id}/triggers")

    def listing(self, method: str, path: str) -> list[dict]:
        payload = self.call(method, path)
        if not isinstance(payload, list):
            raise ReconcileError(f"{method} {path} did not return a list")
        return payload


def owned_trigger(triggers: list[dict], user_id: int) -> dict | None:
    """The component's own trigger token, or None when it has never made one.

    Lowest id first, so a project that somehow holds two of ours picks the same
    one on every run and the hook URL stops moving.
    """
    mine = [
        trigger
        for trigger in sorted(triggers, key=lambda t: t.get("id", 0))
        if trigger.get("description") == TRIGGER_DESCRIPTION
        and (trigger.get("owner") or {}).get("id") == user_id
        and trigger.get("token")
    ]
    return mine[0] if mine else None


def desired_url(project: Project, branch: str, token: str) -> str:
    return (
        f"{project.api_url}/projects/{project.project_id}"
        f"/ref/{urllib.parse.quote(branch, safe='')}/trigger/pipeline"
        f"?token={urllib.parse.quote(token, safe='')}"
    )


def reconcile(project: Project, branch: str) -> list[str]:
    """Bring the hook and the trigger token to the state described above."""
    report: list[str] = []

    # The capability probe. Everything after this point is a real failure.
    hooks = project.hooks()
    user_id = project.identity()
    triggers = project.triggers()

    trigger = owned_trigger(triggers, user_id)
    if trigger is None:
        trigger = project.call(
            "POST",
            f"/projects/{project.project_id}/triggers",
            {"description": TRIGGER_DESCRIPTION},
        )
        if not trigger.get("token"):
            raise ReconcileError("the new trigger token came back without a value")
        report.append(f"trigger  created (id {trigger['id']})")
    else:
        report.append(f"trigger  already present (id {trigger['id']})")

    foreign = [
        other
        for other in triggers
        if other.get("description") == TRIGGER_DESCRIPTION and other.get("id") != trigger["id"]
    ]
    for other in foreign:
        owner = (other.get("owner") or {}).get("username", "another user")
        report.append(
            f"trigger  id {other['id']} has the same description but belongs to {owner}; "
            "this job cannot read its value and will not delete it. Remove it by hand "
            "once no webhook uses it."
        )

    url = desired_url(project, branch, trigger["token"])
    existing = next((hook for hook in hooks if hook.get("name") == HOOK_NAME), None)

    if existing is None:
        created = project.call(
            "POST", f"/projects/{project.project_id}/hooks", {"url": url, **HOOK_SETTINGS}
        )
        report.append(f"webhook  created (id {created.get('id')}) -> {elide_token(url)}")
        return report

    drift = {key: value for key, value in HOOK_SETTINGS.items() if existing.get(key) != value}
    if existing.get("url") != url:
        drift["url"] = url

    if not drift:
        report.append(f"webhook  already correct (id {existing['id']}) -> {elide_token(url)}")
        return report

    project.call("PUT", f"/projects/{project.project_id}/hooks/{existing['id']}", drift)
    changed = ", ".join(sorted(drift))
    report.append(
        f"webhook  updated (id {existing['id']}, {changed}) -> {elide_token(url)}"
    )
    if "url" in drift:
        report.append(
            f"webhook  the old URL was {elide_token(str(existing.get('url', '')))}"
        )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile the wiki sync webhook.")
    parser.add_argument("--api-url", default=os.environ.get("CI_API_V4_URL", ""))
    parser.add_argument("--project-id", default=os.environ.get("CI_PROJECT_ID", ""))
    parser.add_argument("--default-branch", default=os.environ.get("CI_DEFAULT_BRANCH", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get("WIKI_ADMIN_TOKEN") or os.environ.get("WIKI_TOKEN") or ""

    missing = [
        name
        for name, value in (
            ("CI_API_V4_URL", args.api_url),
            ("CI_PROJECT_ID", args.project_id),
            ("CI_DEFAULT_BRANCH", args.default_branch),
            ("WIKI_ADMIN_TOKEN or WIKI_TOKEN", token),
        )
        if not value
    ]
    if missing:
        print(f"ERROR: webhook reconcile needs {', '.join(missing)}", file=sys.stderr)
        return 1

    project = Project(args.api_url, args.project_id, token)
    try:
        for line in reconcile(project, args.default_branch):
            print(f"  {line}")
    except NotPermitted as error:
        # Not an outage and not a gate: the token is narrower than this step
        # needs. Say which variable and which scope, leave the wiring alone.
        print(
            f"  webhook  not reconciled: {error}. The token in WIKI_ADMIN_TOKEN, or in "
            "WIKI_TOKEN when that is unset, needs the `api` scope and the Maintainer "
            "role to manage this project's webhooks and trigger tokens. Grant it, or "
            "pass `webhook-reconcile: off` and wire the hook by hand with "
            "runtime/wiki/wiki-bootstrap.sh."
        )
        return 0
    except ReconcileError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
