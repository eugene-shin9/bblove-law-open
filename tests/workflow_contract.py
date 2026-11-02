"""Read real YAML without repairing workflow values; reusable offline contracts."""
from pathlib import Path
import re
import yaml

class WorkflowLoader(yaml.SafeLoader):
    pass

# YAML 1.2 boolean resolution preserves the literal event key `on`.
WorkflowLoader.yaml_implicit_resolvers = {
    k: [(tag, pattern) for tag, pattern in values if tag != 'tag:yaml.org,2002:bool']
    for k, values in yaml.SafeLoader.yaml_implicit_resolvers.items()}
WorkflowLoader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$', re.I), list('tTfF'))

def load_workflow(path):
    value = yaml.load(Path(path).read_text(encoding='utf-8'), Loader=WorkflowLoader)
    assert isinstance(value, dict), 'workflow must be a mapping'
    return value

def job(wf, name):
    return wf['jobs'][name]

def steps(job):
    return job['steps']

def _step(job, key, value):
    matches = [s for s in steps(job) if s.get(key) == value]
    assert len(matches) == 1, (key, value, len(matches))
    return matches[0]

def step_by_name(job, name):
    return _step(job, 'name', name)

def step_by_uses(job, uses):
    return _step(job, 'uses', uses)

def step_by_id(job, id):
    return _step(job, 'id', id)

def expressions(text):
    return [m.strip() for m in re.findall(r'\$\{\{(.*?)\}\}', text, re.S)]

def strings(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from strings(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings(child, path + (index,))
    elif isinstance(value, str):
        yield path, value

def env_refs(step):
    return [(path, name) for path, text in strings(step)
            for expr in (expressions(text) or ([text] if path[-1:] == ('if',) else []))
            for name in re.findall(r'\benv\.([A-Za-z_][A-Za-z_0-9]*)', expr)]

def inputs_refs(wf):
    return [(path, name) for path, text in strings(wf)
            for name in re.findall(r'\binputs\.([A-Za-z_][A-Za-z_0-9]*)', text)]

def assert_step_envs(wf):
    for name, j in wf['jobs'].items():
        for index, s in enumerate(steps(j)):
            if 'env' in s:
                assert isinstance(s['env'], dict) and s['env'], (name, index, 'empty env')
                assert all(v is not None and v != '' for v in s['env'].values()), (name, index, 'empty env value')

def assert_inputs_only_job_env(wf):
    for path, name in inputs_refs(wf):
        assert len(path) == 4 and path[0] == 'jobs' and path[2] == 'env', (path, name)
        assert path[3] == name.upper(), (path, name)
