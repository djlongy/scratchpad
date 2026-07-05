
## Quick start (GuestInfo mode, static IP)

```yaml
# group_vars/vmware_vms.yml
vsphere_vm_server: "vcenter.example.com"
vsphere_vm_password: "{{ vault_vcenter_password }}"
vsphere_vm_datacenter: "Datacenter"
vsphere_vm_esxi_host: "192.0.2.10"
vsphere_vm_resource_pool: "resgroup-123"     # standalone host: ROOT pool MOID
vsphere_vm_datastore: "datastore1"
vsphere_vm_template: "linux-almalinux-9-main"
vsphere_vm_network: "VLAN10-SVC"
vsphere_vm_dns: [192.0.2.53]
vsphere_vm_provision_via_guestinfo: true     # GOSC-free mode (per-host overridable)
vsphere_vm_from_inventory: true
vsphere_vm_inventory_group: vmware_vms

# host_vars/web01.yml — the only unique bit
ansible_host: 192.0.2.50                     # becomes the VM's static IP
```

```bash
ansible-playbook -i inventories/example/hosts.yml playbook.yml   # create
```

Template prerequisites (Packer-baked): guestId `rhel9_64Guest` (never `other*`),
open-vm-tools + perl, cloud-init with `datasource_list: [VMware, OVF, None]`,
`allow_raw_data: true`, `disable_vmware_customization: true`, sealed with
`cloud-init clean --logs --seed`. Verify in a clone: `cloud-init query platform` → `vmware`.
