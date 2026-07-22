# vSphere GOSC clones and the NIC that never reconnects

Why a VM cloned with `community.vmware.vmware_guest` + a customization spec boots with
its NIC disconnected forever, why `start_connected: true` in the networks dict doesn't
help, and the staged-create fix that makes it connect on first boot every time.
Live-tested July 2026 on vSphere 8 (standalone ESXi host, AlmaLinux 9 template,
community.vmware 5.10.0, open-vm-tools 13.0.10).

## Symptom

- Clone from template with `customization:` (static IP / hostname): the VM powers on,
  vCenter logs `Started customization` → `Customization succeeded`, the guest gets the
  right hostname + NetworkManager profile — but the vNIC shows
  `Connected: false / Start connected: false` and never recovers. Any `wait_for_ip`
  times out.
- Sometimes the NIC *does* connect around the customization reboot, then drops again
  ~10 seconds after the guest comes back up, permanently.
- Forcing `connected: true, start_connected: true` in the module's `networks:` list
  changes nothing.

## Mechanism (established live, corroborated by sources)

1. **The reconnect owner is whoever disconnected the NIC.** vSphere's customization
   flow disconnects vNICs at first power-on and *records* what it disconnected; the
   in-guest open-vm-tools deployPkg plugin finishes customization by asking the VMX to
   re-enable those recorded NICs (`enable-nics`). `CustomizationSucceededEvent` is
   reporting, not the reconnect trigger.
2. **`vmware_guest`'s one-call clone+customize+power-on creates the vNIC already
   disconnected** — the `connected`/`start_connected` values in `networks:` do not
   survive that path (same class of behaviour as ansible/ansible#45834 and
   terraform-provider-vsphere#388). The NIC enters first power-on `false/false`, so the
   recorded set is empty and **no layer owns reconnecting it**.
3. **Wrong guest OS type makes it worse.** With a template identified as
   `other5xLinux64Guest` (outside the customization support matrix), the guest's
   `enable-nics` no-ops: the VMX answers `queryNicsSupported` only, and the log claims
   "network interfaces are connected on 1 second" with nothing tracked. With
   `rhel9_64Guest`, `enable-nics` genuinely flips the runtime state
   (`disconnected` → `connected` responses) — but vCenter's *authoritative device
   config* still says disconnected, and it reasserts that (observed as vDS
   "Deleted ports") ~10 s after the customization reboot. That is the
   "connects, then disconnects, never comes back" variant.
4. **Dropping `customization:` doesn't opt out** — `vmware_guest` attaches a GOSC spec
   to every template clone regardless, so the same dance happens.
5. cloud-init was ruled out as the disconnector (service disabled in the guest;
   deployPkg log confirms "Cloud-init status is 'disabled'").

The reason the identical inventory pattern "just works" in other estates: their stack
delivers `startConnected=true` to vCenter **before first power-on**, so vSphere's own
bookkeeping (or no disconnect at all) keeps the NIC up. The per-inventory
`start_connected: true` dict is necessary-looking but the *sequencing* is what makes it
effective.

## Live test matrix (VM name sanitised)

| Test | Template guestId | Flow | Result |
|---|---|---|---|
| A | other5xLinux64Guest | one-call clone+GOSC+power-on, flags forced true | NIC `false/false` before power-on; customization "succeeds"; NIC dead forever |
| B | rhel9_64Guest | same | enable-nics connects at the GOSC reboot; vCenter reverts it ~10 s after boot; dead |
| C | rhel9_64Guest | same but NO `customization:` | GOSC auto-attached anyway; same as B |
| E ×3 | rhel9_64Guest | **staged** (below) | connected throughout, static IP in ~80–90 s, zero heal |
| F ×2 | other5xLinux64Guest | **staged** | same success — works even on the wrong guestId |

## The fix — staged create (implemented in the `vsphere_vm` role)

Build new VMs in three calls instead of one:

```yaml
# 1. Clone POWERED OFF with the GOSC spec attached (fires at first power-on).
- community.vmware.vmware_guest:
    name: "{{ vm_name }}"
    template: "{{ template }}"
    state: poweredoff
    networks: "{{ networks }}"
    customization: "{{ customization }}"

# 2. Assert every vNIC connected + start-connected WHILE OFF — this is what the
#    one-call path loses.
- community.vmware.vmware_guest_network:
    name: "{{ vm_name }}"
    label: "Network adapter {{ idx + 1 }}"
    network_name: "{{ item.name }}"
    connected: true
    start_connected: true
    state: present
  loop: "{{ networks }}"

# 3. First power-on. The NIC enters first boot live and is never disconnected.
- community.vmware.vmware_guest_powerstate:
    name: "{{ vm_name }}"
    state: powered-on
```

Guard the staging with a cheap per-VM existence probe (`vmware_guest_info`,
`failed_when: false` — a missing `instance` key means "doesn't exist yet"): fresh VMs
take the staged path; existing VMs take a plain idempotent `vmware_guest` reconcile and
are never power-cycled. Keep any bounded reconnect/heal loop as defense-in-depth — with
the staged create it reduces to a bounded IP wait.

## Template baseline (so customization itself works)

- Correct guest OS type: `rhel9_64Guest` for RHEL/Alma 9 on hardware v19
  (`almalinux_64Guest` needs hardware v20 — see packer-examples-for-vsphere#383).
  Never `other5xLinux64Guest`.
- `open-vm-tools` (≥12, carries deployPkg) + `perl`; kickstart `network --onboot=yes`.
- If cloud-init is installed: exactly ONE `disable_vmware_customization: true` in
  `/etc/cloud/cloud.cfg` (duplicates make tools and cloud-init fight);
  `datasource_list: [ VMware, OVF, None ]` only if using the GuestInfo path.
- Seal: truncate machine-id, remove ssh host keys, `cloud-init clean --logs --seed`.

## Sources

- https://github.com/ansible/ansible/issues/45834 — clone+customize leaves NIC disconnected; flags ineffective
- https://github.com/hashicorp/terraform-provider-vsphere/issues/388 — same in Terraform
- https://github.com/vmware/open-vm-tools/issues/208 — guest-side enable-nics internals ("No nics to enable")
- https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere-sdks-tools/7-0/web-services-sdk-programming-guide/virtual-machine-guest-operations/guest-network-customization-for-instant-clone-virtual-machines/disconnecting-virtual-nics.html — vSphere's disconnect flow
- https://github.com/vmware-samples/packer-examples-for-vsphere/issues/383 — AlmaLinux guest type × hardware version
