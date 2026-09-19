"""Workspace isolation, language boundaries, and the optional analysis contract."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import AnalysisResult, analyze_entry, load_analyzer
from assets import render_asset
from localization import translate_data
from settings import load_config
from sync import refresh
from pr_status import read_status

ROOT = Path(__file__).resolve().parents[1]


class ExtensionTests(unittest.TestCase):
    def test_language_does_not_rewrite_source_or_editorial(self):
        value = {'title': '正确性', 'body': '负责人', 'summary': '正确性',
                 'editorial': {'summary': '正确性'}, 'priority_reason': '正确性',
                 'confidence': {'label': '较高', 'evidence': ['提供复现命令、脚本或可执行步骤']}}
        result = translate_data(value)
        for field in ('title', 'body', 'summary', 'editorial'):
            self.assertEqual(result[field], value[field])
        self.assertEqual(result['priority_reason'], 'Correctness')
        self.assertEqual(result['confidence']['label'], 'High')
        self.assertNotRegex(result['confidence']['evidence'][0], r'[\u3400-\u9fff]')

    def test_english_assets_have_complete_copy_and_valid_javascript(self):
        for name in ('index.html', 'auth.html', 'app.js', 'auth.js'):
            source = (ROOT / 'static' / name).read_text()
            result = render_asset(name, source, 'en')
            self.assertNotRegex(result.replace('中文', ''), r'[\u3400-\u9fff]')
            if name.endswith('.js'):
                check = subprocess.run(['node', '--check'], input=result, text=True, capture_output=True)
                self.assertEqual(check.returncode, 0, check.stderr)

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            for value in ({'repository': 'invalid'}, {'default_language': 'xx'}, {'modules': {'all': {'name': 'All', 'pattern': '.'}}}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_repository_isolation_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'state.json').write_text(json.dumps({'repository': 'other/repo', 'records': {}, 'cursor': None}))
            with self.assertRaisesRegex(ValueError, 'separate data directory'):
                refresh(path)
            (path / 'workflow-status.json').write_text(json.dumps({'repository': 'other/repo', 'entries': {}}))
            with self.assertRaisesRegex(ValueError, 'another repository'):
                read_status(path)

    def test_custom_repository_modules_and_assets(self):
        import os
        config = ROOT / 'configs/example.json'
        code = '''from cacheboard import REPO, classify_modules
from assets import render_asset
from pathlib import Path
assert REPO == 'example/project'
assert classify_modules('Scheduler kernel crash', '') == ['scheduler', 'kernel']
html = render_asset('index.html', Path('static/index.html').read_text(), 'en')
assert 'data-module="scheduler"' in html
assert 'data-module="hicache"' not in html
assert 'example / project' in html
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env={**os.environ, 'OBSERVATORY_CONFIG': str(config)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disabled_analysis_and_result_validation(self):
        self.assertIsNone(load_analyzer(None))
        self.assertIsNone(analyze_entry({}, None))
        with self.assertRaises(ValueError):
            load_analyzer('invalid')
        entry = {'id': 'example/project#1', 'title': 'Crash', 'body': 'Report', 'updated_at': '2026-01-01T00:00:00Z'}
        class LocalAnalyzer:
            def analyze(self, request):
                return AnalysisResult(request.title, 'Manual test evidence', 'P1', 'local', 'none', '1')
        result = analyze_entry(entry, LocalAnalyzer())
        self.assertEqual(result['source_updated_at'], entry['updated_at'])
        self.assertEqual(result['suggested_priority'], 'P1')
        self.assertNotIn('analysis', entry)


if __name__ == '__main__':
    unittest.main()
