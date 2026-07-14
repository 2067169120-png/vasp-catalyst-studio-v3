"""pytest 公共夹具。

核心防线:把所有默认用户级配置路径隔离进 per-test tmp 目录,杜绝测试污染真实
%APPDATA%/vcstudio(registry/ledger/clusters/known_hosts/config.yaml)——
否则任何没显式 stub 的用例都会往真实用户目录写幽灵项目/作业,GUI 里就会冒出来。
"""
from __future__ import annotations

import importlib

import pytest

# 下列模块均以 `from vcstudio.shared.config import user_config_dir` 各自持有独立引用,
# 光改源模块不够——它们 default_*_path() 里调的是自己命名空间那个名字,必须逐个改绑。
_USER_CONFIG_DIR_SEAMS = (
    'vcstudio.shared.config',       # 源:user_config_path / writable_config_path 内部也用它
    'vcstudio.project.adsorption',  # default_registry_path() → projects.json
    'vcstudio.cluster.ledger',      # default_ledger_path() → jobs.json
    'vcstudio.cluster.profiles',    # default_clusters_path() → clusters.yaml
    'vcstudio.cluster.ssh_test',    # default_known_hosts_path() → known_hosts
)


@pytest.fixture(autouse=True)
def _isolate_user_config_dir(tmp_path, monkeypatch):
    """全套默认用户路径隔离进 per-test tmp,防测试污染真实 %APPDATA%/vcstudio。

    对每个持有 user_config_dir 引用的模块逐个改绑;模块导入失败(缺可选依赖)则跳过,
    保证夹具本身足够健壮不拖垮整套。
    """
    d = tmp_path / 'vcstudio_cfg'
    d.mkdir(exist_ok=True)
    for mod_name in _USER_CONFIG_DIR_SEAMS:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        if hasattr(mod, 'user_config_dir'):
            monkeypatch.setattr(mod, 'user_config_dir', lambda: d)
    yield
