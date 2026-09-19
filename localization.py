"""Translate application-owned copy, never GitHub source text."""
import json
import re
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / 'locales/en.json').read_text())
PATTERN = re.compile('|'.join(re.escape(key) for key in sorted(CATALOG, key=len, reverse=True)))
COPY_FIELDS = {'priority_reason', 'meaning', 'label', 'evidence', 'error', 'methodology', 'scope', 'priority', 'confidence', 'updates', 'date_filter'}
EMPTY_SUMMARY = '作者暂未提供详细正文，请在 GitHub 查看讨论与后续补充。'


def translate(text):
    return PATTERN.sub(lambda match: CATALOG[match[0]], text)


def translate_data(value, key=''):
    if isinstance(value, dict):
        return {k: v if k in ('editorial', 'analysis', 'body', 'title', 'explanation') else translate_data(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [translate_data(item, key) for item in value]
    if isinstance(value, str) and (key in COPY_FIELDS or (key == 'summary' and value == EMPTY_SUMMARY)):
        return translate(value)
    return value
