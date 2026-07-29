"""Shared setup that must run BEFORE any test module imports ansible.

`ANSIBLE_FILTER_PLUGINS` is read once, when ansible builds its plugin loader.
Setting it at the top of a test module is too late: pytest imports every module
in this directory during collection, and the first one to `import ansible`
freezes the filter path for the whole session. Alphabetically that is not
guaranteed to be the module that sets the variable — a new test file earlier in
the alphabet importing ansible at module top silently breaks every Templar test
that relies on the filter path. A conftest is imported ahead of every test
module in its directory, which is the only place this can be set reliably.

`ANSIBLE_JINJA2_NATIVE` is here for the same reason. Only <= 2.18 reads it:
2.19 removed the toggle and made native the only mode. It is kept so a 2.18
comparison run matches ansible.cfg.
"""

from __future__ import annotations

import os
import pathlib

_FILTER_PLUGINS = (
    pathlib.Path(__file__).resolve().parents[3] / "plugins" / "filter"
)

os.environ.setdefault("ANSIBLE_FILTER_PLUGINS", str(_FILTER_PLUGINS))
os.environ.setdefault("ANSIBLE_JINJA2_NATIVE", "True")
