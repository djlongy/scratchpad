# roles/artifactory/filter_plugins/data_shaping.py
# Data-structure transforms for the export pipeline — kept separate from
# yaml_pretty.py, which only formats already-shaped data into pretty YAML.
# These operate on the data (dict in → smaller dict out) and are
# format-agnostic, so they serve both the YAML and JSON export paths.

from __future__ import annotations


def drop_empty(data):
    """Recursively remove mapping keys whose value is "empty" — None, an empty
    string, an empty list, or an empty dict — pruning children first so a map
    that becomes empty after its own keys are dropped is removed too.

    Artifactory returns unset fields as '' (a few as null) and unset
    collections as []/{}; absent == empty == API default on apply, so dropping
    them is lossless for round-trip and keeps As-Built vars small.

    CRITICAL: only None/''/[]/{} count as empty. Falsy-but-meaningful values
    (`false`, `0`, `0.0`) are KEPT — they are real settings, not absence. The
    comparisons below never match a bool/number (e.g. `0 == ''` is False).
    """
    def is_empty(v):
        return v is None or v == '' or v == [] or v == {}

    def prune(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                pv = prune(v)
                if not is_empty(pv):
                    out[k] = pv
            return out
        if isinstance(node, list):
            # Prune inside each element but keep the list's length/order.
            return [prune(v) for v in node]
        return node

    return prune(data)


class FilterModule(object):
    def filters(self):
        return {'drop_empty': drop_empty}
