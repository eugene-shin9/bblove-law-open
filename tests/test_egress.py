"""Text-level guards for the law.go.kr egress wiring (no PyYAML on the runner)."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = ROOT / '.github' / 'workflows'
ACTION = ROOT / '.github' / 'actions' / 'law-egress' / 'action.yml'
COLLECTORS = {
    'collect-law-all.yml': 'python src/bblove_law_all.py',
    'collect-admrul.yml': 'python src/bblove_admrul.py collect',
    'collect-precedents.yml': 'python scripts/collect_precedents.py',
}
TS_SHA = '064b10558093f38325e838ddea35486bdc6b1d5c'


def triggers(text):
    head = text.split('\njobs:', 1)[0]
    return set(re.findall(r'^  ([a-z_]+):', head.split('\non:', 1)[1].split('\npermissions:', 1)[0], re.M))


class EgressWiring(unittest.TestCase):
    def test_every_uses_is_pinned_or_local(self):
        for path in [*WF.glob('*.yml'), ACTION]:
            for ref in re.findall(r'uses:\s*(\S+)', path.read_text()):
                if ref.startswith('./'):
                    continue
                self.assertRegex(ref, r'@[0-9a-f]{40}$', f'{path.name}: {ref}')

    def test_tailscale_action_pinned_and_userspace_only(self):
        text = ACTION.read_text()
        self.assertIn(f'tailscale/github-action@{TS_SHA}', text)
        self.assertIn('--tun=userspace-networking', text)
        self.assertIn('--outbound-http-proxy-listen=127.0.0.1:1055', text)
        self.assertIn("use-cache: 'false'", text)
        self.assertIn('log-mode: quiet', text)
        self.assertIn('::add-mask::$EXIT_NODE', text)
        self.assertNotIn('tailscale status', text)  # status output would expose tailnet details

    def test_no_privileged_pr_triggers_anywhere(self):
        for path in WF.glob('*.yml'):
            t = triggers(path.read_text())
            self.assertFalse(t & {'pull_request_target', 'workflow_run'}, path.name)

    def test_tailscale_secrets_only_in_main_only_manual_or_scheduled_workflows(self):
        for path in WF.glob('*.yml'):
            text = path.read_text()
            uses_ts = 'TS_CLIENT_ID' in text or 'law-egress' in text
            if not uses_ts:
                continue
            self.assertLessEqual(triggers(text), {'workflow_dispatch', 'schedule'}, path.name)
            self.assertIn("if: github.ref == 'refs/heads/main'", text, path.name)
            self.assertIn('id-token: write', text, path.name)

    def test_collectors_check_egress_first_and_proxy_only_the_collect_step(self):
        for name, command in COLLECTORS.items():
            text = (WF / name).read_text()
            egress = text.index('uses: ./.github/actions/law-egress')
            run = text.index(command)
            self.assertLess(egress, run, name)
            step = text[text.rindex('      - name:', 0, run):run]
            self.assertIn('HTTPS_PROXY: ${{ steps.egress.outputs.https-proxy }}', step, name)
            self.assertEqual(text.count('HTTPS_PROXY'), 1, name)

    def test_guard_workflow_never_touches_tailscale(self):
        text = (WF / 'guard.yml').read_text()
        self.assertNotIn('TS_', text)
        self.assertNotIn('law-egress', text)
        self.assertNotIn('tailscale', text.lower())


if __name__ == '__main__':
    unittest.main()
