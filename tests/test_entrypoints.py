import subprocess
import sysconfig
import unittest
from importlib.metadata import distribution
from pathlib import Path


class EntryPointTests(unittest.TestCase):
    def test_installed_submit_reuses_qoj_submit_entrypoint(self):
        entries = {entry.name: entry for entry in distribution('qoj-submitter-direct').entry_points
                   if entry.group == 'console_scripts'}
        self.assertEqual(entries['submit'].value, entries['qoj-submit'].value)
        for command in ('qoj-submit', 'submit'):
            result = subprocess.run([str(Path(sysconfig.get_path('scripts')) / command), '--help'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--contest', result.stdout)
            self.assertIn('--dry-run', result.stdout)
