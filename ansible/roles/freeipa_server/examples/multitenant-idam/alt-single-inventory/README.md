# Alternative: single inventory + `idam_tenants` selector

This is the **other** way to lay out the same generators — kept for comparison
with the primary per-tenant-inventory pattern one level up.

Here **all** tenants live in ONE inventory (`inventory/group_vars/all/tenant_*.yml`),
and you isolate a run with a runtime flag instead of by which inventory you pass:

```bash
ansible-playbook -i inventory/hosts.yml site.yml --tags idam -e idam_tenants=acme
```

Same convention, same three layers (groups → roles → users), same per-tenant
marker derivation. The differences vs the primary pattern:

| | Primary (per-tenant inventories) | This (single inventory + selector) |
|---|---|---|
| Pick a team | `-i inventories/acme` (which `-i` you pass) | `-e idam_tenants=acme` (a flag) |
| Isolation | structural — other teams aren't loaded | by convention — flag must be remembered |
| Newcomer view | "ACME's stuff is in `inventories/acme/`" | "what's `idam_tenants`?" |

Prefer the primary pattern for day-to-day work. This layout is handy if you'd
rather keep everything in one inventory and reach for a flag, or as a stepping
stone before splitting teams into their own directories.
