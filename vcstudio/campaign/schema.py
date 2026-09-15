"""campaign 数据模型与读写:`.vcstudio/campaign/<id>/` 是唯一事实源。

目录约定(与蓝图 E1「纯文件事实源」一致):
- campaign.yaml:元数据 / 科学假设 / 整体状态(不含任务本体)。
- tasks/*.yaml:每个任务节点一份(id/kind/depends_on/success_criteria/
  required_checks/engine/job_dir/rung),文件名仅为可读别名,真相在文件内 `id` 字段。
- fingerprint.yaml:本 campaign 的方法指纹基准(见 fingerprint.py)。

设计原则:
- **显式校验、绝不静默**:缺字段 / kind 非法 / rung 非法 / 依赖不存在 / 环依赖
  一律抛中文 ValueError,由调用方决定如何提示。
- **原子写**(tmp + os.replace):监控/派生进程永远读不到半个文件。
- **纯记录不执行**:本模块只读写 yaml,不做任何远程/调度/门禁动作。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

SCHEMA_VERSION = 1

CAMPAIGN_YAML = 'campaign.yaml'
TASKS_DIR = 'tasks'
FINGERPRINT_YAML = 'fingerprint.yaml'

# 任务类型(与 manifest.KNOWN_TASK_TYPES 同源,campaign 层再加 analysis 汇总节点)
TASK_KINDS = ('relax', 'static', 'freq', 'neb', 'aimd', 'analysis')
DEFAULT_ENGINE = 'vasp'

_CAMPAIGN_REQUIRED = ('schema', 'id', 'status')
_TASK_REQUIRED = ('schema', 'id', 'kind', 'depends_on', 'rung')

_STEM_SAFE = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-')


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _safe_stem(task_id: str) -> str:
    """任务 id → 文件名安全形式(真相仍在文件内 id 字段,故映射有损也无碍)。"""
    s = ''.join(c if c in _STEM_SAFE else '_' for c in str(task_id))
    return s or 'task'


# ── 目录约定 ──────────────────────────────────────────────────────────────────
def campaign_root(base: str | os.PathLike) -> Path:
    """<base>/.vcstudio/campaign。"""
    return Path(base) / '.vcstudio' / 'campaign'


def campaign_dir(base: str | os.PathLike, campaign_id: str) -> Path:
    """<base>/.vcstudio/campaign/<id>。"""
    return campaign_root(base) / str(campaign_id)


# ── 构造器(纯函数) ───────────────────────────────────────────────────────────
def new_campaign(campaign_id: str, *, title: str = '', hypothesis: str = '',
                 budget_core_hours: float | None = None, require_pilot: bool = False,
                 fingerprint_hash: str | None = None, status: str = 'draft',
                 extra: dict | None = None) -> dict:
    """构造 campaign 元数据 dict(写入 campaign.yaml 的内容)。"""
    if not campaign_id:
        raise ValueError('campaign id 不能为空')
    meta = {
        'schema': SCHEMA_VERSION,
        'id': str(campaign_id),
        'title': title,
        'created_at': _now(),
        'hypothesis': hypothesis,               # 科学假设(组会拷问材料)
        'status': status,                       # draft/active/paused/done
        'budget_core_hours': budget_core_hours,  # None = 不限;见 budget.py
        'require_pilot': bool(require_pilot),    # 单点先行闸开关;见 gates.submit_gate
        'fingerprint_hash': fingerprint_hash,    # 本 campaign 方法指纹基准 hash
    }
    if extra:
        meta.update(extra)
    return meta


def new_task(task_id: str, kind: str, *, depends_on: list | None = None,
             success_criteria: dict | None = None, required_checks: list | None = None,
             engine: str = DEFAULT_ENGINE, job_dir: str | None = None,
             rung: str = 'pending', is_pilot: bool = False,
             fingerprint_hash: str | None = None) -> dict:
    """构造一个任务节点 dict。job_dir 关联现有作业目录(manifest 的 job.yaml 落点)。"""
    return {
        'schema': SCHEMA_VERSION,
        'id': str(task_id),
        'kind': kind,
        'depends_on': list(depends_on or []),
        'success_criteria': dict(success_criteria or {}),
        'required_checks': list(required_checks or []),
        'engine': engine or DEFAULT_ENGINE,
        'job_dir': job_dir,
        'rung': rung,
        'is_pilot': bool(is_pilot),             # 单点先行的代表作业标记
        'fingerprint_hash': fingerprint_hash,   # 本任务能量的方法指纹 hash
        'rung_history': [],                     # 三态推进/降级审计轨迹(states.py 维护)
    }


# ── 校验(缺字段 / 环依赖 → 中文 ValueError) ──────────────────────────────────
def validate_campaign_meta(meta: dict) -> None:
    if not isinstance(meta, dict):
        raise ValueError('campaign 元数据必须是 dict')
    for f in _CAMPAIGN_REQUIRED:
        if f not in meta:
            raise ValueError(f'campaign 元数据缺少必需字段: {f}')
    if not meta.get('id'):
        raise ValueError('campaign id 不能为空')
    b = meta.get('budget_core_hours')
    if b is not None and (isinstance(b, bool) or not isinstance(b, (int, float))):
        raise ValueError('budget_core_hours 必须是数字或 None')


def validate_task(task: dict) -> None:
    from vcstudio.campaign.states import ALL_RUNGS  # 延迟导入避免包内加载顺序耦合
    if not isinstance(task, dict):
        raise ValueError('任务必须是 dict')
    tid = task.get('id', '?')
    for f in _TASK_REQUIRED:
        if f not in task:
            raise ValueError(f'任务 {tid} 缺少必需字段: {f}')
    if not task.get('id'):
        raise ValueError('任务 id 不能为空')
    if task.get('kind') not in TASK_KINDS:
        raise ValueError(f'任务 {tid} 的 kind 非法: {task.get("kind")!r};'
                         f'合法值: {", ".join(TASK_KINDS)}')
    if task.get('rung') not in ALL_RUNGS:
        raise ValueError(f'任务 {tid} 的 rung 非法: {task.get("rung")!r};'
                         f'合法值: {", ".join(ALL_RUNGS)}')
    if not isinstance(task.get('depends_on'), list):
        raise ValueError(f'任务 {tid} 的 depends_on 必须是列表')


def _detect_cycle(graph: dict) -> None:
    """DFS 三色标记;命中回边即抛环路径(中文)。"""
    white, gray, black = 0, 1, 2
    color = {n: white for n in graph}
    stack: list = []

    def dfs(n):
        color[n] = gray
        stack.append(n)
        for m in graph.get(n, []):
            if color.get(m) == gray:
                i = stack.index(m)
                cyc = stack[i:] + [m]
                raise ValueError('检测到循环依赖: ' + ' → '.join(map(str, cyc)))
            if color.get(m) == white:
                dfs(m)
        color[n] = black
        stack.pop()

    for n in list(graph):
        if color[n] == white:
            dfs(n)


def validate_graph(tasks: list) -> None:
    """校验任务集的依赖图:重复 id / 悬空依赖 / 环依赖。"""
    ids = [t.get('id') for t in tasks]
    idset = set(ids)
    if len(idset) != len(ids):
        dup = sorted({str(i) for i in ids if ids.count(i) > 1})
        raise ValueError(f'存在重复任务 id: {", ".join(dup)}')
    for t in tasks:
        for d in (t.get('depends_on') or []):
            if d not in idset:
                raise ValueError(f'任务 {t.get("id")} 依赖不存在的任务: {d}')
    graph = {t['id']: list(t.get('depends_on') or []) for t in tasks}
    _detect_cycle(graph)


def validate_campaign(campaign: dict) -> None:
    """整体校验:元数据 + 每个任务 + 依赖图。campaign 为 load_campaign 的复合结构。"""
    meta = campaign.get('meta', campaign)
    validate_campaign_meta(meta)
    tasks = list(campaign.get('tasks') or [])
    for t in tasks:
        validate_task(t)
    validate_graph(tasks)


def tasks_by_id(campaign: dict) -> dict:
    return {t.get('id'): t for t in (campaign.get('tasks') or []) if t.get('id')}


# ── 原子读写 ──────────────────────────────────────────────────────────────────
def _atomic_yaml(path: Path, data) -> Path:
    """tmp + os.replace 原子写 UTF-8 yaml;写坏中途绝不损坏原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)
    return path


def _load_yaml(path: Path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None


def save_campaign_meta(campaign_dir_path: str | os.PathLike, meta: dict) -> Path:
    return _atomic_yaml(Path(campaign_dir_path) / CAMPAIGN_YAML, meta)


def save_task(campaign_dir_path: str | os.PathLike, task: dict) -> Path:
    d = Path(campaign_dir_path) / TASKS_DIR
    return _atomic_yaml(d / f'{_safe_stem(task["id"])}.yaml', task)


def save_fingerprint(campaign_dir_path: str | os.PathLike, fp: dict) -> Path:
    return _atomic_yaml(Path(campaign_dir_path) / FINGERPRINT_YAML, fp)


def load_fingerprint(campaign_dir_path: str | os.PathLike) -> dict | None:
    data = _load_yaml(Path(campaign_dir_path) / FINGERPRINT_YAML)
    return data if isinstance(data, dict) else None


def load_campaign(campaign_dir_path: str | os.PathLike) -> dict | None:
    """读回复合结构 {'dir','meta','tasks'};无 campaign.yaml / 畸形 → None。

    单个 tasks/*.yaml 读坏(手改/截断)只跳过该任务,不拖垮整份 campaign 读取
    (同 manifest.load_manifest 的容错口径)。
    """
    cdir = Path(campaign_dir_path)
    meta = _load_yaml(cdir / CAMPAIGN_YAML)
    if not isinstance(meta, dict):
        return None
    tasks: list = []
    tdir = cdir / TASKS_DIR
    if tdir.is_dir():
        for f in sorted(tdir.glob('*.yaml')):
            t = _load_yaml(f)
            if isinstance(t, dict) and t.get('id'):
                tasks.append(t)
    tasks.sort(key=lambda t: str(t.get('id', '')))
    return {'dir': str(cdir.resolve()), 'meta': meta, 'tasks': tasks}


# ── 便捷创建 / 增量 ────────────────────────────────────────────────────────────
def init_campaign(base: str | os.PathLike, campaign_id: str, *,
                  tasks: list | None = None, fingerprint: dict | None = None,
                  **meta_kw) -> dict:
    """建目录 + 写 campaign.yaml + 写各 tasks/*.yaml(先全量校验再落盘),返回复合结构。"""
    meta = new_campaign(campaign_id, **meta_kw)
    validate_campaign_meta(meta)
    tlist = list(tasks or [])
    for t in tlist:
        validate_task(t)
    validate_graph(tlist)

    cdir = campaign_dir(base, campaign_id)
    cdir.mkdir(parents=True, exist_ok=True)
    save_campaign_meta(cdir, meta)
    for t in tlist:
        save_task(cdir, t)
    if fingerprint is not None:
        save_fingerprint(cdir, fingerprint)
    return {'dir': str(cdir.resolve()), 'meta': meta, 'tasks': tlist}


def add_task(campaign: dict, task: dict) -> dict:
    """向复合结构追加任务并持久化(校验 + 去重 + 重验依赖图);返回 campaign 本身。"""
    validate_task(task)
    tasks = campaign.setdefault('tasks', [])
    if any(t.get('id') == task['id'] for t in tasks):
        raise ValueError(f'任务 id 重复: {task["id"]}')
    validate_graph(tasks + [task])       # 先校验候选全集,失败不改动原列表(无半提交)
    tasks.append(task)                    # 校验通过才提交并落盘
    if campaign.get('dir'):
        save_task(campaign['dir'], task)
    return campaign
