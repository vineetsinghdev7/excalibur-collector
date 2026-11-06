import os
import subprocess
import sys
import unittest


class DiscoveryPerformance(unittest.TestCase):
    def test_embedded_asset_does_not_stall_employer_discovery(self):
        code = """
from collector.sources.webscout import fingerprint
page = '<img src="data:image/png;base64,' + 'A' * 200000 + '">'
page += '<a href="https://acme.pinpointhq.com/jobs">Open jobs</a>'
assert fingerprint(page) == ('pinpoint', 'acme')
assert fingerprint('https://ats.rippling.com/fr-FR/second-inc/jobs') == ('rippling', 'second-inc')
"""
        subprocess.run([sys.executable, '-c', code], check=True, timeout=5, env=os.environ.copy())
