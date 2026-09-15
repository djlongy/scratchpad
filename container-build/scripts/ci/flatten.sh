#!/usr/bin/env bash
# scripts/ci/flatten.sh — generate the single-file form of the composition
#
# include:remote fetches one file and nothing else: a nested `include: local:`
# inside a remotely included file has no project context and GitLab rejects it.
# So a consumer that includes this template from another host needs the whole
# composition in one file. This script produces it.
#
# It inlines each `- local: templates/*/template.yml` entry of
# pipelines/container.yml, substituting every `$[[ inputs.X ]]` in the template
# body with the value the composition passes for X. Values that are themselves
# `$[[ inputs.Y ]]` stay as they are and resolve against the flat file's own
# spec, which is the composition's spec unchanged. Pure text, so comments and
# block scalars survive and the output is byte-stable.
#
#   bash scripts/ci/flatten.sh            # write the flat file to stdout
#   bash scripts/ci/flatten.sh --write    # write pipelines/container.flat.yml
#
# scripts/test/regression.sh diffs the generated form against the committed
# one, so an edit to a template that is not regenerated fails the suite.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${ROOT}/pipelines/container.flat.yml"

flat="$(python3 - "${ROOT}" <<'PY'
import pathlib, re, sys

root = pathlib.Path(sys.argv[1])
src = (root / "pipelines" / "container.yml").read_text()

BANNER = """# ─── GENERATED FILE — DO NOT EDIT ──────────────────────────────────
# Produced by scripts/ci/flatten.sh from pipelines/container.yml and
# templates/*/template.yml. Edit those, then regenerate:
#
#   bash scripts/ci/flatten.sh --write
#
# This is pipelines/container.yml with its nested includes inlined, for
# consumers that reach the template over include:remote, where a nested
# include:local cannot resolve. Same inputs, same jobs, same behaviour.
# ───────────────────────────────────────────────────────────────────
"""


def split_spec(text, what):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line == "---":
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    sys.exit(f"ERROR: no spec/body separator in {what}")


head, body = split_spec(src, "pipelines/container.yml")
lines = body.split("\n")

try:
    start = lines.index("include:")
except ValueError:
    sys.exit("ERROR: pipelines/container.yml has no top-level include: section")

end = start + 1
while end < len(lines) and (lines[end] == "" or lines[end][:1] in (" ", "\t")):
    end += 1

# Parse the include entries: a path and the flat map of inputs under it.
entries, path, passed = [], None, {}
for raw in lines[start + 1:end]:
    if not raw.strip():
        continue
    if m := re.fullmatch(r"  - local: (\S+)", raw):
        if path:
            entries.append((path, passed))
        path, passed = m.group(1), {}
    elif raw == "    inputs:":
        continue
    elif m := re.fullmatch(r"      ([a-z0-9-]+): (.*)", raw):
        passed[m.group(1)] = m.group(2)
    else:
        sys.exit(f"ERROR: unsupported line in the include section: {raw!r}")
if path:
    entries.append((path, passed))
if not entries:
    sys.exit("ERROR: the include section named no templates")

inlined = []
for path, passed in entries:
    _, tpl_body = split_spec((root / path).read_text(), path)

    def sub(m, path=path, passed=passed):
        name = m.group(1)
        if name not in passed:
            sys.exit(f"ERROR: {path} reads input {name!r}, which the composition does not pass")
        return passed[name]

    inlined.append(f"# ─── inlined from {path} ───")
    # Limitation: raw text substitution. Safe because no passed value contains a
    # double quote that could close a quoted scalar it lands inside; the
    # regression scenario parses the result, which is what would catch it.
    inlined.append(re.sub(r"\$\[\[ inputs\.([a-z0-9-]+) \]\]", sub, tpl_body).strip("\n"))
    inlined.append("")

# The comment block above `include:` describes the include section, which
# no longer exists here. Drop it with the section it documented.
before = lines[:start]
while before and (before[-1].startswith("#") or not before[-1].strip()):
    before.pop()
before.append("")

out = "\n".join([BANNER + head, "---"] + before + inlined + lines[end:])
sys.stdout.write(out)
PY
)"

if [ "${1:-}" = "--write" ]; then
  printf '%s' "${flat}" > "${OUT}"
  echo "→ wrote ${OUT}"
else
  printf '%s' "${flat}"
fi
