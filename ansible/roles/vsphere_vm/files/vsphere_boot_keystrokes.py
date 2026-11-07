#!/usr/bin/env python3
"""Send console keystrokes to a vSphere VM during its boot window.

Some installer media will not start without a keypress. UEFI Windows media is
the common case: its EFI boot file prints "Press any key to boot from CD or
DVD..." and, if nothing is pressed within a few seconds, falls through the rest
of the boot order and lands in the firmware Boot Manager. The VM then sits there
forever looking like a slow install.

There is no way to answer that prompt from inside the guest — the guest does not
exist yet. The hypervisor's PutUsbScanCodes API injects the keypress at the
console instead, which is the only channel available at that point in the boot.

The prompt is short and its timing depends on how long firmware POST takes, so
this sends the key repeatedly across the whole window rather than trying to
predict the instant. Extra presses are harmless: by the time they land, Windows
Setup is being driven by its answer file and ignores console input.

Credentials are read from the environment, never argv, so they do not appear in
the process table.
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import time

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

# USB HID usage ids. The API wants the usage id in the high 16 bits with the
# HID page (7 = keyboard/keypad) in the low bits.
_HID_PAGE_KEYBOARD = 7
_HID_KEYS = {
    "ENTER": 0x28,
    "ESC": 0x29,
    "SPACE": 0x2C,
    "DOWN": 0x51,
    "UP": 0x52,
}


class BootKeystrokeError(RuntimeError):
    """Raised when the VM cannot be found or the console rejects the codes."""


def _hid_code(key: str) -> int:
    """Return the packed usbHidCode the vSphere API expects for ``key``."""
    try:
        usage = _HID_KEYS[key.upper()]
    except KeyError as exc:
        known = ", ".join(sorted(_HID_KEYS))
        raise BootKeystrokeError(f"unknown key {key!r}; known keys: {known}") from exc
    return (usage << 16) | _HID_PAGE_KEYBOARD


def _connect(host: str, user: str, password: str, cafile: str | None):
    """Open a vCenter session. Certificate and hostname checks stay on.

    A vCenter using a private issuing CA chains to the system trust store, so the
    default context verifies. ``cafile`` adds a private CA without turning
    verification off.
    """
    if not cafile:
        return SmartConnect(host=host, user=user, pwd=password)
    context = ssl.create_default_context(cafile=cafile)
    return SmartConnect(host=host, user=user, pwd=password, sslContext=context)


def _find_vm(content, name: str) -> vim.VirtualMachine:
    """Return the VM named ``name``, or raise if it is absent or ambiguous."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        matches = [vm for vm in view.view if vm.name == name]
    finally:
        view.Destroy()

    if not matches:
        raise BootKeystrokeError(f"no VM named {name!r} found in this vCenter")
    if len(matches) > 1:
        raise BootKeystrokeError(
            f"{len(matches)} VMs are named {name!r} — refusing to guess which "
            "console to type into"
        )
    return matches[0]


def send_keystrokes(
    vm: vim.VirtualMachine, key: str, count: int, interval: float
) -> int:
    """Send ``key`` to the VM console ``count`` times, ``interval`` apart.

    Returns the number of presses the console accepted. A press that fails is
    counted as a miss rather than an error: the console is briefly unavailable
    across the firmware-to-bootloader handover, and that is expected.
    """
    spec = vim.UsbScanCodeSpec()
    event = vim.UsbScanCodeSpecKeyEvent()
    event.usbHidCode = _hid_code(key)
    spec.keyEvents = [event]

    accepted = 0
    for attempt in range(count):
        try:
            accepted += vm.PutUsbScanCodes(spec)
        except vim.fault.VimFault:
            pass
        if attempt < count - 1:
            time.sleep(interval)
    return accepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="vCenter hostname")
    parser.add_argument("--vm", required=True, help="VM name (not a path)")
    parser.add_argument("--key", default="ENTER", help="key to send")
    parser.add_argument("--count", type=int, default=60, help="number of presses")
    parser.add_argument(
        "--interval", type=float, default=1.5, help="seconds between presses"
    )
    parser.add_argument(
        "--validate-certs",
        action="store_true",
        help="accepted for compatibility; verification is always on",
    )
    parser.add_argument(
        "--cafile",
        default=os.environ.get("VCENTER_CAFILE"),
        help="CA bundle for a private vCenter. Verification stays on",
    )
    args = parser.parse_args()

    user = os.environ.get("VSPHERE_USER", "")
    password = os.environ.get("VSPHERE_PASSWORD", "")
    if not user or not password:
        print(
            "VSPHERE_USER and VSPHERE_PASSWORD must be set in the environment "
            "(they are deliberately not accepted as arguments, so they never "
            "appear in the process table)",
            file=sys.stderr,
        )
        return 2

    connection = None
    try:
        connection = _connect(args.host, user, password, args.cafile)
        vm = _find_vm(connection.RetrieveContent(), args.vm)
        accepted = send_keystrokes(vm, args.key, args.count, args.interval)
        print(
            f"sent {args.key} x{args.count} to {args.vm} over "
            f"~{int(args.count * args.interval)}s; console accepted {accepted}"
        )
        return 0
    except BootKeystrokeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except vim.fault.InvalidLogin:
        print("error: vCenter rejected the credentials", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            Disconnect(connection)


if __name__ == "__main__":
    sys.exit(main())
