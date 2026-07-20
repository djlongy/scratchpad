# host_vars

Use **only** when a single host must diverge from its group.

Examples that belong here:

- one-off IP / NIC override
- pin `canonical_hostname` if FreeIPA/certmonger must match a legacy name
- host-specific disk layout

Do **not** put group-wide role knobs here — those go in `group_vars/<group>/`.

```yaml
# host_vars/vault-01.yml  (example shape — file not required)
# canonical_hostname: "{{ inventory_hostname }}"   # skip env-token strip
# vsphere_vm_memory: 16384
```
