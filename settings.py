"""One repository per workspace; independent processes can watch different repos."""
import json
import os
import re
from pathlib import Path


def load_config(path=None):
    path = path or os.environ.get('OBSERVATORY_CONFIG')
    value = json.loads(Path(path).read_text()) if path else {}
    repository = value.setdefault('repository', 'sgl-project/sglang')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository must be owner/name')
    value.setdefault('workflow', 'pr-test.yml')
    value.setdefault('workflow_event', 'pull_request')
    value.setdefault('default_language', 'zh-CN')
    if value['default_language'] not in ('zh-CN', 'en'):
        raise ValueError('default_language must be zh-CN or en')
    modules = value.get('modules')
    if modules is not None:
        if not isinstance(modules, dict) or not modules:
            raise ValueError('modules must be a nonempty object')
        for key, module in modules.items():
            if not re.fullmatch(r'[a-z][a-z0-9_-]*', key) or key == 'all':
                raise ValueError('Invalid module identifier')
            if not isinstance(module.get('name'), str) or not module['name']:
                raise ValueError('Module name is required')
            re.compile(module['pattern'], re.I)
    return value


CONFIG = load_config()
