"""credential_markers flags OC= values in every URL form, including HTML-escaped '&amp;OC='."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bblove_quality import credential_markers


class CredentialMarkerTests(unittest.TestCase):
    def test_url_forms(self):
        for raw in [b'?OC=abc', b'x&OC=abc', b'x&amp;OC=abc', b'%3FOC%3Dabc', b'%26OC=abc']:
            self.assertTrue(credential_markers(raw), raw)

    def test_redacted_and_plain_text(self):
        for raw in [b'?OC=REDACTED', b'&amp;OC=REDACTED', b'OC is a word', b'{"law": "x"}']:
            self.assertFalse(credential_markers(raw), raw)


if __name__ == '__main__':
    unittest.main()
