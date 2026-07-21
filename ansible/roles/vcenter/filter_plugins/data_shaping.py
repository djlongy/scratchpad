# roles/vcenter/filter_plugins/data_shaping.py
# drop_empty — omit null / '' / [] / {} recursively (export noise reduction).
# Falsy-but-real values (false, 0) are kept. Same contract as artifactory.

from __future__ import annotations


def drop_empty(value):
    """Recursively drop keys/items whose value is empty (null, '', [], {})."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            cleaned = drop_empty(child)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        out = []
        for child in value:
            cleaned = drop_empty(child)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            out.append(cleaned)
        return out
    return value


class FilterModule:
    def filters(self):
        return {"drop_empty": drop_empty}
