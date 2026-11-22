"""Exercise native OAuth provider requests and readback through real Ansible."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

ROLE = Path(__file__).resolve().parents[3] / "roles/artifactory"
CLI = shutil.which("ansible-playbook")


@pytest.mark.skipif(CLI is None, reason="ansible-playbook is required")
@pytest.mark.parametrize("existing,drop_provider,check", [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_oauth_provider_lifecycle_and_readback(tmp_path, existing, drop_provider, check):
    provider = {"name": "fixture", "enabled": True, "providerType": "openId",
                "id": "fixture-client", "secret": "", "pkce": True}
    unrelated = {"name": "unmanaged", "enabled": True}
    state = {"enabled": False, "providers": [unrelated] + ([{**provider, "id": "old"}] if existing else [])}
    writes = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # Suppress fixture HTTP logs so Ansible failures remain readable.
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())

        def write(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            writes.append((self.command, self.path, body))
            if self.path == "/oauth/provider":
                if not drop_provider:
                    state["providers"] = [unrelated, body]
            else:
                assert "providers" not in body
                state.update(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_PUT = write
        do_POST = write

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars": {"artifactory_ep": {"oauth": f"http://127.0.0.1:{server.server_port}/oauth"},
                      "_af_json_headers": {}, "artifactory_saml_config": {}, "artifactory_crowd_config": {},
                      "artifactory_fail_fast": True,
                      "artifactory_oauth_config": {"enabled": True, "providers": [provider]}},
             "tasks": [{"ansible.builtin.import_tasks": str(ROLE / "tasks/integrations/sso.yml")}]}]
    (tmp_path / "play.json").write_text(json.dumps(play))
    (tmp_path / "ansible.cfg").write_text("[defaults]\nstdout_callback=default\nretry_files_enabled=false\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
           "ANSIBLE_STDOUT_CALLBACK": "default", "ANSIBLE_CALLBACK_PLUGINS": str(tmp_path / "callbacks"),
           "ANSIBLE_CACHE_PLUGIN": "memory", "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"}
    env.pop("ANSIBLE_CALLBACKS_ENABLED", None)
    try:
        result = subprocess.run([CLI, "-i", "localhost,", str(tmp_path / "play.json")] + (["--check"] if check else []),
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert (result.returncode != 0) == drop_provider, result.stdout + result.stderr
    if check:
        assert writes == []
    else:
        assert writes[0] == ("POST" if existing else "PUT", "/oauth/provider", provider)
        assert writes[1] == ("POST", "/oauth", {"enabled": True})
    assert unrelated in state["providers"]
