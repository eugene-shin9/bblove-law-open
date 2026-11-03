"""All live workflows and historical regression fixtures, offline only."""
from pathlib import Path
import unittest
from workflow_contract import load_workflow, assert_step_envs, assert_inputs_only_job_env
ROOT = Path(__file__).resolve().parents[1]
class WorkflowStructureTests(unittest.TestCase):
    def test_all_workflow_step_envs(self):
        paths = list((ROOT / '.github/workflows').glob('*.y*ml'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                assert_step_envs(load_workflow(path))
    def test_empty_env_fixture_rejected(self):
        with self.assertRaises(AssertionError):
            assert_step_envs(load_workflow(ROOT / 'tests/fixtures/workflow_empty_env.yml'))
    def test_scheduled_raw_input_fixture_rejected(self):
        wf = load_workflow(ROOT / 'tests/fixtures/workflow_schedule_inputs.yml')
        self.assertIn('schedule', wf['on'])
        with self.assertRaises(AssertionError):
            assert_inputs_only_job_env(wf)

    def test_public_collection_contracts(self):
        import re
        from workflow_contract import job, steps, step_by_id, strings, inputs_refs
        wf = load_workflow(ROOT / '.github/workflows/collect-precedents.yml')
        self.assertEqual(set(wf['on']), {'workflow_dispatch', 'schedule'})
        self.assertEqual(wf['on']['schedule'], [{'cron': '40 13 * * *'}])
        self.assertEqual(inputs_refs(wf), [])
        j = job(wf, 'collect')
        self.assertEqual(j['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(j['permissions'], {'contents': 'write', 'id-token': 'write', 'attestations': 'write'})
        self.assertIs(wf['concurrency']['cancel-in-progress'], False)
        self.assertEqual({r for _, text in strings(wf) for r in re.findall(r'secrets\.([A-Z_]+)', text)},
                         {'LAW_OC', 'TS_CLIENT_ID', 'TS_AUDIENCE', 'TS_EXIT_NODE'})
        restore = next(s['with'] for s in steps(j) if s.get('uses', '').startswith('actions/cache/restore@'))
        save = next(s['with'] for s in steps(j) if s.get('uses', '').startswith('actions/cache/save@'))
        self.assertEqual(save, {k: restore[k] for k in ('path', 'key')})
        self.assertEqual(restore['restore-keys'].strip(), restore['key'].split('${{ github.run_id }}')[0])
        self.assertEqual(step_by_id(j, 'collect')['env']['LAW_OC'], '${{ secrets.LAW_OC }}')

    def test_helpers_preserve_values_and_reference_locations(self):
        from workflow_contract import job, steps, step_by_name, step_by_uses, step_by_id, expressions, env_refs, inputs_refs
        wf = load_workflow(ROOT / 'tests/fixtures/workflow_schedule_inputs.yml')
        self.assertEqual(inputs_refs(wf), [(('jobs', 'test', 'steps', 0, 'if'), 'mode')])
        self.assertEqual(expressions('x ${{ env.MODE }} y ${{ inputs.mode }}'), ['env.MODE', 'inputs.mode'])
        j = {'steps': [{'name': 'synthetic', 'id': 'fixture', 'uses': 'fixture/action@v1',
                       'if': "env.MODE == 'load'", 'env': {'VALUE': '${{ env.SOURCE }}'}}]}
        for selected in (step_by_name(j, 'synthetic'), step_by_id(j, 'fixture'), step_by_uses(j, 'fixture/action@v1')):
            self.assertIs(selected, steps(j)[0])
        self.assertEqual(env_refs(steps(j)[0]), [(('if',), 'MODE'), (('env', 'VALUE'), 'SOURCE')])
        self.assertIsNone(steps(job(load_workflow(ROOT / 'tests/fixtures/workflow_empty_env.yml'), 'test'))[0]['env'])
        for value in ({}, None, {'VALUE': None}, {'VALUE': ''}):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                assert_step_envs({'jobs': {'fixture': {'steps': [{'env': value}]}}})
