import importlib

import vcstudio.shared.config as cfgmod


def _reload(monkeypatch, appdata):
    monkeypatch.setenv('APPDATA', str(appdata))
    monkeypatch.delenv('VCSTUDIO_CONFIG', raising=False)
    return importlib.reload(cfgmod)


def test_user_config_path_under_appdata(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    assert m.user_config_path() == tmp_path / 'vcstudio' / 'config.yaml'


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    p = m.save_config({'potcar_lib_root': 'D:/lib', 'llm': {}, 'ssh': {}},
                      m.user_config_path())
    assert p.is_file()
    loaded = m.load_config(p)
    assert loaded['potcar_lib_root'] == 'D:/lib'


def test_set_potcar_lib_root_preserves_other_keys(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    cfgpath = m.user_config_path()
    m.save_config({'potcar_lib_root': 'old', 'llm': {'x': 1}, 'ssh': {}}, cfgpath)
    m.set_potcar_lib_root('E:/newlib', cfgpath)
    loaded = m.load_config(cfgpath)
    assert loaded['potcar_lib_root'] == 'E:/newlib'
    assert loaded['llm'] == {'x': 1}  # 其他键不动


def test_writable_path_prefers_frozen_user_dir(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(m.sys, 'frozen', True, raising=False)
    assert m.writable_config_path() == m.user_config_path()


def test_env_var_overrides_everything(tmp_path, monkeypatch):
    # 注意:editable 安装下"包旁 config.yaml"(仓库根)真实存在,会先于 user 路径命中,
    # 故不测 user 回退(不可控);改测 env 显式覆盖这条最高优先级、可控的路径。
    m = importlib.reload(cfgmod)
    target = tmp_path / 'custom.yaml'
    monkeypatch.setenv('VCSTUDIO_CONFIG', str(target))
    assert m.writable_config_path() == target
    assert m.find_config_path() == target
