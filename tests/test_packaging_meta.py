"""发刊要件结构校验:LICENSE / pyproject 元数据。"""
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


def test_contributing_covers_tests_and_issues():
    text = _read('CONTRIBUTING.md')
    assert 'pytest' in text
    assert 'issue' in text.lower()
