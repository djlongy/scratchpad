#!/usr/bin/python3
"""Regenerate /etc/httpd/conf.d/ipa-pki-proxy.conf from FreeIPA's OWN template.

FreeIPA installs the Apache↔Dogtag reverse-proxy config by rendering
``/usr/share/ipa/ipa-pki-proxy.conf.template`` with deployment-specific values
(the AJP secret and port from Tomcat's ``server.xml``, the server FQDN, and the
CRL-generation marker). Neither ``ipa-server-upgrade`` nor ``ipactl`` regenerate
this file once it is missing/corrupt, so this script reproduces exactly what the
installer's ``ipaserver.install.httpinstance`` renderer does.

The AJP secret is read from a file and written straight into the output file — it
is never printed to stdout nor passed as a process argument, so it stays off the
Ansible transcript (the task also runs ``no_log``). Run with ``--stdout-sha`` to
print only the sha256 of the rendered content (dry-run, writes nothing).
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import sys

TEMPLATE = "/usr/share/ipa/ipa-pki-proxy.conf.template"
SERVER_XML = "/etc/pki/pki-tomcat/server.xml"
CS_CFG = "/etc/pki/pki-tomcat/ca/CS.cfg"
DEFAULT_CONF = "/etc/ipa/default.conf"
OUTPUT = "/etc/httpd/conf.d/ipa-pki-proxy.conf"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _server_fqdn() -> str:
    match = re.search(r"^host\s*=\s*(\S+)", _read(DEFAULT_CONF), re.MULTILINE)
    return match.group(1) if match else socket.getfqdn()


def _ajp_port_and_secret() -> "tuple[str, str]":
    """Return (port, secret) from the first AJP Connector that carries a secret."""
    xml = _read(SERVER_XML)
    for tag_match in re.finditer(r"<Connector\b[^>]*>", xml, re.DOTALL):
        tag = tag_match.group(0)
        if "AJP" not in tag:
            continue
        port = re.search(r'port="(\d+)"', tag)
        secret = re.search(r'(?:requiredSecret|secret)="([^"]+)"', tag)
        if port and secret:
            return port.group(1), secret.group(1)
    raise SystemExit("no AJP connector with a secret found in " + SERVER_XML)


def _clone_marker() -> str:
    """'#' on a CRL generator (comment out the redirect), '' otherwise.

    The template's RewriteRule redirects the CRL fetch to the CA; it must be
    enabled only on servers that do NOT generate the CRL themselves.
    """
    if not os.path.exists(CS_CFG):
        return "#"
    generates = re.search(
        r"ca\.crl\.MasterCRL\.enableCRLUpdates\s*=\s*(\w+)", _read(CS_CFG))
    return "#" if generates and generates.group(1).lower() == "true" else ""


def _render() -> str:
    port, secret = _ajp_port_and_secret()
    return (_read(TEMPLATE)
            .replace("$DOGTAG_AJP_SECRET", "secret=" + secret)
            .replace("$DOGTAG_PORT", port)
            .replace("$FQDN", _server_fqdn())
            .replace("${CLONE}", _clone_marker()))


def main() -> None:
    rendered = _render()
    if "--stdout-sha" in sys.argv:
        sys.stdout.write(hashlib.sha256(rendered.encode()).hexdigest() + "\n")
        return
    # Mode 0644 reproduces exactly what the FreeIPA installer writes for this
    # file (a self-heal must produce the same artifact as a fresh install, not a
    # subtly different one). The embedded AJP secret only authorises the
    # loopback-bound (localhost:8009) AJP connector — FreeIPA's own accepted
    # posture for conf.d/ipa-pki-proxy.conf. Created atomically with the final
    # mode (O_CREAT mode arg) so there is no world-readable-then-tighten window.
    fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)  # NOSONAR - S2612: installer parity; AJP secret gates loopback-only
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)


if __name__ == "__main__":
    main()
