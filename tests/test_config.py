"""shared/config.py:定位/加载配置 + potcar_lib_root 访问。"""
from pathlib import Path

from vcstudio.shared import config


def _write_yaml(path, text):
    path.write_text(text, encoding='utf-8')


def test_load_config_reads_explicit_yaml(tmp_path):
    p = tmp_path / 'config.yaml'
    _write_yaml(p, 'potcar_lib_root: "/some/lib"\n')
    cfg = config.load_config(str(p))
    assert cfg['potcar_lib_root'] == '/some/lib'


def test_load_config_missing_file_falls_back_to_default():
    cfg = config.load_config('/no/such/config.yaml')
    assert isinstance(cfg.get('potcar_lib_root'), str) and cfg['potcar_lib_root']


def test_load_config_merges_user_over_default(tmp_path):
    # 用户 yaml 只覆盖 potcar_lib_root;llm/ssh 仍从内置默认补齐
    p = tmp_path / 'config.yaml'
    _write_yaml(p, 'potcar_lib_root: "/x"\n')
    cfg = config.load_config(str(p))
    assert cfg['potcar_lib_root'] == '/x'
    assert 'llm' in cfg and 'ssh' in cfg


def test_get_potcar_lib_root_from_explicit_config():
    assert config.get_potcar_lib_root({'potcar_lib_root': '/foo/bar'}) == '/foo/bar'


def test_find_config_path_env_wins(tmp_path, monkeypatch):
    p = tmp_path / 'myconfig.yaml'
    _write_yaml(p, 'potcar_lib_root: "/env/lib"\n')
    monkeypatch.setenv('VCSTUDIO_CONFIG', str(p))
    assert config.find_config_path() == Path(str(p))


def test_find_config_path_cwd_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv('VCSTUDIO_CONFIG', raising=False)
    p = tmp_path / 'config.yaml'
    _write_yaml(p, 'potcar_lib_root: "/cwd/lib"\n')
    monkeypatch.chdir(tmp_path)
    assert config.find_config_path() == p
