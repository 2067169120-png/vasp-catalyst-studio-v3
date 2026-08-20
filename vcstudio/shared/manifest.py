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
import tempfile
import time
import copy
from pathlib import Path

import yaml

from vcstudio import __version__

SCHEMA_VERSION = 1
MANIFEST_NAME = 'job.yaml'
_MANAGED_VASP_INPUTS = ('INCAR', 'POSCAR', 'KPOINTS', 'POTCAR')

# 状态机:生成期 CREATED;M2 起 UPLOADED/SUBMITTED/QUEUED/RUNNING;
# 终态 DONE/FAILED/UNCONVERGED;规则未命中交人工 NEEDS_HUMAN。
VALID_STATES = (
    'CREATED', 'UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING',
    'DONE', 'FAILED', 'UNCONVERGED', 'NEEDS_HUMAN',
)

# GUI 「计算类型」目录中的全部任务 key。这里不反向导入
# generate.task_catalog，避免 shared 层依赖生成器；同步关系由测试强制校验。
CATALOG_TASK_TYPES = (
    'relax', 'cellopt', 'static', 'adsorption_project', 'spin_scan',
    'dos_pdos', 'bands', 'bader', 'chgdiff', 'elf',
    'freq', 'aimd', 'neb', 'dimer', 'eos', 'surface_energy',
    'workfunction', 'formation_binding', 'vaspsol',
    'conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness',
)

# 存量文件使用的聚合/运行类型：conv_scan 的具体维度记在
# inputs.series；quick 是多引擎「直接提交输入」作业。它们不是 GUI 目录项，
# 但仍是合法 manifest 类型，不能被当成未知值。
OPERATIONAL_TASK_TYPES = ('conv_scan', 'quick')

# 只在写入时规范化；旧 job.yaml 的原文仍可读，不会被暗中改写。
TASK_TYPE_ALIASES = {
    'band': 'bands',
    'dos': 'dos_pdos',
    'pdos': 'dos_pdos',
}

KNOWN_TASK_TYPES = CATALOG_TASK_TYPES + OPERATIONAL_TASK_TYPES


def normalize_task_type(task_type: str) -> str:
    """任务类型规范化为 manifest 的唯一 key；未知值显式拒绝。

    旧别名 ``band/dos/pdos`` 仅为读入兼容，新写入统一落为
    ``bands/dos_pdos``。过去 create_from_build 会把任何拼错静默变成
    relax，可能让静态/动力学作业用错误的完成判据，因此现在必须报错。
    """
    raw = str(task_type or '').strip().lower()
    canonical = TASK_TYPE_ALIASES.get(raw, raw)
    if canonical not in KNOWN_TASK_TYPES:
        raise ValueError(
            f'未知任务类型: {task_type!r};合法值: {", ".join(KNOWN_TASK_TYPES)}')
    return canonical


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
    task_type = normalize_task_type(task_type)
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
    """Durably and atomically write ``job.yaml`` with a unique sibling temp.

    A fixed ``job.yaml.tmp`` name lets two application processes truncate or
    replace each other's staging file.  Per-job operation locks are the primary
    serialization boundary, but a unique temp also keeps this low-level writer
    safe for callers that are not remote-operation aware.  The file is flushed
    before ``os.replace``; the best-effort directory flush makes the rename
    durable on filesystems that expose directory descriptors.
    """
    target = manifest_path(job_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f'.{target.name}.', suffix='.tmp', dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as f:
            yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        try:
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
            parent_fd = os.open(str(target.parent), flags)
        except OSError:
            pass
        else:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            finally:
                os.close(parent_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
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
                      incar_path: str | os.PathLike | None = None,
                      validate: bool = True,
                      task_type: str | None = None,
                      system: str = '',
                      method_recipe_ref: dict | None = None,
                      job_id: str | None = None) -> dict:
    """在 build_job_dir 成功后落一份 job.yaml。返回 manifest dict。

    build_result 即 build_job_dir 的返回值({'ok','out_dir','warnings','kpoints',
    'elements','completions'})。本函数不抛业务异常上抛给调用方决定是否降级
    (生成成功但 manifest 写失败时,调用方应告警而非撤销生成)。

    task_type=None(默认)→ 用 build_result['task_type'](build_job_dir 按 INCAR 的
    NSW/IBRION 推断:NSW=0→static、IBRION=5/6→freq),再退 'relax'。修复静态作业
    被按 relax 收敛标志('reached required accuracy')误判未收敛的正确性 bug。

    incar_path 是可选的用户源 INCAR 路径；提供时记录其绝对路径与
    SHA256。无论是否提供，都会对受管作业目录中已存在的 VASP 四件套
    记录最终 SHA256，以区分用户源文本与自动补全后真正待提交的输入。
    """
    if task_type is None:
        task_type = str(build_result.get('task_type') or 'relax')
    task_type = normalize_task_type(task_type)
    completions = dict(build_result.get('completions') or {})
    job_dir = Path(job_dir)
    inputs = {
        'engine': 'vasp',
        'poscar': str(Path(poscar_path).resolve()),
        'poscar_sha256': sha256_file(poscar_path),
        'incar_source': incar_source_label(validate, completions),
        'completions': completions,
        'elements': list(build_result.get('elements') or []),
        'kpoints': list(build_result.get('kpoints') or []),
        # 赝势身份(发刊级溯源):哪套 POTCAR 算的,结果永远可答
        'potcar': list(build_result.get('potcar') or []),
    }
    recipe = build_result.get('method_recipe')
    if recipe:
        from vcstudio.generate.method_recipe import validate_method_recipe
        inputs['method_recipe'] = validate_method_recipe(recipe)
    else:
        # Old/imported build results remain usable, but strict equivalence must
        # identify them explicitly as legacy/incomplete rather than inventing a
        # recipe decision after the fact.
        inputs['method_recipe_status'] = 'legacy_missing'
    if build_result.get('execution_environment') is not None:
        raise ValueError(
            'execution_environment 只能由服务端在选定可核验集群 profile '
            '后绑定；create_from_build 拒绝 builder 传入的环境证据')
    if incar_path is not None:
        source_incar = Path(incar_path).resolve()
        inputs['source_incar_path'] = str(source_incar)
        inputs['source_incar_sha256'] = sha256_file(source_incar)
    if method_recipe_ref is not None:
        if not isinstance(method_recipe_ref, dict):
            raise ValueError('method_recipe_ref 必须是 dict')
        # Recipe sidecar/reference has already crossed its own strict schema gate.  Keep a detached
        # copy in the existing registration chain so later caller mutation cannot rewrite lineage.
        inputs['method_recipe'] = copy.deepcopy(method_recipe_ref)
    inputs['sha256'] = {
        name: sha256_file(job_dir / name)
        for name in _MANAGED_VASP_INPUTS
        if (job_dir / name).is_file()
    }
    potcar_file = job_dir / 'POTCAR'
    if potcar_file.is_file():
        inputs['potcar_sha256'] = sha256_file(potcar_file)
    m = new_manifest(
        job_id=(str(job_id) if job_id is not None
                else f'{job_dir.resolve().name}-{time.strftime("%Y%m%d-%H%M%S")}'),
        system=system or _poscar_system_name(poscar_path),
        task_type=task_type,
        calc_type=str(build_result.get('calc_type') or ''),
        inputs=inputs,
        warnings=build_result.get('warnings'),
    )
    from vcstudio.shared.scientific_inputs import record_input_closure
    record_input_closure(job_dir, m)
    save_manifest(job_dir, m)
    return m
