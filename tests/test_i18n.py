"""UI i18n guard: plain string values must not contain placeholders.

The t() helper substitutes arguments only for function values; a plain string
with {0} renders literally in the UI (observed bug).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

I18N_FILE = Path(__file__).resolve().parent.parent / "ui" / "i18n.js"


class TestI18nPlaceholders(unittest.TestCase):
    def test_no_plain_placeholder_strings(self):
        text = I18N_FILE.read_text(encoding="utf-8")
        offenders = re.findall(r':\s*"[^"]*\{\d+\}[^"]*"', text)
        self.assertEqual(offenders, [],
                         f"plain strings with placeholders: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
