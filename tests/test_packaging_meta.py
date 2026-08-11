"""发刊要件结构校验:LICENSE / pyproject 元数据。"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def test_license_file_is_mit_plain_text():
    # JOSS 硬性要求:OSI 许可证纯文本文件(README 里提名字不算)
    text = _read('LICENSE')
    assert 'MIT License' in text
    assert 'Copyright' in text
    assert 'WITHOUT WARRANTY' in text.upper()


def test_pyproject_declares_license_and_metadata():
    text = _read('pyproject.toml')
    assert 'license' in text
    assert 'MIT' in text
    assert 'classifiers' in text
    assert 'License :: OSI Approved :: MIT License' in text


def test_citation_cff_parses_with_required_keys():
    import yaml
    data = yaml.safe_load(_read('CITATION.cff'))
    assert data['cff-version']
    assert data['title'] == 'VASP Catalyst Studio'
    assert data['authors'] and data['version'] and data['message']


def test_version_single_source_of_truth():
    """版本号单一事实来源:pyproject 动态取包 __version__,CITATION 与之对齐。

    防回归:历史上 __init__(2.0.0)/pyproject(0.1.0)/CITATION(0.1.0) 三处打架,
    产出物署名(manifest created_by)与发布元数据自相矛盾。此后三者必须一致。
    """
    import yaml

    from vcstudio import __version__

    pyproject = _read('pyproject.toml')
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "vcstudio.__version__"}' in pyproject

    cff = yaml.safe_load(_read('CITATION.cff'))
    assert str(cff['version']) == __version__, (
        f"CITATION.cff version={cff['version']!r} 与 "
        f"vcstudio.__version__={__version__!r} 不一致")


def test_v4_identity_is_consistent_while_unreleased():
    """V4 源码/CFF/UI 口径一致；未发布版本不得虚构发布日期。"""
    import yaml

    from vcstudio import __version__

    assert __version__ == '4.0.0'

    cff = yaml.safe_load(_read('CITATION.cff'))
    assert str(cff['version']) == __version__
    assert 'date-released' not in cff

    zh = json.loads(_read('vcstudio', 'shared', 'locales', 'zh.json'))
    en = json.loads(_read('vcstudio', 'shared', 'locales', 'en.json'))
    assert zh['app.subtitle'] == 'v4.0 · 证据驱动催化研究工作台'
    assert en['app.subtitle'] == 'v4.0 · Evidence-driven Catalysis Workbench'
    assert (
        'data-i18n="app.subtitle">v4.0 · 证据驱动催化研究工作台</span>'
        in _read('vcstudio', 'gui_web', 'assets', 'index.html')
    )

    changelog = _read('CHANGELOG.md')
    unreleased = '## [Unreleased] — V4.0.0'
    assert changelog.count(unreleased) == 1
    assert changelog.index(unreleased) < changelog.index('## v3.3.0')


def test_contributing_covers_tests_and_issues():
    text = _read('CONTRIBUTING.md')
    assert 'pytest' in text
    assert 'issue' in text.lower()
