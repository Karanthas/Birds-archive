import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner

from archive.tests.runner import NonEmptyDiscoverRunner


class RunnerGateTests(SimpleTestCase):
    def test_executed_nonempty_suite_passes(self):
        class PassingCase(unittest.TestCase):
            def runTest(self):
                self.assertEqual(2 + 2, 4)
        runner = NonEmptyDiscoverRunner(verbosity=0)
        runner.test_runner = lambda **kwargs: unittest.TextTestRunner(stream=io.StringIO(), **kwargs)
        suite = unittest.TestSuite([PassingCase()])
        result = runner.run_suite(suite)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [])
        self.assertEqual(runner.suite_result(suite, result), 0)

    def test_empty_suite_is_rejected(self):
        with mock.patch.object(DiscoverRunner, 'build_suite', return_value=unittest.TestSuite()):
            with self.assertRaisesMessage(RuntimeError, '测试集为空'):
                NonEmptyDiscoverRunner(verbosity=0).build_suite([])

    def test_real_runner_subprocess_exits_nonzero_for_unexpected_skip(self):
        script = """
import django
import sys
import unittest
django.setup()
from archive.tests.runner import NonEmptyDiscoverRunner
class ProbeCase(unittest.TestCase):
    @unittest.skip('controlled unexpected skip')
    def test_required_behavior(self):
        self.fail('skipped requirement must not pass the gate')
sys.exit(NonEmptyDiscoverRunner(verbosity=1).run_tests(['__main__.ProbeCase']))
"""
        project = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, '-c', script], cwd=project,
            env={'DJANGO_SETTINGS_MODULE': 'config.settings_test'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('skipped=1', result.stderr)
        self.assertIn('有 1 项测试被跳过，拒绝作为通过', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
