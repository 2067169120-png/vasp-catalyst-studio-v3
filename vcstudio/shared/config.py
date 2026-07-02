"""配置加载:定位并读取 config.yaml,暴露 potcar_lib_root。

定位优先级:①env VCSTUDIO_CONFIG ②cwd/config.yaml ③包根旁 config.yaml。
缺文件 → 回退内置默认。用户 yaml 键覆盖默认,其余键从默认补齐。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys
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
    user = user_config_path()
    if user.is_file():
        return user
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


def user_config_dir() -> Path:
    """用户级配置目录:Windows %APPDATA%/vcstudio,否则 ~/.config/vcstudio。"""
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    return Path(base) / 'vcstudio'


def user_config_path() -> Path:
    """用户级 config.yaml 路径(冻结版可写落点)。"""
    return user_config_dir() / 'config.yaml'


def writable_config_path() -> Path:
    """决定写配置落到哪:env 显式 > 冻结用用户目录 > 已有文件原地 > 用户目录兜底。"""
    env = os.environ.get('VCSTUDIO_CONFIG')
    if env:
        return Path(env)
    if getattr(sys, 'frozen', False):
        return user_config_path()
    found = find_config_path()
    if found is not None:
        return found
    return user_config_path()


def save_config(cfg: dict, path: str | os.PathLike | None = None) -> Path:
    """把配置 dict 写入 yaml(UTF-8)。path 缺省用 writable_config_path()。"""
    target = Path(path) if path is not None else writable_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return target


def set_potcar_lib_root(path_str: str, config_path: str | os.PathLike | None = None) -> Path:
    """只更新 potcar_lib_root 并持久化,其余键原样保留。返回写入路径。"""
    resolved = Path(config_path) if config_path is not None else writable_config_path()
    cfg = load_config(resolved)
    cfg['potcar_lib_root'] = path_str
    return save_config(cfg, resolved)


# ── UI 状态记忆(跨会话回填最近使用的路径等;与科学配置无关) ─────────────────────
def get_ui_state(config: dict | None = None) -> dict:
    """返回 config 的 'ui' 小节(dict);缺失 → {}。"""
    cfg = config if config is not None else load_config()
    ui = cfg.get('ui')
    return dict(ui) if isinstance(ui, dict) else {}


def set_ui_state(config_path: str | os.PathLike | None = None, **kv) -> Path:
    """合并更新 'ui' 小节并持久化(None 值跳过,不清除既有键)。返回写入路径。"""
    resolved = Path(config_path) if config_path is not None else writable_config_path()
    cfg = load_config(resolved)
    ui = cfg.get('ui')
    ui = dict(ui) if isinstance(ui, dict) else {}
    ui.update({k: v for k, v in kv.items() if v is not None})
    cfg['ui'] = ui
    return save_config(cfg, resolved)
