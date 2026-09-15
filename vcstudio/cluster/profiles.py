"""集群连接 profile 模型 + clusters.yaml 读写(M2 集群区前置)。

安全底线:模型**不含密码字段**,故 yaml 天然不可能写出密码;密码只走 keyring
(见 vcstudio.shared.secrets)。字段对应 DPDispatcher 的 Machine 连接部分。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict, field, fields as dc_fields
from pathlib import Path

import yaml

from vcstudio.shared.config import user_config_dir


@dataclass
class ClusterProfile:
    """一个集群的连接+提交配置(不含密码)。auth ∈ {'key','password'}。"""
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
    scheduler: str = 'Slurm'     # 完整支持 Slurm | PBS；探测层可识别 LSF/Shell 但不可提交
    scheduler_bin: str = ''      # 调度器命令目录(如 1w 的 /opt/torque-6.1.2/bin;空=走 PATH)
    # ── S2 资源参数(自动脚本轨) ──
    queue: str = ''              # 队列/分区
    nodes: int = 1
    ppn: int = 0                 # 每节点核数(0=未设置)
    walltime: str = '24:00:00'
    env_lines: list = field(default_factory=list)   # module load / source …(逐行)
    # 旧版 VASP 字段保留为兼容入口。其它引擎不得回退使用它，
    # 否则 CP2K/Gaussian/CASTEP 作业会在集群上误跑 vasp_std。
    vasp_cmd: str = ''           # 完整 VASP 执行行(mpirun/srun …)
    engine_commands: dict = field(default_factory=dict)
    # 按引擎分开的运行命令，如 {'cp2k': 'cp2k.psmp -i {input} -o {stem}.out'}
    # ── S2 提交脚本双轨 ──
    script_mode: str = 'auto'    # 'auto'(参数生成) | 'template'(用户模板透传)
    template_path: str = ''      # 用户模板本地路径(仅路径;内容逐字复用,只填占位符)


def default_clusters_path() -> Path:
    """默认 clusters.yaml 路径:%APPDATA%/vcstudio/clusters.yaml。"""
    return user_config_dir() / 'clusters.yaml'


def load_profiles(path: str | os.PathLike | None = None) -> dict:
    """读 clusters.yaml → {name: ClusterProfile}。文件不存在/损坏/形状不对 → {}。

    GUI 各页 __init__ 同步调用本函数:任何解析问题都兜成空表(同 ledger 兜底口径),
    绝不让一份坏配置拖死启动。
    """
    target = Path(path) if path is not None else default_clusters_path()
    if not target.is_file():
        return {}
    try:
        with open(target, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return {}
    clusters = data.get('clusters') if isinstance(data, dict) else None
    if not isinstance(clusters, dict):
        return {}
    known = {f.name for f in dc_fields(ClusterProfile)}
    out: dict = {}
    for name, d in clusters.items():
        if d is None:
            d = {}
        if not isinstance(d, dict):
            continue                     # 单条目损坏只跳过该条,不拖累其余集群
        # 只取已知字段:老 yaml 缺新字段 → 走默认;未来版本多出的字段 → 忽略不炸(前后兼容)
        fields = {k: v for k, v in d.items() if k != 'name' and k in known}
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
