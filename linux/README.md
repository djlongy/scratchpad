# Linux

Distribution-level reference docs and configs that aren't tied to a
specific automation tool. Things in here are the kind of thing you'd
copy into a runbook or onto a workstation, not into an Ansible repo.
For Ansible-specific snippets see [`../ansible/`](../ansible/).

## Contents

| Folder | What's there | When you'd reach for it |
|---|---|---|
| [`fapolicyd/`](fapolicyd/) | Troubleshooting fapolicyd denials on EL9 — `--debug-deny`, trust.d/ vs rules.d/, common application reference. | "Why is `Operation not permitted` blocking my app on a hardened EL9 box and how do I whitelist it?" |
| [`kickstart/`](kickstart/) | Hardened EL9 kickstart with GNOME, fapolicyd, FIPS, STIG scan, USB passthrough. | Unattended bring-up of a new EL9 workstation/server. |
| [`kickstart-dynamic/`](kickstart-dynamic/) | Dynamic RHEL-family kickstart (EL8/9/10): auto-detects disk/firmware/NIC, LVM with free VG headroom (grow online, no rebuild), CIS mounts, kdump — plus an Ansible playbook that connects on a staging IP and gives the host its permanent IP. | Zero-touch bootstrap of a physical or VM host, then hand off to Ansible. |

## Cross-links into ansible/

The fapolicyd guide above documents the **shell** workflow (debug,
diagnose, drop a rule file). When you want to deploy the same rules
**at scale via Ansible**, the guide cross-links to:

- [`ansible/roles/common/tasks/fapolicyd.yml`](../ansible/roles/common/tasks/fapolicyd.yml) — reusable task that templates a rule into `/etc/fapolicyd/rules.d/` and reloads.
- [`ansible/files/fapolicyd-rule-templates/*.j2`](../ansible/files/fapolicyd-rule-templates/) — Jinja templates for app-specific allow rules (generic, Java, NiFi, podman, vscode).
