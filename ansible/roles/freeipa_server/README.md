# freeipa_server

## TL;DR

Portable, multi-OS FreeIPA **server** role. Wraps the upstream
`freeipa.ansible_freeipa` `ipaserver`/`ipareplica` roles for install, and layers
cold-start resilience, a scheduled backup timer, declarative IAM/DNS
reconciliation, and an opt-in post-install hardening baseline.

```bash
# Install (one-time; primary + replicas from inventory topology)
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa.yml

# Most common day-2 op: reconcile identity after editing freeipa_iam_* data
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa.yml --tags iam
```

Targets EL-family (RHEL/Rocky/Alma/CentOS/Fedora) for the server role; Debian
upstream is client-only.

## Requirements

Install collections before running (repo `requirements.yml`, or ad-hoc):

    ansible-galaxy collection install -r requirements.yml

| Collection | When | Used for |
|---|---|---|
| `freeipa.ansible_freeipa` | always | `ipaserver`/`ipareplica` install; all IAM/DNS/automember/trust/backup reconciliation |
| `community.general` | When `iam` / `hardening` | `ldap_search`/`ldap_attrs`/`dict_kv` for stale-object reporting, hardening, and IAM list flattening |
| `community.hashi_vault` | When Vault credential fallback | admin/DM password lookup, plus migration and AD-trust credential resolution |

## Key variables

Full list: `defaults/main.yml`. Contract: `meta/argument_specs.yml`.

**Required** = value must be correct for a successful run (defaults often work).
**Optional** = safe to leave default / empty; phase stays off or uses built-ins.
**When X** = required only if that feature is on.

| Req | Variable | Default | Purpose |
|---|---|---|---|
| **Required** | `domain` (→ `freeipa_server_domain`) | `""` | Realm derives from this; the one truly required input |
| **Required** | `freeipa_server_admin_password` | `""` | IPA admin password (or `freeipa_server_vault_secret` fallback) |
| When `install` | `freeipa_server_dm_password` | `""` | Directory Manager password, install only |
| When `install` + DNS | `freeipa_server_forwarders` | `[]` | Upstream DNS forwarders (unless `no_forwarders`) |
| Optional | `freeipa_server_vault_secret` | unset | HashiCorp Vault KV path; fallback only when passwords above are empty |
| Optional | `freeipa_server_ca_mode` | `self-signed` | `self-signed` \| `external-ca` \| `ca-less` |
| Optional | `freeipa_server_authoritative` | `false` | Soft-prune switch — reconcile deletes/archives undeclared objects when true |
| Optional | `freeipa_iam_tenants_dir` | `""` | Directory of per-tenant identity files; empty = lists come from group_vars |
| Optional | `freeipa_server_rbac_roles` | `[]` | Optional RBAC overlay (abstract role → group nesting) |
| Optional | `freeipa_server_heal_enabled` | `false` | Opt-in self-heal of a broken server (incident response) |
| Optional | `freeipa_iam_delete` | `false` | Single hard-delete gate; mock/lab realms only |
| Optional | `freeipa_server_resilience_enabled` | `true` | Cold-start recovery timer + SSSD self-heal watchdog |

## Usage

```yaml
# inventory.yml — a single server needs no special groups
all:
  hosts:
    ipa01: { ansible_host: 10.0.0.10 }
  vars:
    domain: example.com
    freeipa_server_admin_password: "..."   # or freeipa_server_vault_secret
    freeipa_server_dm_password: "..."
    freeipa_server_forwarders: ["10.0.0.1"]

# playbooks/freeipa.yml
- hosts: freeipa
  gather_facts: true
  roles:
    - role: freeipa_server
```

```bash
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa.yml            # install + everything
ansible-playbook -i inventories/<env>/hosts.yml playbooks/freeipa.yml --tags iam # reconcile identity only
```

Add hosts to the `freeipa` group (optionally `freeipa_primary`/`freeipa_replica`
for clarity) to grow into a cluster — non-primary hosts enrol as replicas
automatically. Every secret resolves the same way: a declared var wins, Vault
(`freeipa_server_vault_secret`) is only the lazy fallback — supplying the
password directly means no Vault is needed at all.

## Preconditions

- Install/replica join needs the target's crypto policy, time sync, and FQDN
  resolution already correct; a replica additionally needs a reachable,
  already-installed primary.
- AD trust (`--tags adtrust`) needs the trust controller enabled, the AD DCs
  reachable, and two-way DNS resolution between the realm and the AD forest.
- Migration (`playbooks/freeipa_migrate.yml`) needs the source realm reachable
  over LDAPS.

## Behaviour

- **Certificate Authority** — `freeipa_server_ca_mode` picks which CA the
  realm serves: `self-signed` *(default, trusted only where you distribute
  `/etc/ipa/ca.crt`)*, `external-ca` *(chains to your org root)*, or `ca-less`
  *(IPA issues nothing; you supply every service cert)*. `external-ca` is a
  CSR roundtrip: install with `external_cert_files: []` +
  `external_ca_allow_csr_phase: true` halts the run after emitting
  `/root/ipa.csr` for you to sign externally, then re-run with
  `freeipa_server_external_cert_files` set to the signed cert/chain.
  `--tags ca_report` is a read-only probe that prints the live CA vars for
  reproducing a server's CA settings elsewhere. `freeipa_server_trusted_external_cas`
  only imports extra trusted CA certs — it does not change the serving CA.
- **Self-heal (opt-in)** — a server that has lost some of the httpd/PKI config
  files the installer generates (RPM `%ghost` entries) sails through
  "already configured" and then breaks. `--tags heal` with
  `freeipa_server_heal_enabled: true` repairs it by re-rendering the missing
  files from FreeIPA's own templates and republishing the served CA cert — an
  incident-response tool, not a routine converge step. A half-finished install
  is always caught by a separate always-on guard regardless of this toggle.
- **IAM reconciliation** (`--tags iam`) — `freeipa_iam_*` lists are the
  source of truth; creation is additive by default (`state: present` never
  deletes). FreeIPA user groups cannot hold HBAC/sudo/host rules directly, so
  rules target a `ug-*` policy group that nests a `role-*` grant group — a
  user in `role-x` is an indirect member of `ug-x`:

  ```yaml
  freeipa_iam_usergroups:
    - { name: ug-acme-prod-admins, description: "Acme prod admins" }
  freeipa_iam_users:
    - { name: acme.dave, givenname: Dave, sn: Okafor, groups: [ug-acme-prod-admins] }
  freeipa_iam_hbac_rules:
    - { name: hbac-acme-prod-ssh, usergroup: [ug-acme-prod-admins], hostgroup: [hg-acme-prod], service: [sshd] }
  ```

  `freeipa_server_authoritative: true` is the single switch that enables
  pruning: it strips undeclared group members, deletes groups/hostgroups/
  HBAC/sudo/automember objects dropped from the desired state, and archives
  (not hard-deletes) users absent from it — destructive against an incomplete
  desired state, so always assemble the full realm before enabling it.
- **Backup** — a timer runs `ipa-backup` with retention pruning; `--tags
  backup_now` forces a synchronous backup and fails the run on error; `--tags
  restore` (break-glass) restores from a named backup; `freeipa_server_backup_fetch_name`
  offloads a backup to the controller.
- **Automember** — `freeipa_server_automember_rules` auto-assigns users/hosts
  to an existing group/hostgroup by attribute regex, solving the
  enrol-then-authorize ordering problem for freshly joined hosts.
- **Hardening (opt-in)** — anonymous-bind restriction, LDAP search limits,
  forced-OTP groups, guarded `allow_all` teardown, crypto-policy report; see
  `defaults/main.yml` for the full `freeipa_server_harden_*` set.
- **Destructive operations** — two severities, both opt-in and non-default
  tagged: prune (soft, recoverable) via `freeipa_server_authoritative`;
  delete (hard, irrecoverable) via `freeipa_iam_delete` plus the `never`-tag
  that picks the operation (`--tags delete` for declared objects, `--tags
  prune_preserved` for orphaned archived users). `--check` makes either a dry
  run.
- **Adopt / migrate** — `--tags export` is a read-only snapshot of a live
  realm into this role's `freeipa_iam_*`/`freeipa_server_*` contract, drop-in
  and idempotent even onto a fresh empty server (`freeipa_server_export_scope`
  carves it into per-tenant slices). `playbooks/freeipa_migrate.yml` wraps
  `ipa-migrate` to pull identities from a source realm; passwords are never
  migrated (Kerberos keys are realm-salted) and `freeipa_migrate_dryrun: true`
  is the safe default.

## Out of scope

- DNS zone/record reconciliation (`--tags dns`) only covers FreeIPA's own
  integrated DNS — it does not manage an external DNS system.
- Decommissioning a server (removing a master from the realm) is not
  automated — it's a rare, high-blast-radius operation performed by hand.

## Known failure mode

Several filters in `filter_plugins/freeipa_iam.py` parse the text output of
`ipa <type>-find --all --raw` (no JSON API on the CLI path). After a FreeIPA
upgrade that reformats this output, symptoms range from harmless
(prechecked types report every entry changed) to a hard failure with an
actionable message, to a silent evict no-op — check `freeipa_iam_evict_payload`
output at `-vv` after any FreeIPA version bump. Fixture-pinned regression
tests live at `tests/unit/roles/test_freeipa_iam_filters.py`
(`pytest tests/unit -q`).
