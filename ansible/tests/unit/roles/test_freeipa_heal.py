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
HTTPD_RENDERER = ROLE / "files" / "ipa_httpd_render.py"


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


# ── Round 2: generalised full-httpd-config-loss recovery ─────────────────────

def test_vars_define_the_full_httpd_file_set_and_ca_cert_paths() -> None:
    text = ROLE_VARS.read_text()
    for path in (
        "/etc/httpd/conf.d/ipa.conf",
        "/etc/httpd/conf.d/ipa-rewrite.conf",
        "/etc/ipa/kdcproxy/ipa-kdc-proxy.conf",
        "/usr/share/ipa/html/ca.crt",
        "/etc/ipa/ca.crt",
    ):
        assert path in text, path
    # The probe iterates a declared list of the installer-generated httpd files.
    assert "freeipa_server_heal_ipa_httpd_files" in text


def test_heal_recovers_full_httpd_config_loss_via_freeipa_templates() -> None:
    text = heal_text()
    # The generalised renderer is invoked (the 3 non-secret confs), plus the
    # existing pki-proxy renderer for the secret-bearing file.
    assert "ipa_httpd_render.py" in text
    assert "ipa_pki_proxy_render.py" in text
    # The served CA cert is republished from the on-disk source.
    assert "freeipa_server_heal_ca_cert_served" in text
    assert "freeipa_server_heal_ca_cert_source" in text
    # Defect flags for the two new loss classes.
    assert "_freeipa_heal_std_confs_missing" in text
    assert "_freeipa_heal_ca_cert_missing" in text


def test_std_conf_render_only_fires_when_a_file_is_missing() -> None:
    """Round-1 (only ipa-pki-proxy.conf broken) must NOT trigger the std render."""
    tasks = flatten(yaml.safe_load(heal_text()))
    std_render = next(
        t for t in tasks
        if "ipa_httpd_render.py" in str(t.get("ansible.builtin.script", ""))
    )
    assert std_render["when"] == "_freeipa_heal_std_confs_missing | default(false)"


def test_pki_proxy_render_is_gated_on_the_pki_proxy_defect() -> None:
    """Full-loss and round-1 both render pki-proxy; a std-only loss need not."""
    tasks = flatten(yaml.safe_load(heal_text()))
    pki_render = next(
        t for t in tasks
        if "ipa_pki_proxy_render.py" in str(t.get("ansible.builtin.script", ""))
    )
    assert pki_render["when"] == "_freeipa_heal_pki_proxy_broken | default(false)"


def test_ca_cert_republish_is_guarded_and_fails_loud_if_source_gone() -> None:
    text = heal_text()
    # The republish only runs when the served cert is missing.
    tasks = flatten(yaml.safe_load(text))
    republish = next(
        t for t in tasks
        if t.get("ansible.builtin.copy", {}).get("dest", "")
        == "{{ freeipa_server_heal_ca_cert_served }}"
    )
    assert "_freeipa_heal_ca_cert_missing" in str(republish["when"])
    # If the SOURCE is also gone, that is deeper than a file loss → ipa-restore.
    assert "ipa-restore" in text


def test_nss_conf_is_reported_never_rendered() -> None:
    text = heal_text()
    # nss.conf is probed for diagnostics only …
    assert "freeipa_server_heal_nss_conf" in text
    assert "never rendered" in text or "diagnostic only" in text.lower()
    # … and there is NO renderer/template that would fabricate it.
    assert "nss.conf.template" not in text
    assert "nss.conf.template" not in HTTPD_RENDERER.read_text()


def test_httpd_renderer_reuses_freeipa_own_templating_and_is_byte_safe() -> None:
    src = HTTPD_RENDERER.read_text()
    # Reuses FreeIPA's OWN renderer + live constants — not a hand-copied template.
    assert "ipautil.template_file" in src
    assert "from ipaplatform.paths import paths" in src
    assert "from ipaplatform.constants import constants" in src
    # Renders exactly the three installer-generated confs.
    for tmpl in ("ipa.conf.template", "ipa-rewrite.conf.template",
                 "ipa-kdc-proxy.conf.template"):
        assert tmpl in src
    # Dry-run prints only a sha (writes nothing); never rewrites a present file;
    # re-creates the kdc-proxy symlink.
    assert "--stdout-sha" in src
    assert "if os.path.exists(target):" in src and "continue" in src
    assert "os.symlink" in src


def test_post_heal_verify_asserts_every_file_returned() -> None:
    text = heal_text()
    # The end-state assert re-stats the whole set and requires none missing.
    assert "_freeipa_heal_files_after" in text
    assert "rejectattr('stat.exists')" in text
    # Fail-loud guidance for the unrecoverable (data-damage) case.
    assert "ipa-restore" in text
    assert "DATA-level damage" in text or "data-level" in text.lower()
