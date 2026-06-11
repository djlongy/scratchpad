# roles/artifactory/filter_plugins/yaml_pretty.py
# to_pretty_yaml — to_nice_yaml, plus two readability tweaks for very large
# exports (the artifactory state file easily exceeds 5000 lines):
#
#   1. Block sequences are INDENTED under their parent key:
#          artifactory_users:
#            - name: alice
#      instead of PyYAML's default "indentless" style where the hyphen sits at
#      the parent key's column. This cannot be done with to_nice_yaml args or
#      Jinja — PyYAML only honours it via a Dumper.increase_indent override.
#
#   2. gap_depth=N inserts a blank line between sibling nodes (mapping keys
#      and list entries) at nesting depths 1..N, so the document reads as
#      logically separated blocks:
#        gap_depth=0  no blank lines (plain to_nice_yaml layout)
#        gap_depth=1  blank line between root keys only (default)
#        gap_depth=2  …plus between the children of each root key — second-
#                     level mapping keys and top-level list entries
#        gap_depth=3+ …and so on, one level deeper per increment
#      Implemented by dumping each sibling subtree separately and joining with
#      blank lines (never by regexing emitted lines), so multi-line string
#      scalars that happen to contain "key:"-shaped text can't be mangled.
#      Blank lines are insignificant in YAML — the file re-imports identically.

from __future__ import annotations

import yaml

from ansible.errors import AnsibleFilterError
from ansible.module_utils.common.text.converters import to_text

try:
    from ansible.parsing.yaml.dumper import AnsibleDumper
except ImportError:  # path moved in newer ansible-core
    AnsibleDumper = None


# IMPORTANT: base on the pure-Python SafeDumper, NOT AnsibleDumper. When PyYAML
# is compiled with libyaml, AnsibleDumper extends CSafeDumper whose emitter
# runs in C — increase_indent() is never called and the override is silently
# ignored (observed on ansible-core 2.16: hyphens stay at the parent column).
# The pure-Python emitter always honours it; speed is irrelevant at this size.
class IndentedDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        # indentless=False is the whole trick: never emit indentless sequences.
        return super(IndentedDumper, self).increase_indent(flow, False)


# Graft Ansible's representers (AnsibleUnsafeText, vaulted strings, …) onto the
# pure-Python dumper so live API data (uri results) still serializes cleanly.
if AnsibleDumper is not None:
    for _type, _repr in AnsibleDumper.yaml_representers.items():
        IndentedDumper.add_representer(_type, _repr)


def to_pretty_yaml(data, indent=2, width=200, gap_depth=1, sort_keys=True):
    try:
        gap_depth = int(gap_depth)

        def plain(node):
            return to_text(yaml.dump(
                node,
                Dumper=IndentedDumper,
                indent=indent,
                width=width,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=sort_keys,
            ))

        def reindent(text, levels):
            pad = ' ' * (indent * levels)
            return ''.join(
                (pad + ln if ln.strip() else ln) + '\n'
                for ln in text.splitlines()
            )

        def bullet(text):
            # turn a column-0 block into a "- " list entry at column 0
            pad = ' ' * indent
            lines = text.splitlines()
            out = ['-' + ' ' * (indent - 1) + lines[0]]
            out.extend(pad + ln if ln.strip() else ln for ln in lines[1:])
            return '\n'.join(out) + '\n'

        def key_header(k):
            # serialize just the key (with correct quoting): "<key>: {}" → "<key>:"
            line = plain({k: {}}).rstrip('\n')
            return line[:line.rindex(': {}')] + ':'

        def render(node, depth):
            # Emit `node` at column 0 with blank lines between its children
            # when their depth (= `depth`) is within gap_depth.
            if depth > gap_depth or not isinstance(node, (dict, list)) or not node:
                return plain(node)
            parts = []
            if isinstance(node, dict):
                for k in (sorted(node, key=str) if sort_keys else list(node)):
                    v = node[k]
                    if isinstance(v, (dict, list)) and v and depth < gap_depth:
                        body = reindent(render(v, depth + 1), 1)
                        parts.append(key_header(k) + '\n' + body.rstrip('\n'))
                    else:
                        parts.append(plain({k: v}).rstrip('\n'))
            else:
                for e in node:
                    if isinstance(e, (dict, list)) and e and depth < gap_depth:
                        parts.append(bullet(render(e, depth + 1)).rstrip('\n'))
                    else:
                        parts.append(plain([e]).rstrip('\n'))
            return '\n\n'.join(parts) + '\n'

        return render(data, 1)
    except Exception as exc:
        raise AnsibleFilterError('to_pretty_yaml: %s' % exc)


class FilterModule(object):
    def filters(self):
        return {'to_pretty_yaml': to_pretty_yaml}
