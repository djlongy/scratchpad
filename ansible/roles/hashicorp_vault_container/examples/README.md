# hashicorp_vault_container — inventory examples

Copy **one human path**, then add optional add-ons only when needed.
Full contract: [../README.md](../README.md) → **Behaviour** (Auth & secrets phases).

| File | Use when |
|---|---|
| [`path-a-ldap-groups.yml`](path-a-ldap-groups.yml) | FreeIPA humans, flat RBAC (default start) |
| [`path-b-identity-nesting.yml`](path-b-identity-nesting.yml) | Need nested "superadmin inherits tenant groups" |
| [`add-on-ci-and-automation.yml`](add-on-ci-and-automation.yml) | Layer CI JWT / AppRole / break-glass userpass |

```bash
# Apply Path A vars on top of your inventory (merge into group_vars in real use)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/vault_cluster.yml \
  -e @roles/hashicorp_vault_container/examples/path-a-ldap-groups.yml
```

Map each FreeIPA group CN on **one** grant path (Path A *or* Path B).
Dual attachment on the same CN stacks policies and is hard to debug.
