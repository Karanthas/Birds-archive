import json
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


class ProductionTimingTests(SimpleTestCase):
    def test_production_uses_positive_millisecond_parameters(self):
        project = Path(__file__).resolve().parents[2]
        script = """
import json
import config.settings as settings
print(json.dumps([settings.SCAN_INTERVAL_MS, settings.STABLE_WINDOW_MS,
                  settings.BIRDS_SCAN_SECONDS, settings.BIRDS_STABILITY_SECONDS]))
"""
        with tempfile.TemporaryDirectory(prefix='birds-settings-') as temporary:
            root = Path(temporary)
            for scan, stable in ((1250, 4750), (10000, 30000)):
                with self.subTest(scan=scan, stable=stable):
                    result = subprocess.run([sys.executable, '-c', script], cwd=project,
                        env={'BIRDS_INBOX_ROOT': str(root / 'inbox'),
                             'BIRDS_DATA_ROOT': str(root / 'data'),
                             'SCAN_INTERVAL_MS': str(scan), 'STABLE_WINDOW_MS': str(stable),
                             'BIRDS_SCAN_SECONDS': '999', 'BIRDS_STABILITY_SECONDS': '0'},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), [scan, stable, scan / 1000, stable / 1000])

    def test_input_handoff_documents_production_milliseconds(self):
        document = (Path(__file__).resolve().parents[2] / 'docs/input-handoff.md').read_text()
        self.assertIn('SCAN_INTERVAL_MS=10000', document)
        self.assertIn('STABLE_WINDOW_MS=30000', document)
        self.assertIn('毫秒', document)
        self.assertNotIn('`BIRDS_SCAN_SECONDS`', document)
        self.assertNotIn('`BIRDS_STABILITY_SECONDS`', document)
