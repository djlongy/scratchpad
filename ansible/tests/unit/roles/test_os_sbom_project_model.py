"""roles/os_sbom — the project model, replayed through a real Templar.

`resolve_project_model.yml` turns inventory identity (tenancy, env, host) into
the three names the SBOM consumer is addressed by, and into the tag lists that
ride along. Every one of those decisions is a Jinja expression living in a
`set_fact`, so the only honest way to test it is to read the expression out of
the ROLE and evaluate it — not to reimplement the join in Python and assert on
the reimplementation.

What the expressions have to get right:

  * A part list is joined AFTER empty segments are dropped, so an estate with
    no tenancy gets `os/<env>/<host>` rather than `os//<env>/<host>` — a
    double separator would silently create a second, differently-named project
    tree at the consumer.
  * A non-empty full-name override beats the parts, because the same task
    reads and rewrites the same variable; get the branch backwards and the
    override is discarded.
  * `tenancy:` with nothing after the colon is dropped. Dependency-Track
    accepts it, so the failure is a fleet of hosts tagged with a meaningless
    bare prefix rather than an error anyone would notice.

`os_sbom_env` is exercised for the undefined case on purpose: the role is
consumed by inventories that do not define `env`, and a bare `{{ env }}`
default fails the whole role with an undefined-variable error before the
empty-segment handling below ever gets a chance to run.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

# Must match ansible.cfg — see conftest.py.
os.environ.setdefault("ANSIBLE_JINJA2_NATIVE", "True")

from _templating import render as _render  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]
ROLE = REPO_ROOT / "ansible" / "roles" / "os_sbom"
RESOLVE = ROLE / "tasks" / "resolve_project_model.yml"

# set_fact task name → the variable it rewrites.
NAME_TASKS = {
    "os_sbom_project_name": "Join project name parts (drop empty segments)",
    "os_sbom_parent_name": "Join parent portfolio name parts",
    "os_sbom_root_name": "Join root portfolio name parts",
}
TAG_TASKS = {
    "os_sbom_tags_resolved": "Build host project tag list (drop empty / bare-prefix tags)",
    "os_sbom_parent_tags_resolved": "Build parent portfolio tag list",
    "os_sbom_root_tags_resolved": "Build root portfolio tag list",
}


def _defaults() -> dict[str, Any]:
    return dict(yaml.safe_load((ROLE / "defaults" / "main.yml").read_text()))


def _set_fact(task_name: str, var: str) -> str:
    """The SHIPPED expression for one resolved variable."""
    for task in yaml.safe_load(RESOLVE.read_text()):
        if task.get("name") == task_name:
            return task["ansible.builtin.set_fact"][var]
    raise AssertionError(f"no task in resolve_project_model.yml named {task_name!r}")


def _resolve(var: str, **overrides: Any) -> Any:
    """Evaluate one resolve_project_model set_fact over defaults + inventory."""
    task_name = {**NAME_TASKS, **TAG_TASKS}[var]
    scope: dict[str, Any] = {**_defaults(), **overrides}
    resolved = _render(_set_fact(task_name, var), scope)
    return resolved.strip() if isinstance(resolved, str) else resolved


# ── Identity → path parts ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("tenancy", "env_name", "host", "project", "parent", "root"),
    [
        ("acme", "prod", "app-01",
         "os/acme/prod/app-01", "os-hosts/acme/prod", "os-hosts/acme"),
        ("globex", "dev", "api-01",
         "os/globex/dev/api-01", "os-hosts/globex/dev", "os-hosts/globex"),
        # No tenancy → the segment vanishes, it does not become an empty one.
        ("", "staging", "bastion-01",
         "os/staging/bastion-01", "os-hosts/staging", "os-hosts"),
        # Neither identity token set — still a well-formed single-segment tree.
        ("", "", "solo-01", "os/solo-01", "os-hosts", "os-hosts"),
    ],
)
def test_name_parts_join_after_empty_segments_are_dropped(
    tenancy: str, env_name: str, host: str, project: str, parent: str, root: str
) -> None:
    ctx = {"tenancy": tenancy, "env": env_name, "inventory_hostname": host}
    assert _resolve("os_sbom_project_name", **ctx) == project
    assert _resolve("os_sbom_parent_name", **ctx) == parent
    assert _resolve("os_sbom_root_name", **ctx) == root


def test_no_resolved_name_ever_contains_a_double_separator() -> None:
    """The failure an empty segment causes at the consumer, stated directly."""
    ctx = {"tenancy": "", "env": "prod", "inventory_hostname": "app-01"}
    for var in NAME_TASKS:
        assert "//" not in _resolve(var, **ctx)


# ── Override precedence ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("var", "override"),
    [
        ("os_sbom_project_name", "custom/org/leaf-host"),
        ("os_sbom_parent_name", "custom/org/parent"),
        ("os_sbom_root_name", "custom/org"),
    ],
)
def test_full_name_override_wins_over_the_part_list(var: str, override: str) -> None:
    resolved = _resolve(
        var,
        tenancy="acme",
        env="prod",
        inventory_hostname="app-01",
        **{var: override},
    )
    assert resolved == override


def test_a_whitespace_only_override_falls_back_to_the_parts() -> None:
    """Trimmed to empty is not an override — otherwise a stray space renames the tree."""
    resolved = _resolve(
        "os_sbom_project_name",
        tenancy="acme",
        env="prod",
        inventory_hostname="app-01",
        os_sbom_project_name="   ",
    )
    assert resolved == "os/acme/prod/app-01"


# ── Tag filtering ────────────────────────────────────────────────────────
def test_tags_carry_identity_when_it_is_set() -> None:
    tags = _resolve(
        "os_sbom_tags_resolved",
        tenancy="acme",
        env="prod",
        ansible_distribution="AlmaLinux",
    )
    assert tags == ["os-host", "tenancy:acme", "env:prod", "almalinux"]


@pytest.mark.parametrize(
    "var", ["os_sbom_tags_resolved", "os_sbom_parent_tags_resolved",
            "os_sbom_root_tags_resolved"],
)
def test_a_bare_tenancy_prefix_is_dropped_when_tenancy_is_unset(var: str) -> None:
    tags = _resolve(var, tenancy="", env="prod", ansible_distribution="Ubuntu")
    assert "tenancy:" not in tags
    assert not any(t.endswith(":") for t in tags)
    assert "os-host" in tags


def test_dropping_a_bare_prefix_does_not_drop_a_populated_one() -> None:
    """Guards the premise: the reject pattern must be anchored to a trailing colon."""
    tags = _resolve(
        "os_sbom_parent_tags_resolved", tenancy="acme", env="prod"
    )
    assert "tenancy:acme" in tags
    assert "env:prod" in tags


# ── Project version ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("os_release", "AlmaLinux-9.5"),
        ("date", "2026-07-30"),
        ("fixed", "pinned"),
        # Anything unrecognised falls through to the OS release, not to empty.
        ("nonsense", "AlmaLinux-9.5"),
    ],
)
def test_version_mode_selects_the_project_version(mode: str, expected: str) -> None:
    version = _render(
        _defaults()["os_sbom_project_version"],
        {
            "os_sbom_version_mode": mode,
            "os_sbom_project_version_fixed": "pinned",
            "ansible_date_time": {"date": "2026-07-30"},
            "ansible_distribution": "AlmaLinux",
            "ansible_distribution_version": "9.5",
        },
    )
    assert str(version).strip() == expected


# ── Identity defaults survive an inventory that omits them ───────────────
@pytest.mark.parametrize("scope", [{}, {"env": None}, {"env": ""}])
def test_env_defaults_to_empty_rather_than_raising_undefined(scope: dict) -> None:
    """A consumer inventory without `env` must still resolve, not fail the role."""
    assert str(_render(_defaults()["os_sbom_env"], scope)).strip() == ""


@pytest.mark.parametrize("scope", [{}, {"tenancy": None}])
def test_tenancy_defaults_to_empty_rather_than_raising_undefined(scope: dict) -> None:
    assert str(_render(_defaults()["os_sbom_tenancy"], scope)).strip() == ""
