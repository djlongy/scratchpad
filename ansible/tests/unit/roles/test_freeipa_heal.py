"""freeipa_server self-heal phase (tasks/heal.yml) — structural regression tests.

The heal phase repairs a CONFIGURED server whose Apache↔Dogtag reverse-proxy
config (ipa-pki-proxy.conf) is missing/corrupt/wrong-secret, by re-rendering
FreeIPA's OWN template with the real deployed values (NOT the generic Red Hat
template, and NOT ipa-server-upgrade — which was proven not to regenerate the
file). It also fail-fasts on aborted half-installs. These tests pin the
load-bearing properties: ordering (after preflight, before install), gating, the
render mechanism, read-only probes, secret hygiene, and the stopped-but-intact
exclusion.
"""
from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[4]
ROLE = REPO_ROOT / "ansible" / "roles" / "freeipa_server"
HEAL_TASKS = ROLE / "tasks" / "heal.yml"
MAIN_TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
ROLE_VARS = ROLE / "vars" / "main.yml"
ARG_SPECS = ROLE / "meta" / "argument_specs.yml"
RENDERER = ROLE / "files" / "ipa_pki_proxy_render.py"


def heal_text() -> str:
    return HEAL_TASKS.read_text()


def main_text() -> str:
    return MAIN_TASKS.read_text()


def flatten(tasks: list) -> list:
    flat = list(tasks)
    for task in tasks:
        flat.extend(task.get("block", []))
    return flat


def test_heal_is_gated_defaulted_on_and_part_of_a_no_tags_run() -> None:
    assert "freeipa_server_heal_enabled: true" in DEFAULTS.read_text()
    assert "freeipa_server_heal_enabled" in ARG_SPECS.read_text()

    heal_import = next(
        t for t in yaml.safe_load(main_text())
        if t.get("ansible.builtin.import_tasks") == "heal.yml"
    )
    assert heal_import["when"] == "freeipa_server_heal_enabled | bool"
    # Refinement tags only — a plain (no-tags) run and --tags install both heal.
    assert sorted(heal_import["tags"]) == ["heal", "install"]
    assert "never" not in heal_import["tags"]


def test_heal_runs_after_preflight_and_before_the_upstream_install() -> None:
    text = main_text()
    assert (
        text.index("preflight.yml")
        < text.index("heal.yml")
        < text.index("install.yml")
    )


def test_heal_re_renders_freeipa_template_not_upstream_upgrade_or_rh_template() -> None:
    text = heal_text()

    # The repair renders FreeIPA's OWN template via the shipped script; it must
    # NOT lean on ipa-server-upgrade (proven not to regenerate the file) and must
    # NOT ship/patch a generic template of its own (wrong AJP secret).
    assert "ipa_pki_proxy_render.py" in text
    # ipa-server-upgrade is discussed in the header comment (why it's NOT used)
    # but must never be INVOKED as a command.
    assert "command: ipa-server-upgrade" not in text
    assert "builtin.command: ipa-server-upgrade" not in text
    assert "ansible.builtin.template" not in text
    assert not (ROLE / "templates" / "ipa-pki-proxy.conf.j2").exists()

    renderer = RENDERER.read_text()
    assert "/usr/share/ipa/ipa-pki-proxy.conf.template" in renderer
    assert "/etc/pki/pki-tomcat/server.xml" in renderer          # real AJP secret source
    assert "/etc/httpd/conf.d/ipa-pki-proxy.conf" in renderer     # output


def test_secret_bearing_tasks_and_renderer_keep_the_ajp_secret_off_logs() -> None:
    tasks = flatten(yaml.safe_load(heal_text()))
    # Every task that reads/writes the AJP secret must be no_log.
    for task in tasks:
        name = task.get("name", "")
        touches_secret = (
            "AJP secret" in name
            or "Re-render" in name
            or "carries Tomcat's AJP secret" in name
        )
        if touches_secret:
            assert task.get("no_log") is True, name

    # The renderer must never print the secret or pass it as an argument.
    renderer = RENDERER.read_text()
    assert "print(secret" not in renderer
    assert "--stdout-sha" in renderer  # dry-run prints only a hash, not content


def test_heal_probes_are_read_only_and_check_mode_safe() -> None:
    tasks = flatten(yaml.safe_load(heal_text()))
    probes = [
        t for t in tasks
        if "retries" not in t  # the post-heal verify sits in a not-check-mode block
        and (("ipactl status" in str(t.get("ansible.builtin.command", "")))
             or ("httpd -t" in str(t.get("ansible.builtin.command", "")))
             or ("grep" in str(t.get("ansible.builtin.shell", ""))))
    ]
    read_only_probes = [t for t in probes if t.get("check_mode") is False]
    assert len(read_only_probes) >= 3
    for probe in read_only_probes:
        assert probe.get("changed_when") is False, probe["name"]
        assert probe.get("failed_when") is False, probe["name"]


def test_heal_never_fires_on_a_cleanly_stopped_but_intact_server() -> None:
    text = heal_text()

    # ipactl rc alone must not trigger a heal — only a PARTIAL outage (LDAP up,
    # httpd/pki down) does; everything-stopped is the service-start path's job.
    assert "Directory Service: RUNNING" in text
    assert "(httpd|pki-tomcatd) Service: STOPPED" in text


def test_heal_pki_proxy_absence_only_counts_when_a_ca_is_deployed() -> None:
    text = heal_text()

    assert "not _freeipa_heal_pki_proxy.stat.exists" in text
    assert "_freeipa_heal_ca.stat.exists" in text
    assert "freeipa_server_heal_pki_server_xml" in ROLE_VARS.read_text()
    assert "freeipa_server_heal_pki_proxy_conf" in ROLE_VARS.read_text()


def test_aborted_half_install_fails_fast_with_the_cleanup_command() -> None:
    text = heal_text()

    assert "/var/lib/ipa/sysrestore" in text
    assert "ipa-server-install --uninstall" in text
    # Guard applies only to UNCONFIGURED hosts (default.conf absent).
    assert text.count("when: not _freeipa_heal_configured.stat.exists") == 2


def test_heal_backs_up_the_broken_file_verifies_and_gives_actionable_failure() -> None:
    text = heal_text()

    assert ".broken-" in text                       # forensic backup of the old file
    assert "_freeipa_heal_verify.rc == 0" in text    # services verified RUNNING
    assert "is not search('STOPPED')" in text
    assert "freeipa_server_heal_verify_retries" in text
    assert "journalctl -u httpd" in text             # actionable failure guidance
