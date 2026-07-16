"""打包元数据结构校验:LICENSE / pyproject(纯代码分支:文档类校验已随文档移除)。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def test_license_file_is_mit_plain_text():
    # OSI 许可证纯文本文件(法律必需,纯代码分支亦保留)
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


def test_pyproject_uses_dynamic_version():
    """版本号单一事实来源:pyproject 动态取包 __version__(防硬编码回归)。"""
    from vcstudio import __version__

    pyproject = _read('pyproject.toml')
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "vcstudio.__version__"}' in pyproject
    assert __version__  # 包可 import 且有版本号
