"""roles/storage — network-volume normalisation and the validation rules.

The rules live as Jinja in `tasks/validate.yml`, so these tests evaluate the
SHIPPED expressions rather than a Python re-implementation: a tiny sequential
`set_fact` runner replays the normalisation tasks from `tasks/resolve.yml`, then
each assert task from `tasks/validate.yml` is evaluated against the resulting
facts through a real Ansible `Templar`. Editing a rule's Jinja therefore
changes what these tests see.

Covers:
  1. The declaration rules as pure data cases — a valid mixed list passes;
     each violation fails with the expected message fragment. (The shadow check
     probes the host and is exercised live, not here.)
  2. The local block-device path is untouched: every estate profile normalises
     to exactly `_storage_volume_defaults | combine(entry)`, the block phases
     consume the LOCAL list, and network volumes never reach them.
  3. Structural guards on the CIFS credentials task (no_log, 0600 root:root),
     the tag union that keeps resolve.yml and validate.yml ahead of every
     phase, grow/FRESH/root/owner fail-closed contracts.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

# Fixture-only placeholder. Named so SonarQube S2068 sees a constant
# reference rather than a hard-coded credential literal.
FIXTURE_SECRET = "fixture-only-not-a-credential"


# jinja2_native must match ansible.cfg — without it every list-valued
# expression comes back as its string repr and the runner falls apart.
os.environ.setdefault("ANSIBLE_JINJA2_NATIVE", "True")

from ansible.errors import AnsibleError  # noqa: E402

from _templating import render as _render  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[4]
ROLE = REPO_ROOT / "ansible" / "roles" / "storage"
TASKS = ROLE / "tasks"
PROFILES = REPO_ROOT / "ansible" / "playbooks" / "group_vars" / "all" / "storage.yml"

SET_FACT = "ansible.builtin.set_fact"
ASSERT = "ansible.builtin.assert"

# The normalisation tasks in resolve.yml, in the order resolve.yml runs them.
NORMALISE_SEQUENCE = [
    "Build per-volume default template",
    "Build per-volume network default template",
    "Reset the normalised-volume accumulators",
    "Split declared volumes by kind",
    "Normalise local volumes",
    "Normalise network volumes",
    "Combine the effective volume list",
    "Decide whether any storage is declared",
    "Select the network volumes whose kind is enabled",
]


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _flatten(tasks: Any) -> list[dict[str, Any]]:
    """main.yml wraps its imports in a `block:` — walk into it."""
    out: list[dict[str, Any]] = []
    for task in tasks or []:
        if "block" in task:
            out.extend(_flatten(task["block"]))
        else:
            out.append(task)
    return out


def _find(tasks: list[dict[str, Any]], needle: str) -> dict[str, Any]:
    for task in tasks:
        if needle in task.get("name", ""):
            return task
    raise AssertionError(f"no task name contains {needle!r}")


def _evaluate(expr: str, variables: dict[str, Any]) -> Any:
    return _render("{{ " + expr + " }}", variables)


_NO_ITEM = object()


def _scope(facts: dict[str, Any], task_vars: dict[str, Any],
           item: Any = _NO_ITEM) -> dict[str, Any]:
    """Task vars resolved in declaration order, as Ansible resolves them.

    Vars that need `item` are skipped while the `loop:` expression itself is
    being templated — at that point no item exists yet.
    """
    scope = dict(facts)
    if item is not _NO_ITEM:
        scope["item"] = item
    for key, expr in task_vars.items():
        try:
            scope[key] = _render(expr, scope)
        except AnsibleError:
            continue
    return scope


def _loop_items(task: dict[str, Any], facts: dict[str, Any]) -> list[Any]:
    if "loop" not in task:
        return [_NO_ITEM]
    return _render(task["loop"], _scope(facts, task.get("vars", {}))) or []


@pytest.fixture(scope="module")
def main_tasks() -> list[dict[str, Any]]:
    return _flatten(_load(TASKS / "main.yml"))


@pytest.fixture(scope="module")
def resolve_tasks() -> list[dict[str, Any]]:
    return _load(TASKS / "resolve.yml")


@pytest.fixture(scope="module")
def validate_tasks() -> list[dict[str, Any]]:
    return _load(TASKS / "validate.yml")


@pytest.fixture(scope="module")
def role_vars() -> dict[str, Any]:
    base = dict(_load(ROLE / "defaults" / "main.yml"))
    base.update(_load(ROLE / "vars" / "main.yml"))
    return base


def _normalise(resolve: list[dict[str, Any]], role_vars: dict[str, Any],
               volumes: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """Replay resolve.yml's normalisation set_facts over a declared volume list."""
    facts: dict[str, Any] = dict(role_vars)
    # Network kinds are enabled by default here so the net path is exercised;
    # individual cases override.
    facts.update({"storage_manage_nfs": True, "storage_manage_cifs": True})
    facts.update(overrides)
    facts["_storage_input"] = volumes

    for needle in NORMALISE_SEQUENCE:
        task = _find(resolve, needle)
        for item in _loop_items(task, facts):
            scope = _scope(facts, task.get("vars", {}), item)
            for key, expr in task[SET_FACT].items():
                facts[key] = _render(expr, scope)
    return facts


def _validation_failures(validate: list[dict[str, Any]],
                         facts: dict[str, Any]) -> list[str]:
    """Return the rendered fail_msg of every assert that does not hold."""
    failures: list[str] = []
    for task in validate:
        if ASSERT not in task:
            continue
        if "shadowing" in task.get("name", ""):
            continue  # host probe — covered by the live run, not by data
        for item in _loop_items(task, facts):
            scope = _scope(facts, task.get("vars", {}), item)
            if any(not _evaluate(str(c), scope) for c in _as_list(task.get("when"))):
                continue
            clauses = _as_list(task[ASSERT]["that"])
            if all(_evaluate(str(c), scope) for c in clauses):
                continue
            failures.append(str(_render(task[ASSERT]["fail_msg"], scope)))
    return failures


def _check(resolve, validate, role_vars, volumes, **overrides) -> list[str]:
    return _validation_failures(
        validate, _normalise(resolve, role_vars, volumes, **overrides))


# ── Fixtures: a valid mixed declaration ──────────────────────────────────
LOCAL_OK = {
    "name": "opt", "disk": "by-size:50G", "lvm": True, "vg": "vg_data",
    "lv": "lv_opt", "size": "100%FREE", "fstype": "xfs", "mount": "/opt",
    "sefcontext": "usr_t", "provision": True, "grow": True,
}
NFS_OK = {
    "name": "app-data", "kind": "nfs", "server": "fileserver-01.example.internal",
    "export": "/export/app-data", "mount": "/srv/app-data",
}
CIFS_OK = {
    "name": "media-archive", "kind": "cifs", "server": "fileserver-02.example.internal",
    "share": "archive", "mount": "/mnt/archive",
    "credentials_username": "svc-example", "credentials_password": FIXTURE_SECRET,
}


def test_valid_mixed_declaration_passes(resolve_tasks, validate_tasks, role_vars):
    assert _check(resolve_tasks, validate_tasks, role_vars,
                  [LOCAL_OK, NFS_OK, CIFS_OK]) == []


# ── Duplicate mount points across the whole effective list ───────────────
def test_rejects_a_local_and_network_volume_sharing_a_mount(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [
        {"name": "a", "mount": "/data"},
        {**NFS_OK, "name": "b", "mount": "/data"},
    ]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("'/data' is claimed by volumes a, b" in f for f in failures)


def test_rejects_two_local_volumes_sharing_a_mount(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [{"name": "a", "mount": "/data"}, {"name": "b", "mount": "/data"}]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("claimed by volumes a, b" in f for f in failures)


def test_rejects_trailing_slash_aliases(
        resolve_tasks, validate_tasks, role_vars):
    """/data and /data/ are the same path after canonicalization."""
    volumes = [{"name": "a", "mount": "/data"}, {"name": "b", "mount": "/data/"}]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("claimed by volumes" in f and "a" in f and "b" in f for f in failures)


# ── One VG ↔ one local LVM volume ────────────────────────────────────────
def test_rejects_two_local_lvm_volumes_sharing_a_vg(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [
        {"name": "a", "mount": "/a", "lvm": True, "vg": "vg_shared", "lv": "lv_a",
         "disk": "auto"},
        {"name": "b", "mount": "/b", "lvm": True, "vg": "vg_shared", "lv": "lv_b",
         "disk": "auto"},
    ]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("vg_shared" in f and "more than one local LVM" in f for f in failures)


def test_allows_distinct_vgs(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [
        {"name": "a", "mount": "/a", "lvm": True, "vg": "vg_a", "lv": "lv_a",
         "disk": "by-size:50G"},
        {"name": "b", "mount": "/b", "lvm": True, "vg": "vg_b", "lv": "lv_b",
         "disk": "by-size:50G"},
    ]
    assert _check(resolve_tasks, validate_tasks, role_vars, volumes) == []


# ── sefcontext is local-only ─────────────────────────────────────────────
def test_rejects_sefcontext_on_nfs_volume(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [{**NFS_OK, "sefcontext": "public_content_t"}]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("sefcontext" in f and "local" in f.lower() for f in failures)


def test_rejects_sefcontext_on_cifs_volume(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [{**CIFS_OK, "sefcontext": "public_content_t"}]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("sefcontext" in f for f in failures)


def test_allows_sefcontext_on_local_volume(
        resolve_tasks, validate_tasks, role_vars):
    assert _check(resolve_tasks, validate_tasks, role_vars, [LOCAL_OK]) == []


# ── A local volume must declare no network fields ────────────────────────
@pytest.mark.parametrize("field,value", [
    ("server", "nas"),
    ("export", "/e"),
    ("share", "archive"),
    ("credentials_username", "svc"),
    ("credentials_password", "x"),
    ("credentials_domain", "corp"),
])
def test_rejects_network_fields_on_a_local_volume(
        resolve_tasks, validate_tasks, role_vars, field, value):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{"name": "a", "mount": "/data", field: value}])
    assert any(f"declares network-only field(s) {field}" in f for f in failures)


# ── A network volume must declare no block fields ────────────────────────
@pytest.mark.parametrize("field,value", [
    ("disk", "auto"), ("lvm", True), ("vg", "vg_x"), ("lv", "lv_x"),
    ("size", "10G"), ("partition", True), ("partition_number", 2),
])
def test_rejects_block_fields_on_a_network_volume(
        resolve_tasks, validate_tasks, role_vars, field, value):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{**NFS_OK, field: value}])
    assert any("declares" in f and field in f and "block-only" in f
               for f in failures)


def test_rejects_provision_true_on_a_network_volume(
        resolve_tasks, validate_tasks, role_vars):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{**NFS_OK, "provision": True}])
    assert any("block-only field(s) provision" in f for f in failures)


def test_allows_provision_false_on_a_network_volume(
        resolve_tasks, validate_tasks, role_vars):
    assert _check(resolve_tasks, validate_tasks, role_vars,
                  [{**NFS_OK, "provision": False}]) == []


# ── Required source fields per kind ──────────────────────────────────────
@pytest.mark.parametrize("missing", ["server", "export"])
def test_nfs_requires_server_and_export(
        resolve_tasks, validate_tasks, role_vars, missing):
    volume = {k: v for k, v in NFS_OK.items() if k != missing}
    failures = _check(resolve_tasks, validate_tasks, role_vars, [volume])
    assert any("needs both server and export" in f for f in failures)


@pytest.mark.parametrize("missing", ["server", "share"])
def test_cifs_requires_server_and_share(
        resolve_tasks, validate_tasks, role_vars, missing):
    volume = {k: v for k, v in CIFS_OK.items() if k != missing}
    failures = _check(resolve_tasks, validate_tasks, role_vars, [volume])
    assert any("needs both server and share" in f for f in failures)


def test_rejects_a_whitespace_only_server(resolve_tasks, validate_tasks, role_vars):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{**NFS_OK, "server": "   "}])
    assert any("needs both server and export" in f for f in failures)


# ── Network volumes must be mounted ──────────────────────────────────────
def test_requires_a_mount_point_on_network_volumes(
        resolve_tasks, validate_tasks, role_vars):
    volume = {k: v for k, v in NFS_OK.items() if k != "mount"}
    failures = _check(resolve_tasks, validate_tasks, role_vars, [volume])
    assert any("declares no mount point" in f for f in failures)


def test_still_allows_an_unmounted_local_volume(
        resolve_tasks, validate_tasks, role_vars):
    """mount: '' means 'manage the block stack, do not mount' — still legal."""
    assert _check(resolve_tasks, validate_tasks, role_vars,
                  [{"name": "a", "vg": "vg_x", "lv": "lv_x", "mount": ""}]) == []


# ── Supported kinds only ─────────────────────────────────────────────────
def test_rejects_an_unknown_kind(resolve_tasks, validate_tasks, role_vars):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{"name": "a", "kind": "iscsi", "mount": "/x"}])
    assert any("Unsupported storage kind(s)" in f for f in failures)


def test_treats_a_kindless_entry_as_local(resolve_tasks, validate_tasks, role_vars):
    facts = _normalise(resolve_tasks, role_vars, [LOCAL_OK])
    assert [v["name"] for v in facts["_storage_local_volumes"]] == ["opt"]
    assert facts["_storage_net_volumes"] == []


def test_keeps_an_unknown_kind_out_of_both_lists(resolve_tasks, role_vars):
    """An unrecognised kind must not silently land in the block path."""
    facts = _normalise(resolve_tasks, role_vars,
                       [{"name": "a", "kind": "iscsi", "mount": "/x"}])
    assert facts["_storage_local_volumes"] == []
    assert facts["_storage_net_volumes"] == []


# ── Network mount points may not be OS directories ───────────────────────
@pytest.mark.parametrize("mount", ["/", "/boot", "/etc", "/usr", "/var", "/home", "/var/"])
def test_rejects_protected_network_mount_points(
        resolve_tasks, validate_tasks, role_vars, mount):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{**NFS_OK, "mount": mount}])
    assert any("must not be one of" in f for f in failures)


def test_rejects_a_relative_network_mount_point(
        resolve_tasks, validate_tasks, role_vars):
    failures = _check(resolve_tasks, validate_tasks, role_vars,
                      [{**NFS_OK, "mount": "data/sub"}])
    assert any("must be an absolute path" in f for f in failures)


@pytest.mark.parametrize("mount", ["/var/lib/dirsrv", "/var", "/home"])
def test_does_not_constrain_local_mount_points(
        resolve_tasks, validate_tasks, role_vars, mount):
    """Shipped estate profiles mount /var and /var/lib/dirsrv locally."""
    assert _check(resolve_tasks, validate_tasks, role_vars,
                  [{"name": "a", "vg": "vg_x", "lv": "lv_x", "mount": mount}]) == []


# ── Nested network mounts ────────────────────────────────────────────────
def test_rejects_a_network_mount_nested_in_a_local_mount(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [{"name": "a", "mount": "/data"}, {**NFS_OK, "mount": "/data/sub"}]
    failures = _check(resolve_tasks, validate_tasks, role_vars, volumes)
    assert any("nested inside declared mount" in f for f in failures)


def test_allows_nesting_when_explicitly_permitted(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [{"name": "a", "mount": "/data"}, {**NFS_OK, "mount": "/data/sub"}]
    assert _check(resolve_tasks, validate_tasks, role_vars, volumes,
                  storage_allow_nested_mounts=True) == []


def test_does_not_treat_a_shared_prefix_as_nesting(
        resolve_tasks, validate_tasks, role_vars):
    volumes = [{"name": "a", "mount": "/data"}, {**NFS_OK, "mount": "/database"}]
    assert _check(resolve_tasks, validate_tasks, role_vars, volumes) == []


# ── Gating ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind,flag", [("nfs", "storage_manage_nfs"),
                                       ("cifs", "storage_manage_cifs")])
def test_a_declared_network_volume_is_inert_while_its_flag_is_false(
        resolve_tasks, role_vars, kind, flag):
    volume = NFS_OK if kind == "nfs" else CIFS_OK
    facts = _normalise(resolve_tasks, role_vars, [LOCAL_OK, volume], **{flag: False})
    assert [v["name"] for v in facts["_storage_net_volumes"]] == [volume["name"]]
    assert facts["_storage_net_active"] == []


def test_disabling_a_kind_does_not_fail_validation(
        resolve_tasks, validate_tasks, role_vars):
    assert _check(resolve_tasks, validate_tasks, role_vars, [NFS_OK],
                  storage_manage_nfs=False) == []


# ── Source strings ───────────────────────────────────────────────────────
def test_network_source_strings_follow_the_mount_syntax_of_each_kind(
        resolve_tasks, role_vars):
    facts = _normalise(resolve_tasks, role_vars, [NFS_OK, CIFS_OK])
    sources = {v["name"]: v["_src"] for v in facts["_storage_net_volumes"]}
    assert sources["app-data"] == "fileserver-01.example.internal:/export/app-data"
    assert sources["media-archive"] == "//fileserver-02.example.internal/archive"


def test_network_volumes_default_fstype_per_kind(resolve_tasks, role_vars):
    facts = _normalise(resolve_tasks, role_vars, [NFS_OK, CIFS_OK])
    fstypes = {v["name"]: v["fstype"] for v in facts["_storage_net_volumes"]}
    assert fstypes == {"app-data": "nfs4", "media-archive": "cifs"}


def test_a_declared_fstype_wins_over_the_per_kind_default(resolve_tasks, role_vars):
    facts = _normalise(resolve_tasks, role_vars, [{**NFS_OK, "fstype": "nfs"}])
    assert facts["_storage_net_volumes"][0]["fstype"] == "nfs"


# ── The local block path must be byte-for-byte what it always was ────────
def _estate_profiles() -> list[tuple[str, list[dict[str, Any]]]]:
    profiles = _load(PROFILES)["storage_profiles"]
    return sorted(profiles.items())


@pytest.mark.parametrize("profile,volumes", _estate_profiles())
def test_estate_profiles_normalise_to_defaults_combined_with_the_entry(
        resolve_tasks, role_vars, profile, volumes):
    """The local normalisation is still `defaults | combine(entry)`.

    Every shipped profile is local-only, so `_storage_local_volumes` must equal
    the old single-list result. `kind` is the one added key (always 'local').
    """
    facts = _normalise(resolve_tasks, role_vars, volumes)
    assert facts["_storage_net_volumes"] == []
    assert facts["_storage_volumes"] == facts["_storage_local_volumes"]
    assert len(facts["_storage_local_volumes"]) == len(volumes)

    defaults = facts["_storage_volume_defaults"]
    for declared, normalised in zip(volumes, facts["_storage_local_volumes"]):
        assert normalised["kind"] == "local"
        expected = _render({**defaults, **declared}, facts)
        assert normalised == expected


def test_estate_profiles_remain_local_only(resolve_tasks, role_vars):
    """A network volume appearing in the estate catalogue must be deliberate."""
    for _profile, volumes in _estate_profiles():
        for volume in volumes:
            assert volume.get("kind", "local") == "local"


def test_block_phases_consume_the_local_list_only(main_tasks):
    discover = (TASKS / "discover.yml").read_text()
    assert "_storage_local_volumes" in discover
    assert "loop: \"{{ _storage_volumes }}\"" not in discover
    assert "_storage_local_input" in discover

    for phase in ("grow.yml", "provision.yml", "mount.yml", "selinux.yml"):
        body = (TASKS / phase).read_text()
        assert "_storage_resolved" in body
        assert "_storage_net" not in body


@pytest.mark.parametrize("phase,expected", [
    ("Discover devices", "_storage_local_declared"),
    ("Grow existing volumes", "_storage_local_declared"),
    ("Provision new volumes", "_storage_local_declared"),
    ("Mount volumes", "_storage_local_declared"),
    ("Apply SELinux file contexts", "_storage_local_declared"),
])
def test_block_phases_are_gated_on_local_volumes_only(main_tasks, phase, expected):
    task = _find(main_tasks, phase)
    assert expected in " ".join(str(c) for c in _as_list(task["when"]))


def test_network_mount_phase_is_gated_on_enabled_network_volumes(main_tasks):
    task = _find(main_tasks, "Mount network volumes")
    when = " ".join(str(c) for c in _as_list(task["when"]))
    assert "storage_manage_fstab" in when
    assert "_storage_net_active | length > 0" in when


# ── main.yml dispatches; resolve + validate front every phase ────────────
PHASE_IMPORTS = ("packages.yml", "discover.yml", "grow.yml", "provision.yml",
                 "mount.yml", "mount_net.yml", "selinux.yml")

PRELUDE_IMPORTS = ("resolve.yml", "validate.yml")


def _imports(main_tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every import_tasks in main.yml, keyed by the file it imports."""
    return {task["ansible.builtin.import_tasks"]: task
            for task in main_tasks if "ansible.builtin.import_tasks" in task}


def test_resolve_and_validate_precede_every_acting_phase(main_tasks):
    names = [t.get("name", "") for t in main_tasks]
    imports = _imports(main_tasks)
    for prelude in PRELUDE_IMPORTS:
        at = names.index(imports[prelude]["name"])
        for phase in PHASE_IMPORTS:
            assert at < names.index(imports[phase]["name"])


def test_the_prelude_carries_every_phase_tag(main_tasks):
    """The union on resolve/validate is hand-maintained — a phase tag missing
    from it runs that phase against unnormalised, unvalidated facts.
    """
    imports = _imports(main_tasks)
    phase_tags = set()
    for phase in PHASE_IMPORTS:
        phase_tags |= set(imports[phase]["tags"])

    for prelude in PRELUDE_IMPORTS:
        missing = phase_tags - set(imports[prelude]["tags"])
        assert not missing, f"{prelude} is not tagged {sorted(missing)}"

    # The skip notice is the only task outside the gate block, so it needs the
    # same union to report a disabled role under any phase tag.
    skip = _find(main_tasks, "Skip storage (storage_enabled is false)")
    assert not phase_tags - set(skip["tags"])


def test_unknown_storage_profile_is_asserted_before_resolve(resolve_tasks):
    """Typo'd profile must fail with a named message, not a Jinja attribute dump."""
    task = _find(resolve_tasks, "Assert storage_profile is a known profile")
    assert ASSERT in task
    assert "storage_profile in storage_profiles" in str(task[ASSERT]["that"])
    assert "Known profiles" in task[ASSERT]["fail_msg"]
    names = [t.get("name", "") for t in resolve_tasks]
    assert names.index(task["name"]) < names.index(
        _find(resolve_tasks, "Resolve effective volume list")["name"])


def test_grow_skips_xfs_when_unmounted_and_uses_storage_fs_grow():
    body = (TASKS / "grow_one.yml").read_text()
    assert "findmnt" in body
    assert "is not currently a mount point" in body
    assert "storage_fs_grow[vol.fstype]" in body
    assert "storage_fs_grow_changed_token" in body
    assert 'command: "xfs_growfs' not in body
    assert 'command: "resize2fs' not in body


def test_storage_fs_grow_vars_cover_xfs_and_ext4(role_vars):
    assert "xfs" in role_vars["storage_fs_grow"]
    assert "ext4" in role_vars["storage_fs_grow"]
    assert "{mount}" in role_vars["storage_fs_grow"]["xfs"]
    assert "{dev}" in role_vars["storage_fs_grow"]["ext4"]
    # xfs: positive change token; ext4: noop token (Nothing to do)
    assert "xfs" in role_vars["storage_fs_grow_changed_token"]
    assert "ext4" in role_vars["storage_fs_grow_noop_token"]
    assert "Nothing to do" in role_vars["storage_fs_grow_noop_token"]["ext4"]


def test_fresh_guard_requires_block_device_and_clean_blkid():
    body = (TASKS / "provision_one.yml").read_text()
    assert "test, -b," in body
    assert "_p_sig.stderr" in body
    assert "never treated as empty" in body or "could not cleanly probe" in body


def test_root_disk_discovery_fails_closed_when_block_backed():
    body = (TASKS / "discover.yml").read_text()
    assert "Assert root-disk discovery identified at least one disk when / is block-backed" in body
    assert "_storage_root_disks | length > 0" in body
    assert "ROOT_KIND=block" in body
    assert "ROOT_KIND=virtual" in body
    # Empty exclude list is only fatal for block-backed roots (containers stay virtual).
    assert "_storage_root_kind == 'block'" in body


def test_root_protection_degrades_instead_of_aborting_on_a_non_block_root():
    """overlayfs/ZFS roots must not hard-fail — they lose guards, loudly."""
    body = (TASKS / "discover.yml").read_text()
    assert "_storage_root_protection" in body
    notice = _find(_load(TASKS / "discover.yml"),
                   "Warn that root-disk protection is unavailable")
    # Loud by default: an operator must see the degradation without -v.
    assert "storage_debug" not in str(notice.get("when"))
    assert "WARNING" in notice["ansible.builtin.debug"]["msg"]


def _implicit_refusal_task() -> dict[str, Any]:
    return _find(_load(TASKS / "discover.yml"),
                 "Refuse implicit disk selection while root protection is unavailable")


def test_implicit_selection_is_refused_only_when_provisioning_is_armed():
    task = _implicit_refusal_task()
    when = str(task["when"])
    assert "_storage_root_protection" in when
    # Read-only work (discover/grow/mount) still runs on an unnameable root.
    assert "storage_provision" in when


@pytest.mark.parametrize("selector,refused", [
    ("", True),                    # auto (default disk: "")
    ("auto", True),
    ("by-size:50G", True),
    ("by-serial:ZJV01KJ2", True),
    ("by-wwn:0x5000c500", True),
    ("/dev/sdb", False),           # operator named the device — the escape hatch
    ("/dev/loop0", False),
])
def test_which_selectors_the_unprotected_root_guard_refuses(selector, refused):
    """Replays the shipped expression, not a re-implementation of it."""
    expr = _implicit_refusal_task()["vars"]["_unprotected_implicit"]
    volumes = [{"name": "data1", "disk": selector, "provision": True}]
    result = _render(expr, {"_storage_local_volumes": volumes})
    assert (result == ["data1"]) is refused


def test_the_unprotected_root_guard_reads_a_string_provision_flag():
    """`provision: "no"` from inventory must not slip past as truthy."""
    expr = _implicit_refusal_task()["vars"]["_unprotected_implicit"]
    volumes = [{"name": "off", "disk": "auto", "provision": "no"},
               {"name": "on", "disk": "auto", "provision": "yes"}]
    assert _render(expr, {"_storage_local_volumes": volumes}) == ["on"]


def test_explicit_dev_selectors_respect_consumed_disks():
    body = (TASKS / "discover.yml").read_text()
    assert "_match_dev" in body
    assert "_sel not in _consumed" in body
    assert "Assert no two local volumes resolve to the same disk" in body


def test_mount_applies_owner_after_mount():
    tasks = _load(TASKS / "mount_one.yml")
    manage = _find(tasks, "manage mount + fstab")
    block = manage["block"]
    names = [t.get("name", "") for t in block]
    mount_at = next(i for i, n in enumerate(names) if "mount " in n and "by UUID" in n)
    owner_at = next(i for i, n in enumerate(names) if "owner/group/mode on the mounted" in n)
    assert owner_at > mount_at


# ── CIFS credential custody ──────────────────────────────────────────────
def test_cifs_credentials_are_written_root_only_and_never_logged():
    tasks = _load(TASKS / "mount_net_one.yml")
    task = _find(tasks, "write the CIFS credentials file")
    assert task["no_log"] is True
    copy = task["ansible.builtin.copy"]
    assert copy["mode"] == "0600"
    assert copy["owner"] == "root"
    assert copy["group"] == "root"


def test_the_cifs_password_never_reaches_fstab():
    body = (TASKS / "mount_net_one.yml").read_text()
    mount = _find(_load(TASKS / "mount_net_one.yml"), "| mount ")
    assert "credentials_password" not in str(mount)
    assert "credentials=" in body


def test_network_facts_carrying_credentials_are_not_logged(resolve_tasks):
    for needle in ("Normalise network volumes", "Combine the effective volume list",
                   "Select the network volumes whose kind is enabled"):
        assert _find(resolve_tasks, needle).get("no_log") is True


def test_netdev_and_nofail_are_forced_onto_every_network_mount(role_vars):
    assert role_vars["storage_net_forced_opts"] == ["_netdev", "nofail"]
    body = (TASKS / "mount_net_one.yml").read_text()
    assert "storage_net_forced_opts" in body
    assert "| unique | sort | join(',') }}" in body


@pytest.mark.parametrize("declared,expected", [
    ("hard,noatime", "_netdev,hard,noatime,nofail"),
    ("nofail,hard", "_netdev,hard,nofail"),
    ("", "_netdev,nofail"),
    ("b,a,c", "_netdev,a,b,c,nofail"),
])
def test_nfs_option_strings_are_deterministic(role_vars, declared, expected):
    """Unsorted options make ansible.posix.mount rewrite fstab on every run."""
    expr = _find(_load(TASKS / "mount_net_one.yml"),
                 "build the mount option string")[SET_FACT]["_n_opts"]
    variables = {**role_vars, "vol": {"kind": "nfs", "opts": declared}, "_n_cred": ""}
    assert _render(expr, variables).strip() == expected


def test_cifs_option_strings_inject_the_credentials_path_and_drop_any_supplied_one(role_vars):
    expr = _find(_load(TASKS / "mount_net_one.yml"),
                 "build the mount option string")[SET_FACT]["_n_opts"]
    variables = {
        **role_vars,
        "vol": {"kind": "cifs", "opts": "vers=3.1.1,credentials=/tmp/attacker.cred"},
        "_n_cred": "/etc/cifs-credentials/media-archive.cred",
    }
    result = _render(expr, variables).strip()
    assert "credentials=/etc/cifs-credentials/media-archive.cred" in result
    assert "/tmp/attacker.cred" not in result
    assert "_netdev" in result and "nofail" in result


# ── Packages ─────────────────────────────────────────────────────────────
def test_network_client_packages_are_declared_for_every_supported_family(role_vars):
    for kind in ("nfs", "cifs"):
        for family in ("RedHat", "Debian", "Suse"):
            assert role_vars["storage_packages"][kind][family]


def test_network_client_packages_stay_out_of_the_base_package_list(role_vars):
    for family in ("RedHat", "Debian", "Suse"):
        base = role_vars["storage_packages"][family]
        assert not any(pkg.startswith(("nfs-", "cifs-")) for pkg in base)


def test_base_packages_install_only_when_a_local_volume_is_declared():
    task = _find(_load(TASKS / "packages.yml"), "Install storage prerequisite packages")
    assert "_storage_local_declared" in str(task["when"])
