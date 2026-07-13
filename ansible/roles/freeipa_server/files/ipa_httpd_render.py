#!/usr/bin/python3
"""Re-render FreeIPA's installer-generated Apache config files from its OWN templates.

A CONFIGURED FreeIPA server can lose the httpd config files the *installer*
generates (they are RPM ``%ghost`` entries — the package owns the paths but ships
them empty; the installer renders them from ``/usr/share/ipa/*.template``). Neither
``dnf reinstall`` (restores only the templates, not the rendered output) nor
``ipa-server-upgrade`` regenerates them from scratch — upgrade aborts
``/etc/httpd/conf.d/ipa.conf not found`` because its HTTPInstance step reads the
existing file rather than creating it (empirically confirmed on IPA 4.13 / EL9).

This script reproduces EXACTLY what ``ipaserver.install.httpinstance`` does, by
reusing FreeIPA's OWN ``ipautil.template_file`` renderer and building the same
``sub_dict`` from the live installed constants (``ipaplatform.paths`` /
``ipaplatform.constants``) plus REALM/FQDN/DOMAIN read from ``/etc/ipa/default.conf``.
Because it pulls constants from the installed FreeIPA rather than hardcoding them,
it stays correct across versions. Proven sha256-identical to the installer's output.

Renders (each only if MISSING, so a healthy file is never rewritten):
  * /etc/httpd/conf.d/ipa.conf
  * /etc/httpd/conf.d/ipa-rewrite.conf          (Included by ssl.conf; httpd fails without it)
  * /etc/ipa/kdcproxy/ipa-kdc-proxy.conf        (+ the conf.d symlink to it)
It does NOT render ipa-pki-proxy.conf (that carries the Dogtag AJP secret and has
its own dedicated renderer) nor nss.conf (mod_nss is legacy — absent by design on
EL9, package-owned when present; never fabricated here).

``--stdout-sha`` prints a sha256 per target and writes nothing (dry-run/verify).
Exit status is non-zero on any render/import failure so the caller fails loud.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

from ipaplatform.paths import paths
from ipaplatform.constants import constants
from ipapython import ipautil

DEFAULT_CONF = "/etc/ipa/default.conf"
IPA_CONF = "/etc/httpd/conf.d/ipa.conf"
IPA_REWRITE_CONF = "/etc/httpd/conf.d/ipa-rewrite.conf"
KDCPROXY_TARGET = "/etc/ipa/kdcproxy/ipa-kdc-proxy.conf"
KDCPROXY_SYMLINK = "/etc/httpd/conf.d/ipa-kdc-proxy.conf"


def _defconf(key: str) -> "str | None":
    with open(DEFAULT_CONF, encoding="utf-8") as handle:
        match = re.search(r"^%s\s*=\s*(\S+)" % key, handle.read(), re.MULTILINE)
    return match.group(1) if match else None


def _sub_dict() -> dict:
    """The HTTPInstance sub_dict, built from live constants + default.conf.

    Mirrors ipaserver.install.httpinstance.HTTPInstance.sub_dict exactly (same
    keys, same value sources) so the render is byte-identical to the installer's.
    """
    return {
        "REALM": _defconf("realm") or _defconf("realm_name"),
        "FQDN": _defconf("host"),
        "DOMAIN": _defconf("domain"),
        # auto_redirect defaults ON at install (--no-ui-redirect off) → '' enables
        # the http→https RewriteRule; this is the standard server layout.
        "AUTOREDIR": "",
        "CRL_PUBLISH_PATH": paths.PKI_CA_PUBLISH_DIR,
        "FONTS_DIR": paths.FONTS_DIR,
        "FONTS_OPENSANS_DIR": paths.FONTS_OPENSANS_DIR,
        "FONTS_FONTAWESOME_DIR": paths.FONTS_FONTAWESOME_DIR,
        "GSSAPI_SESSION_KEY": paths.GSSAPI_SESSION_KEY,
        "IPA_CUSTODIA_SOCKET": paths.IPA_CUSTODIA_SOCKET,
        "IPA_CCACHES": paths.IPA_CCACHES,
        "WSGI_PREFIX_DIR": paths.WSGI_PREFIX_DIR,
        "WSGI_PROCESSES": constants.WSGI_PROCESSES,
    }


def _render(template_name: str, sub: dict) -> str:
    tmpl = os.path.join(paths.USR_SHARE_IPA_DIR, template_name)
    return ipautil.template_file(tmpl, sub)


def _restorecon(path: str) -> None:
    """Reset the SELinux context so httpd can read the freshly written file."""
    if os.path.exists(paths.RESTORECON):
        # NOSONAR - S603: fixed absolute argv (paths.RESTORECON), shell=False, no user input.
        subprocess.run([paths.RESTORECON, path], check=False)  # NOSONAR


def main() -> None:
    sub = _sub_dict()
    plan = [
        (IPA_CONF, "ipa.conf.template", sub),
        (IPA_REWRITE_CONF, "ipa-rewrite.conf.template", sub),
        (KDCPROXY_TARGET, "ipa-kdc-proxy.conf.template",
         {"KDCPROXY_CONFIG": paths.KDCPROXY_CONFIG}),
    ]
    dry_run = "--stdout-sha" in sys.argv
    for target, template_name, sub_dict in plan:
        rendered = _render(template_name, sub_dict)
        if dry_run:
            sys.stdout.write("%s %s\n" % (
                hashlib.sha256(rendered.encode()).hexdigest(), target))
            continue
        if os.path.exists(target):
            continue  # never rewrite a file that is already present
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o755)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        _restorecon(target)
    # Re-create the conf.d → kdcproxy symlink if missing (the installer links it).
    if not dry_run and not os.path.islink(KDCPROXY_SYMLINK) \
            and not os.path.exists(KDCPROXY_SYMLINK):
        os.symlink(KDCPROXY_TARGET, KDCPROXY_SYMLINK)
        _restorecon(KDCPROXY_SYMLINK)


if __name__ == "__main__":
    main()
