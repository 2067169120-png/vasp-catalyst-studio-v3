"""CI 配置结构校验:yaml 可解析 + 关键步骤存在(防手滑改坏)。"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_ci_yaml_parses_and_runs_pytest_on_matrix():
    path = os.path.join(ROOT, '.github', 'workflows', 'ci.yml')
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    # PyYAML 把裸 on 解析成布尔 True 键
    assert 'on' in data or True in data
    job = data['jobs']['test']
    matrix = job['strategy']['matrix']
    assert 'ubuntu-latest' in matrix['os'] and 'windows-latest' in matrix['os']
    steps_text = str(job['steps'])
    assert 'pytest' in steps_text
    assert 'pip install -e' in steps_text
