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
#
#   3. gap_blocks_only (default True) refines (2): a blank line goes between two
#      siblings only when at least one of them is a multi-line BLOCK. Adjacent
#      single-line siblings stay packed. This stops scalar-only maps and
#      single-line list items from looking shredded at gap_depth >= 2:
#        artifactory_crowd_config:        artifactory_environments:
#          enableIntegration: false         - name: DEV
#          useDefaultProxy: false           - name: PROD
#      while multi-line objects (a repo, a group) still each get their own
#      paragraph. Set False to gap every sibling regardless (the old behaviour).

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
        return super().increase_indent(flow, False)


# Graft Ansible's representers (AnsibleUnsafeText, vaulted strings, …) onto the
# pure-Python dumper so live API data (uri results) still serializes cleanly.
if AnsibleDumper is not None and hasattr(AnsibleDumper, "yaml_representers"):
    for _type, _repr in AnsibleDumper.yaml_representers.items():
        IndentedDumper.add_representer(_type, _repr)
else:
    # ansible-core 2.20 turned AnsibleDumper into a factory function with no
    # class-level representers (the old grafting raised AttributeError, not
    # ImportError, so it was not caught above). 2.20 also wraps filter inputs in
    # lazy/tagged proxy SUBCLASSES of dict/list/str, which SafeDumper's exact-type
    # representers miss ("cannot represent an object"). Fall back to representing
    # each proxy as its plain equivalent so live data still serializes.
    IndentedDumper.add_multi_representer(
        str, lambda dumper, data: dumper.represent_str(str(data)))
    IndentedDumper.add_multi_representer(
        dict, lambda dumper, data: dumper.represent_dict(dict(data)))
    IndentedDumper.add_multi_representer(
        list, lambda dumper, data: dumper.represent_list(list(data)))


class _GappedRenderer:
    """Render a tree to indented YAML with blank-line gaps between siblings down
    to `gap_depth`. Splitting the closures of the old to_pretty_yaml() into
    methods keeps each unit simple (and well under Sonar's cognitive-complexity
    limit) while preserving the exact output."""

    def __init__(self, indent, width, gap_depth, gap_blocks_only, sort_keys):
        self.indent = indent
        self.width = width
        self.gap_depth = gap_depth
        self.gap_blocks_only = gap_blocks_only
        self.sort_keys = sort_keys

    def _plain(self, node):
        return to_text(yaml.dump(
            node,
            Dumper=IndentedDumper,
            indent=self.indent,
            width=self.width,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=self.sort_keys,
        ))

    def _reindent(self, text, levels):
        pad = ' ' * (self.indent * levels)
        return ''.join(
            (pad + ln if ln.strip() else ln) + '\n'
            for ln in text.splitlines()
        )

    def _bullet(self, text):
        # turn a column-0 block into a "- " list entry at column 0
        pad = ' ' * self.indent
        lines = text.splitlines()
        out = ['-' + ' ' * (self.indent - 1) + lines[0]]
        out.extend(pad + ln if ln.strip() else ln for ln in lines[1:])
        return '\n'.join(out) + '\n'

    def _key_header(self, key):
        # serialize just the key (with correct quoting): "<key>: {}" → "<key>:"
        line = self._plain({key: {}}).rstrip('\n')
        return line[:line.rindex(': {}')] + ':'

    def _join(self, parts):
        # Concatenate sibling blocks, deciding the separator per-boundary.
        # gap_blocks_only: blank line only when a multi-line block is on either
        # side; two single-line siblings stay packed. Otherwise: always a blank
        # line (every sibling its own paragraph).
        out = ''
        for i, part in enumerate(parts):
            if i:
                block = ('\n' in parts[i - 1]) or ('\n' in part)
                out += '\n\n' if (block or not self.gap_blocks_only) else '\n'
            out += part
        return out + '\n'

    def _render_dict(self, node, depth):
        parts = []
        for key in (sorted(node, key=str) if self.sort_keys else list(node)):
            value = node[key]
            if isinstance(value, (dict, list)) and value and depth < self.gap_depth:
                body = self._reindent(self.render(value, depth + 1), 1)
                parts.append(self._key_header(key) + '\n' + body.rstrip('\n'))
            else:
                parts.append(self._plain({key: value}).rstrip('\n'))
        return parts

    def _render_list(self, node, depth):
        parts = []
        for entry in node:
            if isinstance(entry, (dict, list)) and entry and depth < self.gap_depth:
                parts.append(self._bullet(self.render(entry, depth + 1)).rstrip('\n'))
            else:
                parts.append(self._plain([entry]).rstrip('\n'))
        return parts

    def render(self, node, depth):
        # Emit `node` at column 0 with blank lines between its children when
        # their depth (= `depth`) is within gap_depth.
        if depth > self.gap_depth or not isinstance(node, (dict, list)) or not node:
            return self._plain(node)
        parts = (self._render_dict(node, depth) if isinstance(node, dict)
                 else self._render_list(node, depth))
        return self._join(parts)


def to_pretty_yaml(data, indent=2, width=200, gap_depth=1,
                   gap_blocks_only=True, sort_keys=False):
    try:
        renderer = _GappedRenderer(
            indent, width, int(gap_depth), bool(gap_blocks_only), sort_keys)
        return renderer.render(data, 1)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise AnsibleFilterError(f"to_pretty_yaml: {exc}") from exc


class FilterModule(object):
    def filters(self):
        return {'to_pretty_yaml': to_pretty_yaml}
