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
#   2. item_gap=True (default) inserts a blank line between top-level keys and
#      between the entries of top-level lists, so each repo/user/permission
#      dict reads as its own paragraph. Blank lines are insignificant in YAML,
#      so the file re-imports identically.

from __future__ import annotations

import yaml

from ansible.errors import AnsibleFilterError
from ansible.module_utils.common.text.converters import to_text
from ansible.parsing.yaml.dumper import AnsibleDumper


class IndentedDumper(AnsibleDumper):
    def increase_indent(self, flow=False, indentless=False):
        # indentless=False is the whole trick: never emit indentless sequences.
        return super(IndentedDumper, self).increase_indent(flow, False)


def to_pretty_yaml(data, indent=2, width=200, item_gap=True, sort_keys=True):
    try:
        text = yaml.dump(
            data,
            Dumper=IndentedDumper,
            indent=indent,
            width=width,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=sort_keys,
        )
    except Exception as exc:
        raise AnsibleFilterError('to_pretty_yaml: %s' % exc)

    text = to_text(text)
    if not item_gap:
        return text

    bullet = ' ' * indent + '- '
    out = []
    prev = ''
    for line in text.splitlines():
        if out:
            if line and not line[0].isspace():
                out.append('')  # blank line before each top-level key
            elif line.startswith(bullet) and prev[:1].isspace():
                # blank line between top-level list items (but not between a
                # parent key and its first item; nested lists are untouched)
                out.append('')
        out.append(line)
        prev = line
    return '\n'.join(out) + '\n'


class FilterModule(object):
    def filters(self):
        return {'to_pretty_yaml': to_pretty_yaml}
