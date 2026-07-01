"""集群连接 profile 模型 + clusters.yaml 读写(M2 集群区前置)。

安全底线:模型**不含密码字段**,故 yaml 天然不可能写出密码;密码只走 keyring
(见 vcstudio.shared.secrets)。字段对应 DPDispatcher 的 Machine 连接部分。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

from vcstudio.shared.config import user_config_dir


@dataclass
class ClusterProfile:
    """一个集群的连接配置(不含密码)。auth ∈ {'key','password'}。"""
    name: str
    hostname: str = ''
    port: int = 22
    username: str = ''
    auth: str = 'key'            # 'key' | 'password'
    key_path: str = ''           # 私钥文件路径(仅路径,绝不存内容)
    use_jump: bool = False
    jump_host: str = ''
    jump_user: str = ''
    jump_port: int = 22
    remote_root: str = ''
    scheduler: str = 'Slurm'     # Slurm | PBS | LSF | Shell


def default_clusters_path() -> Path:
    """默认 clusters.yaml 路径:%APPDATA%/vcstudio/clusters.yaml。"""
    return user_config_dir() / 'clusters.yaml'


def load_profiles(path: str | os.PathLike | None = None) -> dict:
    """读 clusters.yaml → {name: ClusterProfile}。文件不存在 → {}。"""
    target = Path(path) if path is not None else default_clusters_path()
    if not target.is_file():
        return {}
    with open(target, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    out: dict = {}
    for name, d in (data.get('clusters') or {}).items():
        fields = {k: v for k, v in (d or {}).items() if k != 'name'}
        out[name] = ClusterProfile(name=name, **fields)
    return out


def save_profiles(profiles: dict, path: str | os.PathLike | None = None) -> Path:
    """把 {name: ClusterProfile} 写入 clusters.yaml(UTF-8)。返回写入路径。"""
    target = Path(path) if path is not None else default_clusters_path()
    out = {'clusters': {}}
    for name, p in profiles.items():
        d = asdict(p)
        d.pop('name', None)
        out['clusters'][name] = d
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w', encoding='utf-8') as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)
    return target
