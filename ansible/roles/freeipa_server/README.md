# freeipa_server

## TL;DR

Wraps the upstream `freeipa.ansible_freeipa` `ipaserver`/`ipareplica` roles for
install, then layers cold-start resilience, a backup timer, declarative IAM
reconciliation (users/groups/HBAC/sudo/DNS/automember), and opt-in hardening.

**Most common: reconcile identity.** Edit the `freeipa_iam_*` lists, then
re-run (idempotent, primary-only):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa.yml --tags iam
```

Install is a separate one-time run (no tags does install + everything):

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa.yml
```

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `freeipa.ansible_freeipa` | always | wraps `ipaserver`/`ipareplica` and every declarative IAM/DNS/trust module |
| `community.general` | always | `ldap_search` for the stale-account report; `ldap_attrs` for opt-in hardening |
| `community.hashi_vault` | When admin/dm password empty | reads the admin/DM password fallback from Vault |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `domain` | (inventory `group_vars/all`) | Base domain; `freeipa_server_domain`/`_realm` derive from it |
| When install | `freeipa_server_admin_password` | `""` | IPA admin password (declared var wins over Vault fallback) |
| When install | `freeipa_server_dm_password` | `""` | Directory Manager password (declared var wins over Vault fallback) |
| When admin/dm empty | `freeipa_server_vault_secret` | unset | HashiCorp Vault KV path — fallback only |
| **Required** | `freeipa_server_forwarders` | `[]` | Upstream DNS forwarders (install + day-2 `ipadnsconfig`; e.g. `["{{ network_gateway }}"]`). Integrated DNS is ON by default, and preflight rejects a run that declares neither this nor `freeipa_server_no_forwarders: true` — so this is required unless you set one of those. |
| When DNS | `freeipa_server_dns_trusted_networks` | `[]` | CIDRs allowed to recurse (BIND ACL; every DNS server) |
| Optional | `freeipa_server_primary_group` | `freeipa_primary` | Inventory group whose first host is the primary (falls back to `freeipa`, then this host) |
| Optional | `freeipa_server_ca_mode` | `self-signed` | Serving CA: `self-signed` \| `external-ca` \| `ca-less` |
| Optional | `freeipa_server_setup_dns` | `true` | Install FreeIPA's integrated DNS |
| Optional | `freeipa_server_resilience_enabled` | `true` | Cold-start recovery timer + SSSD self-heal watchdog |
| Optional | `freeipa_iam_tenants_dir` | `""` | Directory of per-tenant identity files (empty = legacy single group_vars) |
| Optional | `freeipa_server_authoritative` | `false` | Soft-prune switch: reconcile removes undeclared members/objects (archives users) |
| Optional | `freeipa_iam_users_preserved` | `[]` | Explicit offboarding — entries **moved** out of `freeipa_iam_users`. Preserve-only, ungated by `freeipa_server_authoritative` |
| Optional | `freeipa_iam_delete` | `false` | Hard-delete gate (irrecoverable `ipa *-del`); which operation runs is chosen by `--tags` |
| Optional | `freeipa_server_rbac_roles` | `[]` | Thin RBAC overlay: assign users an abstract role instead of many groups |
| Optional | `freeipa_server_trusted_external_cas` | `[]` | Additive trust of third-party CA certs (does not change the serving CA) |

## Minimum configuration

```yaml
# group_vars/freeipa_server_hosts.yml
---
# Required
domain: example.internal
# Integrated DNS is ON by default, and preflight refuses a run that declares
# neither of these. Pick ONE (declaring both is rejected upstream):
freeipa_server_forwarders: ["192.0.2.1"]
# freeipa_server_no_forwarders: true     # root servers only
# ...or turn integrated DNS off entirely with freeipa_server_setup_dns: false
```

<!-- NOTE: hand-maintained. scripts/update_role_readmes_min_config.py emits a scalar
     "REPLACE_ME_<var>" placeholder for every required var, which is wrong for
     list-typed ones like freeipa_server_forwarders — regenerating this block would
     reintroduce a minimum configuration that cannot run. -->

## Usage

```yaml
- name: Deploy and reconcile FreeIPA server
  hosts: freeipa
  roles:
    - role: freeipa_server
      tags: [freeipa_server]
```

Run it:

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/20_iam_freeipa.yml --tags iam
```

Single server needs no special inventory groups — one host in `freeipa` is the
primary. Add more hosts (optionally to `freeipa_primary`) to grow into a
cluster; non-primary hosts enrol as replicas automatically.

## Preconditions

- FIPS mode must already be enabled (`fips-mode-setup --enable`) and the host
  rebooted before install when `freeipa_server_fips_required` is true — FreeIPA
  cannot be migrated into FIPS after install.
- Chrony must be tracking with clock skew inside
  `freeipa_server_preflight_time_tolerance_sec` — Kerberos auth fails above
  300s skew.
- A replica install needs the primary's LDAP port (389) already reachable.
- An offline install (`freeipa_server_offline: true`) needs
  `freeipa_server_package_repo` defined with a working baseurl.

## Behaviour

- `freeipa_server_ca_mode` picks the realm's CA at **install** time:
  `self-signed` (default; IPA-minted root, trusted only where you
  distribute `/etc/ipa/ca.crt`), `external-ca` (two-phase — the first run
  emits a CSR at `/root/ipa.csr`, sign it externally, set
  `freeipa_server_external_cert_files`, and re-run), or `ca-less` (no CA of
  its own; you supply every service cert). The install path is a no-op once
  `/etc/ipa/default.conf` exists — changing `ca_mode` later does not convert
  the realm. To reparent a **working** self-signed CA under the org offline
  CA without reinstall, use playbook `--tags reparent_ca` /
  `scripts/freeipa-reparent-ca.sh` (`ipa-cacert-manage renew --external-ca`).
  See `docs/runbooks/freeipa-external-ca-reparent.md`.
- `freeipa_server_trusted_external_cas` additively imports third-party CA
  certs into the trust store without changing the serving CA.
- Prune (`freeipa_server_authoritative: true`) archives undeclared users
  (recoverable — `ipa user-undel`) and deletes undeclared matrix-managed
  objects. **Object deletion is NOT recoverable**: a group cannot be
  preserve-archived, so a deleted group and its memberships are gone. Object
  deletion is scoped — only names containing `freeipa_iam_reconcile_scope` are
  eligible, a blank scope deletes nothing, and
  `freeipa_iam_reconcile_protect_regexes` shields externally-owned names. That
  shield ships **empty** — the role carries no patterns for any particular
  third-party product — so declare your realm's in inventory; an armed prune
  with both protect lists empty warns and shields nothing by pattern.
  `freeipa_iam_protected_groups` is separate and unconditional: those groups are
  never deleted at any setting, and are exempt from both undeclared-member
  removers (the declarative strip and the eviction/snap-back pass).
- **Offboarding has two routes, and the explicit one is ungated.** *Inferred*: a
  managed user missing from `freeipa_iam_users` becomes an archival candidate,
  but only under `freeipa_server_authoritative` — dropping someone from
  `group_vars` must not deactivate them on an ordinary converge. *Explicit*:
  `freeipa_iam_users_preserved` is the leaver's own entry, **moved** out of
  `freeipa_iam_users`. It runs at **every** setting, because an entry there is an
  instruction rather than an inference from absence — so you can offboard one
  person on an additive run without arming whole-realm authoritative mode (which
  would also enable group deletion and the membership strip). It accepts a bare
  login or a whole user dict, so the move is a literal cut-and-paste, and it only
  ever *preserves* (`user-del --preserve`, recoverable via `ipa user-undel`)
  regardless of `freeipa_iam_preserve_archived`. The two routes agree in steady
  state and are each idempotent, so running both is free.
  **Move, do not copy:** a login in both lists is a hard validation failure
  naming the user. It is refused rather than resolved, because task order would
  otherwise decide it — the reactivate pass would undel the account early and the
  offboarding pass would re-archive it late, so the run would never reach
  `changed=0` and the account would be able to authenticate in between. Names are
  folded, so `Alice` in one list and `alice` in the other is still caught.
- **"Additive" covers usergroup membership and object existence — nothing
  else.** Every other member or attribute list that a declared object *carries*
  is reconciled declaratively at *every* setting, defaults included: HBAC and
  sudo rule members, hostgroup nesting **and its `host:` members**,
  hbacsvcgroup/sudocmdgroup nesting, permission/privilege/role members,
  automember conditions, and a user's
  `principal`/`manager`/`certificate`/`certmapdata`. The upstream modules
  compute `del_list = current − declared`, so a member added out of band is
  removed on the next converge even with `freeipa_server_authoritative: false`.
  Sudo rules are the worst case — no precheck gate, so they reconcile on *every*
  run — and every rule the RBAC overlay generates has `usergroup: [<role>]`
  injected unconditionally, so any other group bound to it is revoked each run.
  The strip exemptions (`freeipa_iam_protected_groups` plus automember-owned
  groups) cover usergroup membership only; there is no exemption mechanism for
  the lists above. See `defaults/main.yml` under "Pruning control".
- Delete (`freeipa_iam_delete: true`) is hard and irrecoverable — it enables
  `ipa *-del`; `--tags delete` or `--tags prune_preserved` picks the
  operation. `--check` makes either a dry-run.

## Out of scope

- Host enrolment — handled by `freeipa_client`.
- Removing a master from the realm — a deliberate, manual operator action, not
  automated by this role.
