# Example: provision a VM + its storage in one play

A complete, self-contained example of the `vsphere_vm` role composed with the
`storage` role. One `ansible-playbook` run builds the VM from a template and turns
its two extra disks into mounted LVM volumes.

## Files

| File | What it shows |
|---|---|
| `inventory.yml` | one host `vm01` with the full contract: vCenter placement, hardware, **two data disks**, a static NIC, a **storage profile**, tags, and a commented **3-NIC** alternative |
| `site.yml` | the two-role play — `vsphere_vm` (with `vsphere_vm_wait_for_ssh`) then `storage`, `become` correct for each |

Everything in `inventory.yml` is a placeholder — swap in your vCenter, datastore,
template, portgroups, IPs and Vault path.

## Run it

```bash
# build the VM, wait for SSH, provision /opt + /data
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory.yml site.yml
```

```bash
# recreate from scratch (delete VM + disks, rebuild)
ansible-playbook -i inventory.yml site.yml --tags redeploy -e vsphere_vm_allow_redeploy=true

# grow: bump the vsphere_vm_disk sizes (and the by-size: selectors), then
ansible-playbook -i inventory.yml site.yml --tags create,grow

# destroy
ansible-playbook -i inventory.yml site.yml -e vsphere_vm_state=absent \
  --tags destroy -e vsphere_vm_allow_destroy=true
```

## What it demonstrates

- **One inventory → provision-if-absent → configure.** The play targets the VM host;
  `vsphere_vm` builds it (or reconciles an existing one — the plan is purely
  inventory-derived, no vCenter state read), then hands off over SSH so `storage`
  runs on the guest in the same play. A pre-build **report** prints every host's plan
  (vCenter name, guest hostname, NICs, disks, mode) before anything is touched.
- **Two disks via a storage profile.** Each data disk is pinned `by-size` and filled
  `100%FREE`. Bump a `vsphere_vm_disk` size + the matching `by-size:` selector and
  `--tags create,grow` grows the vmdk (via `vmware_guest_disk`) and the filesystem —
  non-destructive.
- **Multi-NIC, per-NIC routing.** Uncomment `vsphere_vm_networks` for three static
  NICs. A NIC with a `gateway` is routed and carries DNS; one without is unrouted
  (data/storage plane). `type` defaults to `static` (an `ip` implies static; no `ip`
  → DHCP). `interface` defaults to VMXNET3 slot order (`ens192`/`ens224`/`ens256`).
- **`become` placement.** `become: true` at the play is safe — `vsphere_vm` forces
  `become: false` on its own (localhost-delegated) tasks, and the on-guest `storage`
  role inherits the play's `become: true`.

See the role README for the full variable reference and the gateway precedence
(explicit `vsphere_vm_gateway` > `vsphere_vm_gateway_auto` > no gateway).
