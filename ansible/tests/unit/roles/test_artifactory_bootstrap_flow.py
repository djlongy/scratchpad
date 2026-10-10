"""Run the real role entry point with harmless bootstrap/persistence stand-ins."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROLE = Path(__file__).resolve().parents[3] / "roles/artifactory"
CLI = shutil.which("ansible-playbook")


@pytest.mark.skipif(CLI is None, reason="ansible-playbook is required")
@pytest.mark.parametrize("tags", [None, "bootstrap"])
def test_bootstrap_persistence_runs_after_adopting_token(tmp_path, tags):
    role = tmp_path / "roles/artifactory"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir()
    shutil.copyfile(ROLE / "tasks/main.yml", role / "tasks/main.yml")
    shutil.copyfile(ROLE / "defaults/main.yml", role / "defaults/main.yml")
    bootstrap = [
        {"name": "Adopt fixture token", "ansible.builtin.set_fact": {"_af_token": "public-fixture-token"}},
        {"name": "Record persistence", "ansible.builtin.set_fact": {"_af_bootstrap_persisted": True}},
    ]
    (role / "tasks/bootstrap.yml").write_text(json.dumps(bootstrap))
    for name in ("preflight", "backup", "apply", "compare"):
        (role / f"tasks/{name}.yml").write_text(json.dumps([{
            "name": "Mock service boundary", "ansible.builtin.debug": {"msg": "No service calls"}
        }]))
    play = [{
        "name": "Exercise bootstrap control flow without services or credentials",
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"artifactory_access_token": "", "artifactory_admin_password": "",
                 "artifactory_bootstrap_enabled": True},
        "roles": ["artifactory"],
        "post_tasks": [{"name": "Require persistence after token adoption", "tags": ["always"],
                        "ansible.builtin.assert": {"that": ["_af_bootstrap_persisted | default(false)"]}}],
    }]
    (tmp_path / "play.yml").write_text(json.dumps(play))
    config = tmp_path / "ansible.cfg"
    config.write_text("[defaults]\nstdout_callback=default\nretry_files_enabled=false\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(config), "ANSIBLE_ROLES_PATH": str(tmp_path / "roles"),
           "ANSIBLE_LOCAL_TEMP": str(tmp_path / "local-tmp"), "ANSIBLE_STDOUT_CALLBACK": "default",
           "ANSIBLE_CALLBACK_PLUGINS": str(tmp_path / "callbacks"), "ANSIBLE_CACHE_PLUGIN": "memory",
           "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"}
    env.pop("ANSIBLE_CALLBACKS_ENABLED", None)
    args = [CLI, "-i", "localhost,", str(tmp_path / "play.yml")]
    if tags:
        args += ["--tags", tags]
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
