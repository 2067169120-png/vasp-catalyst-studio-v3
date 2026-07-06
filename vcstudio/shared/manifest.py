"""job.yaml 任务清单(manifest):每个作业目录一份,贯穿 生成→提交→监控→修复→分析。

设计原则(与全局不变式一致):
- **显式状态**:state 只能取 VALID_STATES;每次变更追加 state_history(可审计)。
- **溯源**:记录 POSCAR sha256、INCAR 来源(原文透传/含补全/关闭校验)、补全项与警告。
- **纯记录不执行**:本模块只读写 yaml,不做任何远程/调度动作(那是 M2 的事)。
- 原子写(tmp + os.replace),防止监控进程读到半个文件。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import yaml

from vcstudio import __version__

SCHEMA_VERSION = 1
MANIFEST_NAME = 'job.yaml'

# 状态机:生成期 CREATED;M2 起 UPLOADED/SUBMITTED/QUEUED/RUNNING;
# 终态 DONE/FAILED/UNCONVERGED;规则未命中交人工 NEEDS_HUMAN。
VALID_STATES = (
    'CREATED', 'UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING',
    'DONE', 'FAILED', 'UNCONVERGED', 'NEEDS_HUMAN',
)

# 任务类型:M1 阶段吸附能研究以 relax 为主;后续类型链(static/dos/band/freq/neb)按此扩展。
KNOWN_TASK_TYPES = ('relax', 'static', 'dos', 'band', 'freq', 'neb', 'aimd')


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def sha256_file(path: str | os.PathLike) -> str:
    """文件 sha256(溯源:结果对应哪份输入)。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def incar_source_label(validate: bool, completions: dict | None) -> str:
    """INCAR 来源标签:no_validate=严格照抄;有补全项=user+completion;否则原文透传。"""
    if not validate:
        return 'user_no_validate'
    return 'user+completion' if completions else 'user_verbatim'


def new_manifest(*, job_id: str, system: str, task_type: str, calc_type: str,
                 inputs: dict, warnings: list | None = None) -> dict:
    """构造一份新 manifest dict(state=CREATED)。"""
    now = _now_iso()
    return {
        'schema': SCHEMA_VERSION,
        'job_id': job_id,
        'system': system,
        'task_type': task_type,
        'calc_type': calc_type,
        'created_at': now,
        'created_by': f'vcstudio {__version__}',
        'inputs': inputs,
        'cluster': None,            # M2:提交到哪个 ClusterProfile
        'remote_dir': None,         # M2:远程作业目录
        'scheduler_job_id': None,   # M2:qsub/sbatch 返回的作业号
        'state': 'CREATED',
        'state_history': [{'state': 'CREATED', 'at': now}],
        'attempts': [],             # M2/M3.5:每次提交/修复一条记录
        'results': {},              # M4:能量/收敛位等回填
        'warnings': list(warnings or []),
    }


def set_state(manifest: dict, state: str, note: str = '') -> dict:
    """更新状态并追加历史。非法状态抛 ValueError(绝不静默)。返回 manifest 本身。"""
    if state not in VALID_STATES:
        raise ValueError(f'非法作业状态: {state!r};合法值: {", ".join(VALID_STATES)}')
    manifest['state'] = state
    entry = {'state': state, 'at': _now_iso()}
    if note:
        entry['note'] = note
    manifest.setdefault('state_history', []).append(entry)
    return manifest


def manifest_path(job_dir: str | os.PathLike) -> Path:
    return Path(job_dir) / MANIFEST_NAME


def save_manifest(job_dir: str | os.PathLike, manifest: dict) -> Path:
    """原子写 job.yaml(tmp + os.replace),UTF-8。返回写入路径。"""
    target = manifest_path(job_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.yaml.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, target)
    return target


def load_manifest(job_dir: str | os.PathLike) -> dict | None:
    """读 job.yaml → dict;不存在/损坏/形状不对 → None(调用方一律按"不可读"降级)。

    手改坏一份 job.yaml(制表符/截断/编码)不能拖垮批量报告或 GUI 台账刷新——
    每个调用方都把 None 当"无 manifest",故解析异常在此统一兜成 None(同 load_profiles 口径)。
    """
    p = manifest_path(job_dir)
    if not p.is_file():
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _poscar_system_name(poscar_path: str | os.PathLike) -> str:
    """取 POSCAR 首行注释作体系名;失败降级为文件所在目录名。"""
    try:
        with open(poscar_path, 'r', encoding='utf-8') as f:
            first = f.readline().strip()
        if first:
            return first
    except OSError:
        pass
    return Path(poscar_path).resolve().parent.name


def create_from_build(job_dir: str | os.PathLike, build_result: dict, *,
                      poscar_path: str | os.PathLike,
                      validate: bool = True,
                      task_type: str = 'relax',
                      system: str = '') -> dict:
    """在 build_job_dir 成功后落一份 job.yaml。返回 manifest dict。

    build_result 即 build_job_dir 的返回值({'ok','out_dir','warnings','kpoints',
    'elements','completions'})。本函数不抛业务异常上抛给调用方决定是否降级
    (生成成功但 manifest 写失败时,调用方应告警而非撤销生成)。
    """
    completions = dict(build_result.get('completions') or {})
    job_dir = Path(job_dir)
    inputs = {
        'poscar': str(Path(poscar_path).resolve()),
        'poscar_sha256': sha256_file(poscar_path),
        'incar_source': incar_source_label(validate, completions),
        'completions': completions,
        'elements': list(build_result.get('elements') or []),
        'kpoints': list(build_result.get('kpoints') or []),
        # 赝势身份(发刊级溯源):哪套 POTCAR 算的,结果永远可答
        'potcar': list(build_result.get('potcar') or []),
    }
    potcar_file = job_dir / 'POTCAR'
    if potcar_file.is_file():
        inputs['potcar_sha256'] = sha256_file(potcar_file)
    m = new_manifest(
        job_id=f'{job_dir.resolve().name}-{time.strftime("%Y%m%d-%H%M%S")}',
        system=system or _poscar_system_name(poscar_path),
        task_type=task_type if task_type in KNOWN_TASK_TYPES else 'relax',
        calc_type=str(build_result.get('calc_type') or ''),
        inputs=inputs,
        warnings=build_result.get('warnings'),
    )
    save_manifest(job_dir, m)
    return m
