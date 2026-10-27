"""Public-repository guards: credential scan, repository allowlist/PII patterns, release-info."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import secret_scan
import repo_guard
import release_info

SECRET = 'synthetic.user_01'


class SecretScanTests(unittest.TestCase):
    def test_finds_plain_encoded_and_case_variants_without_printing(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / 'clean.json').write_text('{"a": 1}')
            (root / 'deep').mkdir()
            (root / 'deep/plain.txt').write_text('x' + SECRET.upper() + 'y')
            (root / 'encoded.txt').write_text('OC=' + 'synthetic.user_01'.replace('_', '%5F'))
            # quote() leaves '_' and '.' unescaped, so '%5F' is not a form a URL would carry;
            # only the upper-case plain copy is a hit.
            self.assertEqual(secret_scan.scan([root], SECRET), 1)
            with patch.dict(os.environ, {'LAW_OC': SECRET}):
                out = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/secret_scan.py'), str(root)],
                                     capture_output=True, text=True, env={**os.environ})
            self.assertEqual(out.returncode, 1)
            self.assertNotIn(SECRET, out.stdout.lower() + out.stderr.lower())
            self.assertNotIn(SECRET.lower(), (out.stdout + out.stderr).lower())

    def test_match_across_chunk_boundary(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'big.bin'
            path.write_bytes(b'a' * (secret_scan.CHUNK - 5) + SECRET.encode() + b'b' * 10)
            self.assertEqual(secret_scan.scan([path], SECRET), 1)

    def test_url_encoded_form_of_special_characters(self):
        secret = 'ab cd@ef'
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / 'a.txt').write_text('OC=ab%20cd%40ef')
            (Path(t) / 'b.txt').write_text('OC=ab+cd%40ef')
            self.assertEqual(secret_scan.scan([t], secret), 2)

    def test_missing_credential_or_paths_fail_closed(self):
        env = {k: v for k, v in os.environ.items() if k != 'LAW_OC'}
        out = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/secret_scan.py'), '.'],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 1)
        out = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/secret_scan.py')],
                             capture_output=True, text=True, env={**env, 'LAW_OC': SECRET})
        self.assertEqual(out.returncode, 1)

    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / 'a.json').write_text('{"law": "국세기본법"}')
            self.assertEqual(secret_scan.scan([t], SECRET), 0)


class RepoGuardTests(unittest.TestCase):
    def test_allowlist_and_personal_patterns(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            files = {
                'src/ok.py': 'x = 1\n',
                'notes/private.md': 'memo\n',
                'scripts/rrn.txt': 'id 900101' + '-1234567\n',
                'scripts/phone.txt': 'call 010' + '-1234-5678\n',
                'scripts/mail.txt': 'me' + '@realmail.co.kr\n',
                'tests/fixture.txt': 'user@example.com and 1@users.noreply.github.com\n',
                'README.md': '법령 수집\n',
            }
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding='utf-8')
            got = sorted(repo_guard.check(root, list(files)))
            self.assertEqual(got, [('notes/private.md', 'path_not_allowed'), ('scripts/mail.txt', 'email'),
                                   ('scripts/phone.txt', 'mobile_phone'), ('scripts/rrn.txt', 'resident_number')])

    def test_this_repository_is_clean(self):
        self.assertEqual(repo_guard.check(ROOT, repo_guard.tracked(ROOT)), [])


class ReleaseInfoTests(unittest.TestCase):
    def test_hashes_every_payload_file_and_excludes_self(self):
        with tempfile.TemporaryDirectory() as t:
            rel = Path(t) / 'release'
            rel.mkdir()
            (rel / 'official-all.zip').write_bytes(b'zip')
            (rel / 'SHA256SUMS').write_text('x')
            manifest = Path(t) / 'manifest.json'
            manifest.write_text(json.dumps({'date': '20261028', 'status': 'COMPLETE', 'status_counts': {'SUCCESS': 1}}))
            info = release_info.build(rel, manifest, 'official-law-raw')
            self.assertEqual(info['format_version'], 1)
            self.assertEqual(list(info['files']), ['official-all.zip'])
            self.assertEqual(info['files']['official-all.zip']['bytes'], 3)
            self.assertEqual(info['collection_status'], 'COMPLETE')


if __name__ == '__main__':
    unittest.main()
