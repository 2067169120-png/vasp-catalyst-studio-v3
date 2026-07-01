"""配置加载:定位并读取 config.yaml,暴露 potcar_lib_root。

定位优先级:①env VCSTUDIO_CONFIG ②cwd/config.yaml ③包根旁 config.yaml。
缺文件 → 回退内置默认。用户 yaml 键覆盖默认,其余键从默认补齐。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# 内置默认:缺 config.yaml 时兜底。potcar_lib_root 指向 E: 现有 PAW_PBE 库(M1;M5 复制入包)。
_DEFAULT = {
    'potcar_lib_root':
        'E:/V2.0.0/results/inputs/potpaw54/potpaw54/potpaw54/potpaw_PBE/paw_pbe',
    'llm': {},
    'ssh': {},
}


def find_config_path() -> Path | None:
    """按 env > cwd > 包旁 顺序定位 config.yaml;都无 → None。

    env VCSTUDIO_CONFIG 为显式覆盖,即便指向不存在文件也直接返回(交由 load_config 兜底)。
    """
    env = os.environ.get('VCSTUDIO_CONFIG')
    if env:
        return Path(env)
    cwd = Path.cwd() / 'config.yaml'
    if cwd.is_file():
        return cwd
    beside = Path(__file__).resolve().parents[2] / 'config.yaml'  # shared → vcstudio → 包根
    if beside.is_file():
        return beside
    return None


def load_config(path: str | os.PathLike | None = None) -> dict:
    """加载配置 dict。path 缺省时按 find_config_path 定位;缺文件 → 内置默认。

    返回值 = 内置默认 与 用户 yaml 的浅合并(用户键优先)。
    """
    resolved = Path(path) if path is not None else find_config_path()
    cfg = dict(_DEFAULT)
    if resolved is not None:
        try:
            with open(resolved, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f) or {}
        except FileNotFoundError:
            loaded = {}
        if isinstance(loaded, dict):
            cfg.update(loaded)
    return cfg


def get_potcar_lib_root(config: dict | None = None) -> str:
    """返回本地 PAW_PBE 库根路径;config 缺省则自动 load_config()。"""
    cfg = config if config is not None else load_config()
    return cfg['potcar_lib_root']
