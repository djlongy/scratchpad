"""Templating helpers shared by every harness that replays role Jinja.

These suites all do the same thing: load a role's real tasks or templates,
render them through a real Ansible `Templar`, and assert on the result. That
only works if the harness reproduces the engine's TRUST model.

ansible-core 2.19 made templating opt-in. `Templar.template()` renders a
string only if it is marked TRUSTED; anything else is handed straight back,
verbatim, as its own source text. Ansible marks values loaded through its
DataLoader — playbooks, role defaults, inventory — and does not mark values a
test built with `yaml.safe_load` or `Path.read_text`. So a harness that worked
on 2.18 silently stops templating on 2.19+: a JSON policy template comes back
as `'{#\\n  Managed service policy …'` and `json.loads` chokes, and a fact whose value
is `{{ ... }}` compares unequal to everything.

Both halves are needed, for different reasons:

  * `trust_as_template(expr)` — the expression under test. Without it nothing
    renders at all.
  * `trust_scope(variables)` — the SCOPE, recursively. Real Ansible resolves a
    variable whose own value contains `{{ }}`, because DataLoader trusted it on
    the way in and the engine re-templates it lazily on access. Role defaults
    are chained several levels deep, so a scope of untrusted strings leaves
    every derived fact equal to its own source text.

Trusting the scope is right BECAUSE these values model inventory and defaults,
which are trusted in a real run. It is not a blanket licence — the expression
under test is trusted separately, at the call site.

Nothing here is version-gated by choice: on <= 2.18 there is no trust model, so
`trust_as_template` is the identity function and the behaviour is unchanged.
"""

from __future__ import annotations

from typing import Any

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:
    from ansible.template import trust_as_template
except ImportError:  # <= 2.18 has no trust model; strings are templated as-is
    def trust_as_template(value: Any) -> Any:
        return value


def trust_scope(value: Any) -> Any:
    """Mark scope VALUES trusted, recursing into containers."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: trust_scope(item) for key, item in value.items()}
    if isinstance(value, list):
        return [trust_scope(item) for item in value]
    return value


def render(expr: Any, variables: dict[str, Any]) -> Any:
    """Template a value the way Ansible does — recursing into containers."""
    if isinstance(expr, str):
        return Templar(loader=DataLoader(),
                       variables=trust_scope(variables)).template(
                           trust_as_template(expr))
    if isinstance(expr, dict):
        return {key: render(value, variables) for key, value in expr.items()}
    if isinstance(expr, list):
        return [render(value, variables) for value in expr]
    return expr
