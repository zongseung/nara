import subprocess
import sys
import unittest


class CLIGuards(unittest.TestCase):
    def test_shadow_rejects_nonzero_fallback_before_reading(self):
        result = subprocess.run([sys.executable, 'compare_file.py', 'missing.xls', '--mode', 'shadow', '--fallback-band', '.3'], check=False, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fallback', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_shadow_rejects_diagnostic_source(self):
        result = subprocess.run([sys.executable, 'compare_file.py', 'missing.xls', '--mode', 'shadow', '--catalog-db', 'missing.db'], check=False, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('suggest', result.stderr)

    def test_module_commands_are_available(self):
        result = subprocess.run([sys.executable, '-m', 'src.matching.cli', '--help'], check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('index', 'labels-import', 'review-export', 'review-import', 'train', 'evaluate', 'run', 'status', 'rollback'):
            self.assertIn(command, result.stdout)

    def test_corrupt_status_is_concise(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'ledger.sqlite'
            path.write_bytes(b'not sqlite')
            result = subprocess.run([sys.executable, '-m', 'src.matching.cli', 'status', '--ledger', str(path)], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            self.assertIn('error:', result.stderr)
