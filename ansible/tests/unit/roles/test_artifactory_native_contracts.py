"""Native role conditionals and project attachment success contracts."""
from pathlib import Path

import pytest
import yaml

from _templating import render


@pytest.mark.parametrize("dropped,rejected,expected", [({}, {}, False), ({"unknown": "key"}, {}, True), ({}, {"block": "error"}, True)])
def test_system_report_condition_is_boolean(dropped, rejected, expected):
    source = Path(__file__).resolve().parents[3] / "roles/artifactory/tasks/system/system_config.yml"
    tasks = yaml.safe_load(source.read_text())
    task = next(item for item in tasks if item["name"] == "System_config | Report dropped / rejected config keys")
    result = render("{{ " + task["when"][1] + " }}", {"_ap_sysyaml": {"dropped": dropped, "rejected": rejected}})
    assert result is expected


@pytest.mark.parametrize("status,failed,changed", [(204, False, True), (403, True, False), (500, True, False)])
def test_project_attachment_accepts_native_success_status(status, failed, changed):
    source = Path(__file__).resolve().parents[3] / "roles/artifactory/tasks/projects/project_item.yml"
    tasks = yaml.safe_load(source.read_text())
    block = next(item["block"] for item in tasks if item["name"] == "Project | reconcile")
    task = next(item for item in block if item["name"] == "Project | attach repositories")
    scope = {"_proj_repo": {"status": status}, "artifactory_fail_fast": True}
    assert render("{{ " + task["failed_when"][1] + " }}", scope) is failed
    assert render("{{ " + task["changed_when"] + " }}", scope) is changed
    assert (status in task["ansible.builtin.uri"]["status_code"]) is (not failed)
