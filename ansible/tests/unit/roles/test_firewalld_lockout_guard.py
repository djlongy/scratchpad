"""Regressions for the firewalld lockout-safety machinery.

Every case here replays the ROLE'S OWN Jinja through a real Templar (or asserts
on the role's real task graph), rather than re-implementing the logic in Python
— a mirrored predicate can agree with itself while the YAML says something else.

Covers the 2026-07-29 adjudication of the blind-review firewalld findings:

  A  install.yml    firewalld_service_state=stopped must not abort the role
  B  main.yml       guard must be armed BEFORE the first permanent write
  C  guard_arm.yml  revert must recover a host already at drop/block
  D  main.yml       arm / re-arm / disarm must share one tag list
  E  default_zone   self-heal reload must pair with meta: reset_connection
  F  interface_audit  SSH session NIC beats the default-route NIC
  G  validate.yml   CIDR exclusivity must see inline zone sources too
  H  verify.yml     the managed-zone surface must actually be compared
  I  validate.yml   the list-type guard must be reachable, not decorative
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

from _templating import render

ROLE = pathlib.Path(__file__).resolve().parents[3] / "roles" / "firewalld"
TASKS = ROLE / "tasks"


def load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((TASKS / name).read_text())


def task(name: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    for entry in tasks:
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"task {name!r} not found in {[t.get('name') for t in tasks]}")


def arg(entry: dict[str, Any], module: str) -> Any:
    """Module args, tolerating short name or FQCN (the role uses FQCN)."""
    for key in (module, f"ansible.builtin.{module}"):
        if key in entry:
            return entry[key]
    raise AssertionError(f"{entry.get('name')!r} does not use module {module!r}")


def uses(entry: dict[str, Any], module: str) -> bool:
    return module in entry or f"ansible.builtin.{module}" in entry


def included_file(entry: dict[str, Any]) -> str:
    """The file an include_tasks pulls in, in either accepted form.

    `include_tasks: foo.yml` and the expanded `include_tasks: {file: foo.yml,
    apply: {...}}` name the same file; only the expanded form can carry apply.
    """
    spec = arg(entry, "include_tasks")
    return spec if isinstance(spec, str) else spec["file"]


def applied_tags(entry: dict[str, Any]) -> list[str]:
    """Tags stamped onto an include's CHILDREN, empty when none are."""
    spec = arg(entry, "include_tasks")
    if isinstance(spec, str):
        return []
    return spec.get("apply", {}).get("tags", [])


def main_block() -> list[dict[str, Any]]:
    """The single `block:` in tasks/main.yml holding every phase."""
    return next(t for t in load("main.yml") if "block" in t)["block"]


def names_in_order() -> list[str]:
    out = []
    for entry in main_block():
        if uses(entry, "meta"):
            out.append(f"meta:{arg(entry, 'meta')}")
        out.append(entry.get("name", ""))
    return out


# ── A: config-only mode ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "state,expect_up",
    [("started", True), ("restarted", True), ("stopped", False)],
)
def test_daemon_expected_up_derivation(state: str, expect_up: bool) -> None:
    """vars/main.yml derives the single runtime gate from the service state."""
    expr = yaml.safe_load((ROLE / "vars" / "main.yml").read_text())[
        "firewalld_daemon_expected_up"
    ]
    assert render(expr, {"firewalld_service_state": state}) is expect_up


def test_readiness_probe_is_gated_on_the_daemon_being_expected_up() -> None:
    """`until: rc == 0` is a hard gate — it must not run for state=stopped.

    Regression: the probe was unconditional, so the advertised value
    firewalld_service_state=stopped retried `firewall-cmd --state` (non-zero on
    a stopped daemon) three times and then aborted the role every run.
    """
    probe = task("Probe firewalld is responsive", load("install.yml"))
    assert probe["until"] == "_firewalld_state.rc == 0"
    assert probe["when"] == "firewalld_daemon_expected_up | bool"


def test_stopped_is_still_an_advertised_choice() -> None:
    """The fix honours the contract rather than narrowing it."""
    spec = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())
    choices = spec["argument_specs"]["main"]["options"]["firewalld_service_state"][
        "choices"
    ]
    assert "stopped" in choices


def test_every_runtime_phase_is_gated_in_config_only_mode() -> None:
    """No phase that talks to firewalld may run when the daemon is down."""
    runtime_phases = {
        "Audit interface-to-zone bindings",
        "Arm the lockout revert guard (covers every permanent write)",
        "Reconcile source CIDRs to zones (L3)",
        "Reconcile interfaces to zones (L2 — use sparingly)",
        "Reload firewalld after runtime binding reconcile",
        "Re-arm the lockout revert guard (full budget for the switch)",
        "Set the default zone",
        "Verify permanent firewalld state matches inventory",
    }
    for entry in main_block():
        if entry.get("name") in runtime_phases:
            when = entry["when"]
            when = when if isinstance(when, list) else [when]
            assert "firewalld_daemon_expected_up | bool" in when, entry["name"]

    handler = task(
        "Reload firewalld",
        yaml.safe_load((ROLE / "handlers" / "main.yml").read_text()),
    )
    assert "firewalld_daemon_expected_up | bool" in handler["when"]


# ── B: the guard must cover the reload window ───────────────────────────────

def test_guard_is_armed_before_the_handler_flush() -> None:
    """Regression: the guard armed only after flush_handlers, so the reload
    that re-templates zone XML — the step that can strand SSH on a host already
    at drop — ran with no guard at all."""
    order = names_in_order()
    first_arm = order.index("Arm the lockout revert guard (covers every permanent write)")
    flush = order.index("meta:flush_handlers")
    assert first_arm < flush, "guard must be armed before the reload it covers"


def test_guard_is_armed_before_the_first_permanent_write() -> None:
    """The guard's revert extracts a snapshot of /etc/firewalld, so the snapshot
    is worthless unless it is taken before this run re-templates that XML.
    Arming after the XML phases captures the NEW state, and the revert then
    restores the very change it exists to undo — which is what guard_arm.yml's
    own header ("Snapshot BEFORE anything rewrites zone XML") always claimed
    while main.yml did the opposite.
    """
    order = names_in_order()
    arm = order.index("Arm the lockout revert guard (covers every permanent write)")
    writes = [
        "Render custom service XML definitions",
        "Render custom zone XML definitions (sources/interfaces inlined)",
        "Remove orphaned service/zone XML definitions",
    ]
    for phase in writes:
        assert arm < order.index(phase), f"guard must arm before {phase!r}"


def test_the_audit_runs_before_anything_is_written() -> None:
    """An audit that correctly aborts a dangerous run must do so while permanent
    state is still the operator's own. Auditing after the XML phases leaves the
    zone files re-templated — potentially without their ssh service and admin
    sources — with no guard armed to put them back.
    """
    order = names_in_order()
    audit = order.index("Audit interface-to-zone bindings")
    arm = order.index("Arm the lockout revert guard (covers every permanent write)")
    first_write = order.index("Render custom service XML definitions")
    assert audit < arm < first_write


def test_guard_is_rearmed_between_the_reload_and_the_switch() -> None:
    """A slow reload/reconcile must not burn the whole timer budget."""
    order = names_in_order()
    flush = order.index("meta:flush_handlers")
    rearm = order.index("Re-arm the lockout revert guard (full budget for the switch)")
    switch = order.index("Set the default zone")
    assert flush < rearm < switch


def test_guard_arm_header_no_longer_claims_the_wrong_ordering() -> None:
    """The old header asserted it was armed before the flush while main.yml
    armed it after — the contradiction that made this finding real."""
    text = (TASKS / "guard_arm.yml").read_text()
    assert "INCLUDED TWICE ON PURPOSE" in text


# ── C: the revert must actually recover ─────────────────────────────────────

def test_guard_snapshots_permanent_config_before_the_risky_window() -> None:
    snap = task(
        "Snapshot the permanent firewalld config for the revert guard",
        load("guard_arm.yml"),
    )
    argv = arg(snap, "command")["argv"]
    assert argv[0] == "tar" and "czf" in argv
    assert "/etc/firewalld" in argv


def test_revert_restores_the_snapshot_not_just_the_default_zone() -> None:
    """Regression: reverting only the default zone set drop back to drop on a
    host whose default was ALREADY drop/block, recovering nothing."""
    arm = task("Arm the transient revert timer", load("guard_arm.yml"))
    revert = arg(arm, "command")["argv"][-1]
    assert "tar xzf" in revert, "revert must restore the config snapshot"
    assert "--set-default-zone=" in revert
    assert "firewall-cmd --reload" in revert
    # Extract failure must not stop the zone rollback — fail open toward recovery.
    assert "|| true" in revert


def test_snapshot_is_taken_once_so_rearm_cannot_capture_post_reload_state() -> None:
    tasks = load("guard_arm.yml")
    snap = task(
        "Snapshot the permanent firewalld config for the revert guard", tasks
    )
    assert (
        "not (_firewalld_guard_snapshot_done | default(false) | bool)" in snap["when"]
    )
    capture = task("Capture the live default zone before the risky window", tasks)
    assert capture["when"] == "_firewalld_guard_prev_default is not defined"


def test_prev_default_is_pinned_into_a_fact_not_read_off_the_register() -> None:
    """A SKIPPED task still registers, overwriting its variable with
    {'skipped': true, ...}. The capture is when:-guarded, so on the RE-ARM the
    register has no .stdout — reading it there failed the live run with
    "'dict object' has no attribute 'stdout'". set_fact survives the skip.
    """
    tasks = load("guard_arm.yml")
    pin = task(
        "Pin the pre-change default zone (must survive the re-arm include)", tasks
    )
    assert arg(pin, "set_fact")["_firewalld_guard_prev_default"] == (
        "{{ _fw_guard_prev_default_probe.stdout | trim }}"
    )
    assert pin["when"] == "_firewalld_guard_prev_default is not defined"

    # Only the producer (register:) and the pin may name the raw register.
    # Any other reader is reading a variable a skip can clobber — the bug.
    for name in ("guard_arm.yml", "guard_disarm.yml"):
        for entry in load(name):
            if entry.get("name") == pin["name"]:
                continue
            if entry.get("register") == "_fw_guard_prev_default_probe":
                continue
            assert "_fw_guard_prev_default_probe" not in str(entry), (
                f"{name}:{entry.get('name')} reads the register instead of the fact"
            )


def test_snapshot_is_removed_only_after_the_timer_is_proven_disarmed() -> None:
    disarm = load("guard_disarm.yml")
    order = [t.get("name") for t in disarm]
    assert order.index("Assert the revert timer is disarmed") < order.index(
        "Remove the guard's config snapshot"
    )


# ── D: tag isolation must not silently disarm the safety net ────────────────

def test_arm_rearm_and_disarm_share_one_tag_list() -> None:
    """A YAML anchor binds all three; drift here is what let `--tags
    default_zone` switch to drop with no timer and no fresh-connection proof."""
    by_name = {e.get("name"): e for e in main_block()}
    arm = by_name["Arm the lockout revert guard (covers every permanent write)"]["tags"]
    rearm = by_name["Re-arm the lockout revert guard (full budget for the switch)"][
        "tags"
    ]
    disarm = by_name["Disarm the lockout revert guard (after fresh-connection proof)"][
        "tags"
    ]
    assert arm == rearm == disarm


def test_validate_contract_is_imported_so_always_really_propagates() -> None:
    """`tags: [always]` on a DYNAMIC include is a lie: the include expands and
    every untagged child is then filtered out. Verified live — under
    `--tags default_zone` the contract ran nothing while the directly-tagged
    ICMP task still modified the firewall. Only a static import propagates tags
    to children, so this must stay import_tasks.
    """
    validate_phase = task("Validate firewalld inventory contract", main_block())
    assert validate_phase["tags"] == ["always"]
    assert uses(validate_phase, "import_tasks"), (
        "validate must be import_tasks — include_tasks does not propagate 'always'"
    )


def test_phases_carrying_meta_stay_dynamic_includes() -> None:
    """These end in `meta: reset_connection`, which ignores `when:`. They must
    stay include_tasks so the conditional lives on the include, not the child.
    """
    for phase, target in (
        ("Reload firewalld after runtime binding reconcile", "bindings_reload.yml"),
        (
            "Disarm the lockout revert guard (after fresh-connection proof)",
            "guard_disarm.yml",
        ),
    ):
        entry = task(phase, main_block())
        assert uses(entry, "include_tasks"), phase
        assert included_file(entry) == target


def test_every_tagged_dynamic_include_stamps_its_children() -> None:
    """Tags on a dynamic include select the include STATEMENT only — its
    untagged children are then filtered out, so a tag-isolated run expands the
    include and does nothing. `apply.tags` is what makes the child tasks
    selectable, and it must carry the same tags as the include or the two
    disagree about when the phase runs.
    """
    offenders = []
    for entry in main_block():
        if not uses(entry, "include_tasks") or not entry.get("tags"):
            continue
        if sorted(applied_tags(entry)) != sorted(entry["tags"]):
            offenders.append(
                f"{entry.get('name')!r}: tags={sorted(entry['tags'])} "
                f"apply.tags={sorted(applied_tags(entry))}"
            )
    assert not offenders, "dynamic includes whose children are not stamped:\n" + "\n".join(
        offenders
    )


def test_default_zone_fails_closed_when_an_expected_guard_never_armed() -> None:
    refuse = task(
        "Refuse a restrictive switch when the expected lockout guard never armed",
        load("default_zone.yml"),
    )
    assert refuse["when"] == [
        "_fw_target_lockdown | bool",
        "_firewalld_guard_expected | default(false) | bool",
        "not (_firewalld_guard_active | default(false) | bool)",
    ]
    assert uses(refuse, "fail")


def test_guard_expected_is_published_by_the_always_tagged_validate_phase() -> None:
    """The fail-closed assert is only reachable if this fact survives every tag
    selection — validate.yml is tags:[always]."""
    validate_phase = task("Validate firewalld inventory contract", main_block())
    assert validate_phase["tags"] == ["always"]
    assert any(
        t.get("name") == "Determine whether a lockout guard is expected for this run"
        for t in load("validate.yml")
    )


@pytest.mark.parametrize(
    "guard,reload_on,up,inv_default,lockdown,expect",
    [
        (True, True, True, "drop", False, True),
        (True, True, True, "", True, True),      # live default already restrictive
        (False, True, True, "drop", True, False),  # operator disabled the guard
        (True, False, True, "drop", True, False),  # deferred reload
        (True, True, False, "drop", True, False),  # config-only mode
        (True, True, True, "public", False, False),  # nothing restrictive
    ],
)
def test_guard_expected_expression(
    guard: bool, reload_on: bool, up: bool,
    inv_default: str, lockdown: bool, expect: bool,
) -> None:
    expr = task(
        "Determine whether a lockout guard is expected for this run",
        load("validate.yml"),
    )["ansible.builtin.set_fact"]["_firewalld_guard_expected"]
    assert render(expr, {
        "firewalld_lockout_guard": guard,
        "firewalld_reload": reload_on,
        "firewalld_daemon_expected_up": up,
        "firewalld_default_zone": inv_default,
        "_firewalld_lockdown": lockdown,
        "ansible_check_mode": False,
    }) is expect


# ── E: every reload pairs with a reconnect ──────────────────────────────────

def test_selfheal_reload_pairs_with_reset_connection() -> None:
    """Regression: this was the one reload issued bare, and it fires in the
    narrowest window of all — immediately before the restrictive switch."""
    inc = task(
        "Self-heal reload so permanent ssh bindings reach runtime",
        load("default_zone.yml"),
    )
    assert arg(inc, "include_tasks") == "default_zone_selfheal_reload.yml"
    body = load("default_zone_selfheal_reload.yml")
    assert arg(body[-1], "meta") == "reset_connection"


def test_no_bare_reload_remains_in_default_zone() -> None:
    for entry in load("default_zone.yml"):
        cmd = "".join(str(entry.get(k, "")) for k in
                      ("command", "shell", "ansible.builtin.command", "ansible.builtin.shell"))
        assert "firewall-cmd --reload" not in str(cmd), entry.get("name")


# ── F: the audit must judge the operator's real NIC ─────────────────────────

def _ssh_iface(ssh_local_ip: str, default_iface: str | None,
               addrs: dict[str, str]) -> Any:
    expr = task(
        "Resolve the SSH ingress interface (SSH session first, else default route)",
        load("interface_audit.yml"),
    )["ansible.builtin.set_fact"]["_fw_ssh_iface"]
    scope: dict[str, Any] = {
        "_fw_ssh_local_ip": ssh_local_ip,
        "_fw_audit_ifaces": list(addrs),
        "ansible_facts": {
            nic: {"ipv4": {"address": ip}} for nic, ip in addrs.items()
        },
    }
    if default_iface is not None:
        scope["ansible_default_ipv4"] = {"interface": default_iface}
    return render(expr, scope)


ADDRS = {"ens192": "192.168.10.92", "ens224": "192.168.50.92", "ens256": "10.50.1.92"}


def test_ssh_session_nic_beats_the_default_route_nic() -> None:
    """THE finding: on a multi-homed host the audit judged the default-route
    NIC, so it could pass while the operator's real NIC fell to drop."""
    assert _ssh_iface("192.168.50.92", "ens192", ADDRS) == "ens224"


def test_default_route_is_the_fallback_when_ssh_connection_is_unreadable() -> None:
    assert _ssh_iface("", "ens192", ADDRS) == "ens192"


def test_session_and_default_route_agree_on_a_single_homed_host() -> None:
    assert _ssh_iface("192.168.10.92", "ens192", ADDRS) == "ens192"


def test_unmatched_ssh_ip_falls_back_rather_than_returning_empty() -> None:
    assert _ssh_iface("10.99.99.99", "ens192", ADDRS) == "ens192"


def test_no_default_route_and_no_ssh_ip_yields_empty_not_an_error() -> None:
    assert _ssh_iface("", None, ADDRS) == ""


# ── G: CIDR exclusivity must see both declaration routes ────────────────────

def _pairs(bindings: list[dict[str, str]],
           zones: list[dict[str, Any]]) -> list[dict[str, str]]:
    expr = task(
        "Build the full source→zone map (bindings plus inline zone sources)",
        load("validate.yml"),
    )["ansible.builtin.set_fact"]["_firewalld_source_zone_pairs"]
    return render(expr, {
        "firewalld_source_zone_bindings": bindings,
        "firewalld_zones": zones,
    })


def _zones_for(source: str, pairs: list[dict[str, str]]) -> set[str]:
    return {p["zone"] for p in pairs if p["source"] == source}


def test_inline_zone_sources_are_collected_alongside_bindings() -> None:
    pairs = _pairs(
        [{"zone": "mgmt", "source": "192.168.10.0/24"}],
        [{"name": "data", "sources": ["10.50.1.0/24"]}],
    )
    assert _zones_for("192.168.10.0/24", pairs) == {"mgmt"}
    assert _zones_for("10.50.1.0/24", pairs) == {"data"}


def test_dual_bind_across_inline_and_binding_is_now_visible() -> None:
    """THE finding: inline on zone A + bound to zone B passed validation and was
    written into BOTH zone files by templates/zone.xml.j2."""
    pairs = _pairs(
        [{"zone": "mgmt", "source": "192.168.10.0/24"}],
        [{"name": "access", "sources": ["192.168.10.0/24"]}],
    )
    assert _zones_for("192.168.10.0/24", pairs) == {"mgmt", "access"}


def test_dual_bind_across_two_inline_zones_is_visible() -> None:
    pairs = _pairs(
        [],
        [
            {"name": "access", "sources": ["192.168.10.0/24"]},
            {"name": "data", "sources": ["192.168.10.0/24"]},
        ],
    )
    assert len(_zones_for("192.168.10.0/24", pairs)) == 2


def test_same_cidr_twice_on_one_zone_is_not_a_conflict() -> None:
    pairs = _pairs(
        [{"zone": "mgmt", "source": "192.168.10.0/24"}],
        [{"name": "mgmt", "sources": ["192.168.10.0/24"]}],
    )
    assert _zones_for("192.168.10.0/24", pairs) == {"mgmt"}


def test_null_and_missing_sources_keys_do_not_explode() -> None:
    pairs = _pairs(
        [],
        [{"name": "a", "sources": None}, {"name": "b"}],
    )
    assert pairs == []


def test_pairs_record_which_route_declared_each_source() -> None:
    """The fail message names the route so the operator knows where to look."""
    pairs = _pairs(
        [{"zone": "mgmt", "source": "192.168.10.0/24"}],
        [{"name": "access", "sources": ["192.168.10.0/24"]}],
    )
    routes = {p["via"] for p in pairs}
    assert "firewalld_source_zone_bindings" in routes
    assert "firewalld_zones[access].sources" in routes


def test_exclusivity_assert_loops_over_the_combined_pairs() -> None:
    """Regression guard: the loop must not go back to bindings-only."""
    assert_task = task(
        "Assert each source CIDR binds to exactly one zone", load("validate.yml")
    )
    assert "_firewalld_source_zone_pairs" in assert_task["loop"]


# ── H: verify.yml must compare the managed-zone surface ─────────────────────
#
# THE finding (reproduced on a multi-NIC lab host): verify.yml checked
# the default zone, permanent sources, runtime sources and permanent interfaces
# — and nothing else. With 31337/tcp, service telnet AND masquerade injected into
# the managed `mgmt` zone permanently, the phase still recapped failed=0. A later
# apply re-templates the zone and wipes the injection, so this was never drift
# that persists; it was the reporting phase reporting nothing.

VERIFY_COMPARE = "Assert permanent zone attributes match inventory"

# Real `firewall-cmd --permanent --zone=mgmt --list-all` output. Empty attributes
# really do come back as "key: " with a trailing space, so the samples carry one.
LIST_ALL_CLEAN = "\n".join([
    "mgmt (active)",
    "  target: default",
    "  icmp-block-inversion: no",
    "  interfaces: ",
    "  sources: 192.168.10.0/24 192.168.0.0/24",
    "  services: ssh",
    "  ports: ",
    "  protocols: icmp",
    "  forward: no",
    "  masquerade: no",
    "  forward-ports: ",
    "  source-ports: ",
    "  icmp-blocks: ",
    "  rich rules: ",
])

# The same zone after the repro's three injections.
LIST_ALL_INJECTED = LIST_ALL_CLEAN.replace(
    "  services: ssh", "  services: ssh telnet",
).replace(
    "  ports: ", "  ports: 31337/tcp",
).replace(
    "  masquerade: no", "  masquerade: yes",
)

MGMT = {
    "name": "mgmt",
    "short": "Management",
    "target": "default",
    "services": ["ssh"],
    "protocols": ["icmp"],
}


def _set_fact_expr(task_name: str, fact: str) -> Any:
    return task(task_name, load("verify.yml"))["ansible.builtin.set_fact"][fact]


def _parse(list_all_by_zone: dict[str, str]) -> dict[str, Any]:
    """Replay verify.yml's own --list-all parser."""
    return render(
        _set_fact_expr(
            "Parse the permanent zone configuration into a comparable map",
            "_firewalld_permanent_zone_state",
        ),
        {
            "_firewalld_zone_all": {
                "results": [
                    {"item": {"name": zone}, "stdout_lines": text.splitlines()}
                    for zone, text in list_all_by_zone.items()
                ]
            }
        },
    )


def _expected(zones: list[dict[str, Any]], *, default_zone: str = "",
              allow_icmp: bool = False,
              live_default: str = "drop") -> dict[str, Any]:
    """Replay verify.yml's own inventory-side expectation builder."""
    return render(
        _set_fact_expr(
            "Compute the expected permanent zone state from inventory",
            "_firewalld_expected_zone_state",
        ),
        {
            "firewalld_zones": zones,
            "firewalld_default_zone": default_zone,
            "firewalld_allow_icmp": allow_icmp,
            "_firewalld_verify_default": {"stdout": live_default},
        },
    )


def _cond(expr: str, scope: dict[str, Any]) -> Any:
    """Evaluate a bare `when:`/`that:` expression.

    Templar only renders a string that carries Jinja delimiters, so an
    undelimited conditional comes back verbatim — and a non-empty string is
    truthy, which makes every predicate "pass". Ansible wraps these itself
    before evaluating them; so does this.
    """
    return render("{{ " + expr + " }}", scope)


def _drift(zone: dict[str, Any], attr: str, list_all: str,
           **expected_kwargs: Any) -> dict[str, Any]:
    """Replay the comparison assert's own vars + `that:` for one zone/attribute.

    Returns the rendered scope plus whether each `that:` expression held, so a
    case can assert on the verdict AND on what the operator would be told.
    """
    entry = task(VERIFY_COMPARE, load("verify.yml"))
    scope: dict[str, Any] = dict(entry["vars"])
    scope.update({
        "item": [zone, attr],
        "_firewalld_permanent_zone_state": _parse({zone["name"]: list_all}),
        "_firewalld_expected_zone_state": _expected([zone], **expected_kwargs),
    })
    rendered = {key: render(value, scope) for key, value in entry["vars"].items()}
    rendered["passed"] = all(
        _cond(expr, scope) for expr in arg(entry, "assert")["that"]
    )
    rendered["fail_msg"] = render(arg(entry, "assert")["fail_msg"], scope)
    return rendered


def _compared_attributes() -> list[str]:
    """The attribute names the comparison loop iterates."""
    loop = task(VERIFY_COMPARE, load("verify.yml"))["loop"]
    return re.findall(r"'([a-z-]+)'", loop)


def test_parser_reads_every_list_all_key() -> None:
    parsed = _parse({"mgmt": LIST_ALL_CLEAN})["mgmt"]
    assert parsed["target"] == "default"
    assert parsed["services"] == "ssh"
    assert parsed["protocols"] == "icmp"
    assert parsed["masquerade"] == "no"
    assert parsed["sources"] == "192.168.10.0/24 192.168.0.0/24"


def test_parser_keeps_empty_attributes_empty_rather_than_undefined() -> None:
    """`ports: ` with nothing after it must read as "no ports", not as missing —
    otherwise an empty attribute would fall through to the `| default('')`
    and an actual parse failure would look identical to a clean zone."""
    parsed = _parse({"mgmt": LIST_ALL_CLEAN})["mgmt"]
    assert parsed["ports"] == ""
    assert parsed["icmp-blocks"] == ""


def test_parser_ignores_the_zone_header_line() -> None:
    """`mgmt (active)` carries no colon and must not become an attribute."""
    parsed = _parse({"mgmt": LIST_ALL_CLEAN})["mgmt"]
    assert "mgmt (active)" not in parsed
    assert all(":" not in key for key in parsed)


def test_every_compared_attribute_is_a_key_firewall_cmd_actually_prints() -> None:
    """A typo in the loop list would compare against `| default('')` forever —
    i.e. silently pass. Every name must appear in real --list-all output."""
    printed = set(_parse({"mgmt": LIST_ALL_CLEAN})["mgmt"])
    assert set(_compared_attributes()) <= printed


@pytest.mark.parametrize("zone", [
    MGMT,
    {"name": "edge", "target": "ACCEPT", "services": ["http", "https"],
     "ports": [{"port": 8443, "protocol": "tcp"},
               {"port": "9000-9100", "protocol": "udp"}],
     "protocols": ["icmp", "esp"],
     "source_ports": [{"port": 1234, "protocol": "tcp"}],
     "icmp_blocks": ["echo-request"],
     "icmp_block_inversion": True, "masquerade": True, "forward": True},
    {"name": "bare"},
])
def test_expected_state_matches_what_the_zone_template_writes(
    zone: dict[str, Any],
) -> None:
    """The anti-drift test. Expectations are derived from firewalld_zones with
    the same expressions templates/zone.xml.j2 uses; render the template for the
    same zone and the two must agree element for element, or verify is checking
    a contract the role does not write."""
    text = render((ROLE / "templates" / "zone.xml.j2").read_text(), {
        "item": zone,
        "firewalld_source_zone_bindings": [],
        "firewalld_interface_zone_bindings": [],
    })
    target = re.search(r'<zone target="([^"]+)"', text)
    emitted = {
        "services": re.findall(r'<service name="([^"]+)"/>', text),
        "ports": [f"{p}/{q}" for p, q in
                  re.findall(r'<port port="([^"]+)" protocol="([^"]+)"/>', text)],
        "protocols": re.findall(r'<protocol value="([^"]+)"/>', text),
        "source-ports": [f"{p}/{q}" for p, q in
                         re.findall(r'<source-port port="([^"]+)" protocol="([^"]+)"/>',
                                    text)],
        "icmp-blocks": re.findall(r'<icmp-block name="([^"]+)"/>', text),
        "target": target.group(1) if target else "default",
        "masquerade": "yes" if "<masquerade/>" in text else "no",
        "forward": "yes" if "<forward/>" in text else "no",
        "icmp-block-inversion": "yes" if "<icmp-block-inversion/>" in text else "no",
    }
    expected = _expected([zone])[zone["name"]]
    for attr in _compared_attributes():
        assert expected[attr] == emitted[attr], attr


@pytest.mark.parametrize("attr", [
    "services", "ports", "protocols", "source-ports", "icmp-blocks",
    "target", "masquerade", "forward", "icmp-block-inversion",
])
def test_converged_zone_produces_no_drift_on_any_attribute(attr: str) -> None:
    """The false-positive guard: a correctly converged zone must pass on every
    attribute, or verify cries wolf on every host in the estate."""
    assert _drift(MGMT, attr, LIST_ALL_CLEAN)["passed"]


@pytest.mark.parametrize("attr,element", [
    ("services", "telnet"),
    ("ports", "31337/tcp"),
])
def test_injected_element_is_reported_as_unexpected(attr: str,
                                                    element: str) -> None:
    """THE repro, for the two list attributes it injected."""
    result = _drift(MGMT, attr, LIST_ALL_INJECTED)
    assert not result["passed"]
    assert result["_extra"] == [element]
    assert element in result["fail_msg"]
    assert "UNEXPECTED on the host" in result["fail_msg"]
    assert "mgmt" in result["fail_msg"]


def test_injected_masquerade_is_reported_with_both_values() -> None:
    """THE repro's third injection. Scalars get their own phrasing — an
    extra/missing list reads as nonsense for a yes/no attribute."""
    result = _drift(MGMT, "masquerade", LIST_ALL_INJECTED)
    assert not result["passed"]
    assert "masquerade=yes" in result["fail_msg"]
    assert "inventory declares no" in result["fail_msg"]


def test_all_three_injections_land_in_one_looped_task() -> None:
    """They must be reported TOGETHER. A looped assert runs every item and
    collects them in results[]; splitting list and scalar attributes into two
    tasks would stop the play at the first and hide masquerade entirely."""
    attrs = _compared_attributes()
    assert {"services", "ports", "masquerade"} <= set(attrs)
    failed = [a for a in ("services", "ports", "masquerade")
              if not _drift(MGMT, a, LIST_ALL_INJECTED)["passed"]]
    assert failed == ["services", "ports", "masquerade"]


def test_element_missing_from_the_host_is_reported_as_missing() -> None:
    """Both directions are failures, and the message must say which."""
    stripped = LIST_ALL_CLEAN.replace("  services: ssh", "  services: ")
    result = _drift(MGMT, "services", stripped)
    assert not result["passed"]
    assert result["_missing"] == ["ssh"]
    assert "MISSING from the host" in result["fail_msg"]


def test_only_zones_in_firewalld_zones_are_compared() -> None:
    """A stock `public`, or a built-in `drop` used as the default, carries state
    the inventory contract says nothing about. Asserting on it would fail every
    host that has one."""
    entry = task(VERIFY_COMPARE, load("verify.yml"))
    assert entry["loop"].lstrip().startswith("{{ firewalld_zones | product(")
    read = task("Read permanent configuration of each managed zone",
                load("verify.yml"))
    assert read["loop"] == "{{ firewalld_zones }}"


def test_the_comparison_reads_permanent_state_not_runtime() -> None:
    """Runtime is already covered for sources; this phase's job is the permanent
    XML the next reload will make live."""
    read = task("Read permanent configuration of each managed zone",
                load("verify.yml"))
    assert "--permanent" in arg(read, "command")
    assert "--list-all" in arg(read, "command")


def test_a_managed_zone_absent_from_permanent_config_is_named() -> None:
    """The read is failed_when: false so this assert owns the message."""
    check = task("Assert every managed zone exists in the permanent configuration",
                 load("verify.yml"))
    assert "(_res.rc | default(1)) == 0" in arg(check, "assert")["that"]
    assert "{{ item.name }}" in arg(check, "assert")["fail_msg"]
    read = task("Read permanent configuration of each managed zone",
                load("verify.yml"))
    assert read["failed_when"] is False


def test_icmp_on_a_managed_default_zone_is_not_counted_as_drift() -> None:
    """main.yml opens `protocol: icmp` permanently on the EFFECTIVE default zone
    when firewalld_allow_icmp. If that zone is managed, the element is legitimate
    and must not be reported — this is exactly the false-positive the brief said
    to stop and report rather than ship."""
    zone = {"name": "mgmt", "services": ["ssh"]}
    listing = LIST_ALL_CLEAN.replace("  protocols: icmp", "  protocols: icmp")
    result = _drift(zone, "protocols", listing,
                    allow_icmp=True, live_default="mgmt")
    assert result["passed"]


def test_icmp_is_not_expected_on_a_zone_that_is_not_the_default() -> None:
    """The allowance is scoped to the default zone only — everywhere else icmp
    must still be declared in inventory."""
    zone = {"name": "mgmt", "services": ["ssh"]}
    result = _drift(zone, "protocols", LIST_ALL_CLEAN,
                    allow_icmp=True, live_default="drop")
    assert not result["passed"]
    assert result["_extra"] == ["icmp"]


# ── I: the list-type guard must be reachable ────────────────────────────────
#
# validate.yml used to assert `_firewalld_ssh_zones is not string` AFTER the
# set_fact that builds it. set_fact hands back a real list whatever went in, so
# the assert could not fail: with firewalld_zones set to the string "not-a-list"
# the builder walks its CHARACTERS, finds no services on any of them, and yields
# [] — the assert passes and the empty result then reads as "no zone opens ssh",
# a claim about the firewall rather than a report about the inventory. The guard
# now inspects the RAW inventory value, before anything coerces it.

LIST_GUARD = "Assert the firewalld list contracts are lists"


def _guard_holds(value: Any) -> bool:
    entry = task(LIST_GUARD, load("validate.yml"))
    scope = {"item": {"key": "firewalld_zones", "value": value}}
    return all(_cond(expr, scope) for expr in arg(entry, "assert")["that"])


@pytest.mark.parametrize("value,holds", [
    ([], True),
    ([{"name": "mgmt"}], True),
    ("not-a-list", False),                      # THE case the old assert missed
    ("", False),
    ({"name": "mgmt"}, False),                  # a bare dict is `sequence` too
])
def test_list_contract_guard_verdicts(value: Any, holds: bool) -> None:
    assert _guard_holds(value) is holds


def test_list_contract_guard_names_the_offending_variable() -> None:
    entry = task(LIST_GUARD, load("validate.yml"))
    msg = render(arg(entry, "assert")["fail_msg"],
                 {"item": {"key": "firewalld_zones", "value": "not-a-list"}})
    assert "firewalld_zones must be a list" in msg
    assert "str" in msg


def test_list_contract_guard_runs_before_anything_consumes_the_lists() -> None:
    """Reachability is positional: every consumer downstream either coerces the
    value or walks it silently, so the guard is only real if it runs first."""
    tasks = load("validate.yml")
    assert tasks[0]["name"] == LIST_GUARD


def test_list_contract_guard_covers_every_documented_list_var() -> None:
    """Each of these is iterated somewhere in the role, and several are supplied
    from a Jinja expression in inventory (see group_vars/fwtest.yml), which is
    how a scalar gets in."""
    entry = task(LIST_GUARD, load("validate.yml"))
    covered = set(entry["vars"]["_fw_list_vars"])
    assert covered == {
        "firewalld_zones",
        "firewalld_zones_remove",
        "firewalld_services",
        "firewalld_services_remove",
        "firewalld_source_zone_bindings",
        "firewalld_interface_zone_bindings",
    }


def test_the_unreachable_post_coercion_assert_is_gone() -> None:
    """It read as coverage while being unexercisable. Replaced, not kept."""
    text = (TASKS / "validate.yml").read_text()
    assert "Assert ssh-zone detection produced a list" not in text



# ── J: an inheriting NIC follows the default zone wherever it goes ───────────
#
# `--get-zone-of-interface` reports a zone NAME for a NIC that merely inherits
# the current default, so it cannot answer "is this NIC explicitly bound?".
# Probed on two lab hosts at default=drop: an unbound NIC and a NIC permanently
# bound to another zone both answered `drop`. Only the permanent interface lists
# separate them, and the difference decides whether the NIC follows a switch to
# a restrictive default.

def _falls(iface_zone: str, explicit: bool, inventory_default: str,
           live_default: str) -> Any:
    expr = task(
        "Classify whether the SSH ingress NIC falls onto a restrictive default",
        load("interface_audit.yml"),
    )["ansible.builtin.set_fact"]["_fw_ssh_falls_to_restrictive_default"]
    return render(expr, {
        "_fw_ssh_iface_zone": iface_zone,
        "_fw_ssh_iface_explicit": explicit,
        "firewalld_default_zone": inventory_default,
        "_fw_audit_live_default_name": live_default,
    })


def test_inheriting_nic_reported_as_the_live_default_is_caught() -> None:
    """THE residual: live default `public`, inventory switching to `drop`. The
    NIC inherits, so firewall-cmd answers `public` — it matches no restrictive
    zone name, every earlier branch is False, and the switch then parks the
    operator's own NIC in drop."""
    assert _falls("public", explicit=False,
                  inventory_default="drop", live_default="public") is True


def test_explicitly_bound_nic_is_not_dragged_along_by_the_default() -> None:
    """The control that keeps the gate from crying wolf: a NIC pinned to a
    service zone stays there when the default becomes drop."""
    assert _falls("access", explicit=True,
                  inventory_default="drop", live_default="public") is False


def test_inheriting_nic_is_fine_while_no_default_is_restrictive() -> None:
    assert _falls("public", explicit=False,
                  inventory_default="", live_default="public") is False


def test_explicit_bind_to_a_restrictive_zone_still_counts() -> None:
    """Pinned directly to drop is a lockout by declaration, not by inheritance."""
    assert _falls("drop", explicit=True,
                  inventory_default="drop", live_default="drop") is True


def test_unbound_nic_still_caught_when_the_probe_says_nothing() -> None:
    """The pre-existing empty-zone branch must survive the new one."""
    assert _falls("", explicit=False,
                  inventory_default="drop", live_default="public") is True


def test_explicit_flag_defaults_to_inheriting_when_the_probe_failed() -> None:
    """Fail closed: if the permanent lists could not be read, treat the NIC as
    inheriting so the gate warns rather than waving the run through."""
    expr = task(
        "Classify whether the SSH ingress NIC falls onto a restrictive default",
        load("interface_audit.yml"),
    )["ansible.builtin.set_fact"]["_fw_ssh_falls_to_restrictive_default"]
    assert render(expr, {
        "_fw_ssh_iface_zone": "public",
        "firewalld_default_zone": "drop",
        "_fw_audit_live_default_name": "public",
    }) is True
