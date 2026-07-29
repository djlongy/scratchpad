"""freeipa_server export_config.yml — output-path resolution contract.

Replays the REAL set_fact expression and warn condition from
roles/freeipa_server/tasks/export_config.yml through a real Templar, so the
task file cannot drift from what these tests certify.

Contract:
  * absolute and '~' paths pass through untouched;
  * a RELATIVE path resolves against the directory ansible-playbook was
    invoked from ($PWD), NOT the playbook directory — `./snapshot.yml` means
    what a human expects;
  * $PWD unset (cron/CI) falls back to the playbook directory;
  * a resolved path inside the repository tree trips the loud warning (the
    old absolute-only assert survived as that warning, not as a refusal).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:
    from ansible.template import trust_as_template
except ImportError:  # <= 2.18 has no trust model; strings are templated as-is
    def trust_as_template(value: Any) -> Any:
        return value

REPO_ROOT = Path(__file__).resolve().parents[4]
TASKS = REPO_ROOT / "ansible" / "roles" / "freeipa_server" / "tasks" / "export_config.yml"


def render(expr: str, variables: dict[str, Any]) -> Any:
    scope = {k: trust_as_template(v) if isinstance(v, str) else v
             for k, v in variables.items()}
    return Templar(loader=DataLoader(), variables=scope).template(
        trust_as_template(expr))


@pytest.fixture(scope="module")
def tasks():
    return yaml.safe_load(TASKS.read_text(encoding="utf-8"))


def _task(tasks, name_fragment):
    for t in tasks:
        if name_fragment in (t.get("name") or ""):
            return t
    raise AssertionError(f"no task matching {name_fragment!r} in {TASKS}")


@pytest.fixture(scope="module")
def resolve_expr(tasks):
    t = _task(tasks, "Resolve the output path")
    sf = t.get("ansible.builtin.set_fact") or t["set_fact"]
    return sf["_freeipa_export_output"]


@pytest.fixture(scope="module")
def warn_when(tasks):
    return "{{ " + _task(tasks, "inside the repository tree")["when"] + " }}"


def _resolve(expr, output, cwd):
    """Render the set_fact expression with the hoisted cwd var supplied directly.

    The task hoists the env lookup into `vars: _freeipa_export_cwd` precisely so
    this harness can replay the resolution arithmetic without a live lookup
    plugin — the env read is ansible-core's own tested behaviour."""
    return render(expr, {
        "freeipa_server_export_output": output,
        "_freeipa_export_cwd": cwd,
    }).strip()


def test_absolute_path_passes_through(resolve_expr):
    assert _resolve(resolve_expr, "/tmp/snap.yml", "/anywhere") == "/tmp/snap.yml"


def test_tilde_passes_through_for_the_template_action_to_expand(resolve_expr):
    assert _resolve(resolve_expr, "~/snaps/snap.yml", "/anywhere") == "~/snaps/snap.yml"


def test_relative_resolves_against_invocation_cwd_not_playbook_dir(resolve_expr):
    """The whole point of the change: ./snapshot.yml lands where the human ran
    the command, which the template action alone would NOT do (it resolves
    relative dests against the playbook directory)."""
    got = _resolve(resolve_expr, "exports/snap.yml", "/home/op/work")
    assert got == "/home/op/work/exports/snap.yml"


def test_trailing_slash_cwd_does_not_double_the_separator(resolve_expr):
    got = _resolve(resolve_expr, "snap.yml", "/home/op/")
    assert got == "/home/op/snap.yml"


def test_cwd_var_reads_pwd_with_playbook_dir_fallback(tasks):
    """The hoisted var must read $PWD and fall back to playbook_dir when unset
    (cron/CI) — pinned textually since the lookup cannot run in this harness."""
    t = _task(tasks, "Resolve the output path")
    cwd_expr = t["vars"]["_freeipa_export_cwd"]
    assert "lookup('ansible.builtin.env', 'PWD')" in cwd_expr
    assert "default(playbook_dir, true)" in cwd_expr


@pytest.mark.parametrize("resolved,inside", [
    ("/repo/exports/snap.yml", True),          # repo root (playbook_dir/../..)
    ("/repo/ansible/playbooks/snap.yml", True),
    ("/tmp/snap.yml", False),
    ("/home/op/work/snap.yml", False),
])
def test_repo_tree_warning_condition(warn_when, resolved, inside):
    got = render(warn_when, {
        "_freeipa_export_output": resolved,
        "playbook_dir": "/repo/ansible/playbooks",
    })
    assert got is inside or str(got) == str(inside)


def test_template_dest_uses_the_resolved_fact(tasks):
    dest = _task(tasks, "Render the snapshot")["ansible.builtin.template"]["dest"]
    assert "_freeipa_export_output" in dest, (
        "the template must write the RESOLVED path, not the raw var")
