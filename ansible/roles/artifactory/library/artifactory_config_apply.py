#!/usr/bin/python
# -*- coding: utf-8 -*-
# roles/artifactory/library/artifactory_config_apply.py
"""Apply Artifactory system/global config via the YAML PATCH, version-adaptively.

The config descriptor only reads as XML, but the resilient restore path is the
YAML PATCH (PATCH /api/system/configuration, application/yaml). Its accepted
schema is the set of WRITABLE config properties, which is a SUBSET of the XML
descriptor's elements and DRIFTS between Artifactory versions: a field a newer
version accepts an older one rejects with HTTP 400 and one of

    Key "<name>" is not part of the configuration      (newer phrasing)
    Key "<name>" is not a property                      (older phrasing)

So rather than hard-code a per-version schema, this module treats the server as
the oracle: it PATCHes each top-level block, and on such a 400 it DROPS the named
key and retries, until the block applies (200) or nothing droppable remains. The
maximal version-compatible subset is applied, and every dropped/failed key is
reported so cross-version (up/down-grade) restores are transparent.

Returns: changed, applied (blocks that took), dropped ({block: [keys]}),
rejected ({block: message}).  NB: the result key is 'rejected', not 'failed' —
'failed' is reserved by Ansible (a truthy value there marks the task failed).
"""
from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import copy
import json
import re

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url
from ansible.module_utils.common.text.converters import to_text

try:
    import yaml as _yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_HTTP_OK = 200

# 400 messages that name a single offending key we can drop and retry.
_UNKNOWN_KEY_RE = re.compile(
    r'Key "([^"]+)" is not (?:a property|part of the configuration)', re.I)


def _strip_key(node, target):
    """Recursively remove every mapping key == target. Returns count removed."""
    removed = 0
    if isinstance(node, dict):
        if target in node:
            del node[target]
            removed += 1
        for v in node.values():
            removed += _strip_key(v, target)
    elif isinstance(node, list):
        for v in node:
            removed += _strip_key(v, target)
    return removed


def _patch_block(module, url, headers, block, value, max_drops):
    """PATCH one {block: value}, dropping server-rejected keys until it applies.
    Returns (status, dropped_keys, message)."""
    body_val = copy.deepcopy(value)
    dropped = []
    for _ in range(max_drops + 1):
        payload = _yaml.safe_dump({block: body_val}, default_flow_style=False)
        resp, info = fetch_url(
            module, url, data=to_text(payload).encode('utf-8'),
            headers=headers, method='PATCH',
            timeout=module.params['timeout'])
        status = info.get('status', -1)
        if status == _HTTP_OK:
            return _HTTP_OK, dropped, ''
        # read the error body (fetch_url puts it in info['body'] on failure)
        raw = info.get('body')
        if raw is None and resp is not None:
            try:
                raw = resp.read()
            except (OSError, AttributeError):
                raw = b''
        text = to_text(raw or b'')
        # The message is wrapped in JSON ({"errors":[{"message":"Key \"x\" …"}]}),
        # so quotes around the key are backslash-escaped in the raw body. Parse
        # out the clean message before matching.
        msg_text = text
        try:
            errs = json.loads(text).get('errors')
            if errs:
                msg_text = '; '.join(e.get('message', '') for e in errs)
        except (ValueError, AttributeError):
            pass
        m = _UNKNOWN_KEY_RE.search(msg_text)
        if not m:
            return status, dropped, (msg_text or text)[:300]
        bad = m.group(1)
        # don't drop the block key itself (would empty the payload)
        if bad == block:
            return status, dropped, text[:300]
        n = _strip_key(body_val, bad)
        if n == 0 or body_val in ({}, None, [], ''):
            return status, dropped, text[:300]
        dropped.append(bad)
    return status, dropped, f'exceeded max_drops ({max_drops})'


def main():
    module = AnsibleModule(
        argument_spec=dict(
            url=dict(type='str', required=True),
            config=dict(type='dict', required=True),
            token=dict(type='str', default='', no_log=True),
            username=dict(type='str', default=''),
            password=dict(type='str', default='', no_log=True),
            validate_certs=dict(type='bool', default=True),
            timeout=dict(type='int', default=30),
            max_drops=dict(type='int', default=200),
        ),
        supports_check_mode=True,
    )
    if not HAS_YAML:
        module.fail_json(msg="PyYAML is required on the controller for artifactory_config_apply.")

    p = module.params
    cfg = p['config'] or {}
    headers = {'Content-Type': 'application/yaml'}
    if p['token']:
        headers['Authorization'] = 'Bearer ' + p['token']
    elif p['username']:
        creds = f"{p['username']}:{p['password']}"
        basic = base64.b64encode(creds.encode()).decode()
        headers['Authorization'] = 'Basic ' + basic

    applied, dropped, rejected = [], {}, {}

    if module.check_mode:
        module.exit_json(changed=bool(cfg), applied=list(cfg.keys()),
                         dropped={}, rejected={},
                         msg=f"check mode: would apply {len(cfg)} block(s)")

    for block, value in cfg.items():
        status, drops, msg = _patch_block(
            module, p['url'], headers, block, value, p['max_drops'])
        if status == _HTTP_OK:
            applied.append(block)
            if drops:
                dropped[block] = drops
        else:
            rejected[block] = msg

    module.exit_json(
        changed=bool(applied),
        applied=applied,
        dropped=dropped,
        rejected=rejected,
        msg=(f"applied {len(applied)} block(s); "
             f"{len(dropped)} with dropped keys; {len(rejected)} rejected"),
    )


if __name__ == '__main__':
    main()
