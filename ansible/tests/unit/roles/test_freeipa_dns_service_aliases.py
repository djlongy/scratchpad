"""Service aliases preserve public routing and explicit internal records."""

import importlib.util
from pathlib import Path

import pytest
from ansible.errors import AnsibleFilterError


def load(name):
    path = Path(__file__).resolve().parents[3] / "roles/freeipa_server/filter_plugins" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_aliases():
    aliases = load("dns_aliases").freeipa_dns_service_aliases
    catalogue = {
        "npm_ip": "10.0.0.2",
        "npm_proxy_hosts": [{"domain_names": ["wiki.example.test", "wiki.example.test",
                                                "git.ops.example.test", "other.example.net",
                                                "app.dev.example.test"]}],
        "firewall_dns_records": [{"host": "vip", "ip": "10.0.0.1", "domain": "ops.{{ domain }}",
                                 "aliases": [{"host": "git", "domain": "ops.example.test"},
                                             {"host": "default"},
                                             {"host": "api", "domain": "example.test"}]}],
    }
    records = aliases(catalogue, "example.test", "example.internal",
                      ["example.internal.", "ops.example.internal."], ["dev.example.internal"])
    assert records == [
        {"zone_name": "example.internal.", "records": [
            {"record_name": "api", "a_record": ["10.0.0.1"], "create_reverse": False},
            {"record_name": "default", "a_record": ["10.0.0.1"], "create_reverse": False},
            {"record_name": "wiki", "a_record": ["10.0.0.2"], "create_reverse": False}]},
        {"zone_name": "ops.example.internal.", "records": [
            {"record_name": "git", "a_record": ["10.0.0.1", "10.0.0.2"], "create_reverse": False},
            {"record_name": "vip", "a_record": ["10.0.0.1"], "create_reverse": False}]},
    ]
    static = [{"zone_name": "ops.example.internal.", "records": [
        {"record_name": "git", "a_record": ["10.0.0.1"]}]}]
    merged = load("dns_aliases").freeipa_dns_merge_records(static, records)
    assert merged[0]["records"][0] == static[0]["records"][0]
    assert len(merged[0]["records"]) == 2
    catalogue["firewall_dns_records_shared"] = catalogue.pop("firewall_dns_records")
    assert aliases(catalogue, "example.test", "example.internal",
                   ["example.internal.", "ops.example.internal."], ["dev.example.internal"]) == records
    for public, internal in [("", "example.internal"), ("example.test", "example.test")]:
        with pytest.raises(AnsibleFilterError):
            aliases(catalogue, public, internal, ["example.internal"], [])
    with pytest.raises(AnsibleFilterError, match="Invalid service DNS name"):
        aliases({"npm_ip": "10.0.0.2", "npm_proxy_hosts": [{"domain_names": ["bad name.example.test"]}]},
                "example.test", "example.internal", ["example.internal"], [])
