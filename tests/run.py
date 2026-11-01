import subprocess
import sys
import unittest

suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
    raise SystemExit(1)
raise SystemExit(subprocess.call([sys.executable, "tests/integration.py"]))
