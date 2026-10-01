"""Compile internal address aliases from existing proxy and resolver catalogues."""

import ipaddress
import re

from ansible.errors import AnsibleFilterError
from jinja2 import Environment, StrictUndefined


def _catalogue_addresses(catalogue, public_domain):
    """firewall aliases inherit the default domain, independently of their parent."""
    names = {}
    templar = Environment(undefined=StrictUndefined)
    def add(name, address):
        name = templar.from_string(name).render(domain=public_domain)
        address = ipaddress.IPv4Address(address)
        names.setdefault(name.lower().rstrip("."), set()).add(str(address))

    for proxy in catalogue.get("npm_proxy_hosts", []):
        for name in proxy["domain_names"]:
            if name not in catalogue.get("npm_dns_skip_domains", []):
                add(name, catalogue["npm_ip"])
    for record in catalogue.get("firewall_dns_records_shared", catalogue.get("firewall_dns_records", [])):
        domain = record.get("domain", public_domain)
        add(f"{record['host']}.{domain}", record["ip"])
        for alias in record.get("aliases", []):
            add(f"{alias['host']}.{alias.get('domain', public_domain)}", record["ip"])
    return names


def freeipa_dns_service_aliases(catalogue, public_domain, internal_domain, zones, excluded_domains):
    """Preserve relative names and select the longest hosted zone."""
    public_domain = public_domain.lower().rstrip(".")
    internal_domain = internal_domain.lower().rstrip(".")
    if not public_domain or not internal_domain or public_domain == internal_domain:
        raise AnsibleFilterError("Public and internal DNS domains must be distinct and nonempty")
    hosted = sorted({z.rstrip(".").lower() for z in zones}, key=len, reverse=True)
    result = {}
    addresses = _catalogue_addresses(catalogue, public_domain)
    for name in sorted(addresses):
        name = name.lower().rstrip(".")
        if not name.endswith("." + public_domain):
            continue
        if not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)+", name):
            raise AnsibleFilterError(f"Invalid service DNS name: {name}")
        target = name[:-len(public_domain)] + internal_domain
        if any(target == d.rstrip(".") or target.endswith("." + d.rstrip("."))
               for d in excluded_domains):
            continue
        zone = next((z for z in hosted if target.endswith("." + z)), None)
        if zone is None:
            raise AnsibleFilterError(f"No hosted internal zone for {target}")
        result.setdefault(zone, []).append({
            "record_name": target[:-(len(zone) + 1)],
            "a_record": sorted(addresses[name]),
            "create_reverse": False,
        })
    return [{"zone_name": zone + ".", "records": result[zone]} for zone in sorted(result)]


def _dns_zone_dot(zone):
    return str(zone).rstrip(".") + "." if zone else ""


def freeipa_dns_merge_records(static_records, derived_records) -> list[dict]:
    """Merge inventory-derived bulk DNS into static freeipa_server_dns_records.

    Static always wins on the same (zone_name, record_name). Flat static items
    (``name`` + A/AAAA fields) also reserve that name. Derived bulk zones are
    appended or merged into existing bulk zone entries.
    """
    static = list(static_records or [])
    derived = list(derived_records or [])
    if not derived:
        return static

    # Names claimed by static (zone → set of record names).
    claimed: dict[str, set[str]] = {}

    def _claim(zone: str, name: str) -> None:
        claimed.setdefault(_dns_zone_dot(zone), set()).add(name)

    for item in static:
        if not isinstance(item, dict):
            continue
        zone = item.get("zone_name") or ""
        if not zone:
            continue
        if item.get("name"):
            _claim(zone, str(item["name"]))
        for rec in (item.get("records") or []):
            if isinstance(rec, dict) and rec.get("record_name"):
                _claim(zone, str(rec["record_name"]))

    # Index static bulk entries by zone for in-place merge.
    bulk_index: dict[str, int] = {}
    for i, item in enumerate(static):
        if (isinstance(item, dict) and item.get("zone_name")
                and item.get("records") is not None and not item.get("name")):
            bulk_index[_dns_zone_dot(item["zone_name"])] = i

    result = [dict(x) if isinstance(x, dict) else x for x in static]

    for d_item in derived:
        if not isinstance(d_item, dict):
            continue
        zone = _dns_zone_dot(d_item.get("zone_name") or "")
        if not zone:
            continue
        new_recs = []
        for rec in (d_item.get("records") or []):
            if not isinstance(rec, dict):
                continue
            rname = rec.get("record_name")
            if not rname or rname in claimed.get(zone, set()):
                continue
            new_recs.append(dict(rec))
            _claim(zone, str(rname))
        if not new_recs:
            continue
        if zone in bulk_index:
            existing = result[bulk_index[zone]]
            merged_recs = list(existing.get("records") or []) + new_recs
            result[bulk_index[zone]] = {**existing, "records": merged_recs}
        else:
            bulk_index[zone] = len(result)
            result.append({"zone_name": zone, "records": new_recs})

    return result

class FilterModule:
    """Ansible filter registration."""

    def filters(self):
        return {"freeipa_dns_service_aliases": freeipa_dns_service_aliases,
                "freeipa_dns_merge_records": freeipa_dns_merge_records}
