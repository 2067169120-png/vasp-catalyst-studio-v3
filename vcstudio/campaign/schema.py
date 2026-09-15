"""campaign 数据模型与读写:`.vcstudio/campaign/<id>/` 是唯一事实源。

目录约定(与蓝图 E1「纯文件事实源」一致):
- campaign.yaml:元数据 / 科学假设 / 整体状态(不含任务本体)。
- tasks/*.yaml:每个任务节点一份(id/kind/depends_on/success_criteria/
  required_checks/engine/job_dir/rung),文件名仅为可读别名,真相在文件内 `id` 字段。
- fingerprint.yaml:本 campaign 的方法指纹基准(见 fingerprint.py)。

设计原则:
- **显式校验、绝不静默**:缺字段 / kind 非法 / rung 非法 / 依赖不存在 / 环依赖
  一律抛中文 ValueError,由调用方决定如何提示。
- **原子写**(每次唯一 tmp + fsync + os.replace):监控/派生进程永远读不到半个文件。
- **并发写 fail-closed**:任务带 revision；claim/persist 必须 CAS，旧任务缺 revision 按 0 迁移。
- **纯记录不执行**:本模块只读写 yaml,不做任何远程/调度/门禁动作。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import copy
import errno
import os
import tempfile
import threading
import time
from contextlib import contextmanager
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

# OS byte locks coordinate processes; this lock also serializes independent file handles in
# threads of the current process.  The on-disk lock inode is deliberately persistent: unlinking
# after release could split already-waiting writers across two different inodes.
_TASK_GUARD_THREAD_LOCK = threading.Lock()
_CAMPAIGN_GUARD_THREAD_LOCK = threading.Lock()


class CampaignConflictError(ValueError):
    """create-only campaign 初始化发现同 ID 的权威文件已经存在。"""

    def __init__(self, campaign_id: str, path: Path):
        self.campaign_id = str(campaign_id)
        self.path = Path(path)
        super().__init__(
            f'campaign {self.campaign_id} 创建冲突:campaign.yaml 已存在；'
            'init_campaign 只允许 create-only，不会覆盖既有或损坏文件')


class RevisionConflictError(ValueError):
    """任务持久化的 expected revision 与磁盘当前 revision 不一致。"""

    def __init__(self, task_id: str, expected: int | None, current: int | None):
        self.task_id = str(task_id)
        self.expected_revision = expected
        self.current_revision = current
        expected_label = 'absent' if expected is None else str(expected)
        super().__init__(
            f'任务 {self.task_id} revision 冲突:'
            f'expected_revision={expected_label}, current_revision={current}')


class TaskClaimError(RuntimeError):
    """任务无法被本次 runner 原子 claim。"""


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
             fingerprint_hash: str | None = None,
             input_fingerprints: dict | None = None) -> dict:
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
        # revision 是任务文件的 CAS 代次。旧 campaign 缺该字段时按 0 读取；任何
        # claim/persist 成功后严格 +1，GateDecision 绑定的正是这一代任务。
        'revision': 0,
        'is_pilot': bool(is_pilot),             # 单点先行的代表作业标记
        'fingerprint_hash': fingerprint_hash,   # 本任务能量的方法指纹 hash
        'input_fingerprints': dict(
            input_fingerprints
            if input_fingerprints is not None
            else ({'method': fingerprint_hash} if fingerprint_hash else {})),
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
    task_revision(task)  # 缺失兼容为 0；畸形 revision 必须 fail-closed
    fps = task.get('input_fingerprints', {})
    if fps is not None and not isinstance(fps, dict):
        raise ValueError(f'任务 {tid} 的 input_fingerprints 必须是 dict')


def task_revision(task: dict) -> int:
    """读任务 revision；旧任务缺字段兼容为 0，布尔/负数/非整数拒绝。"""
    value = task.get('revision', 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'任务 {task.get("id", "?")} 的 revision 非法:{value!r}')
    return value


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
    """唯一临时文件 + fsync + os.replace 原子写，绝不共享固定 ``.tmp``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        stream = os.fdopen(fd, 'w', encoding='utf-8', newline='\n')
        fd = -1
        with stream as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def _create_yaml_exclusive(path: Path, data) -> Path:
    """Publish a complete YAML file only when ``path`` is still absent.

    The same-directory temporary is fully flushed before ``os.link`` performs the no-replace
    publication.  Unlike an existence check followed by ``os.replace``, the hard-link operation
    is an actual create-if-absent CAS and never exposes a partially written destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        stream = os.fdopen(fd, 'w', encoding='utf-8', newline='\n')
        fd = -1
        with stream as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


def _load_yaml(path: Path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None


@contextmanager
def _campaign_guard(campaign_dir_path: str | os.PathLike):
    """在稳定 inode 上持有 campaign 初始化/元数据写 OS byte lock。"""
    cdir = Path(campaign_dir_path)
    guard = cdir / '.campaign.yaml.cas.lock'
    guard.parent.mkdir(parents=True, exist_ok=True)
    with _CAMPAIGN_GUARD_THREAD_LOCK, guard.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == 'nt':
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by the Linux CI matrix
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_campaign_meta(campaign_dir_path: str | os.PathLike, meta: dict) -> Path:
    with _campaign_guard(campaign_dir_path):
        return _atomic_yaml(Path(campaign_dir_path) / CAMPAIGN_YAML, meta)


def task_path(campaign_dir_path: str | os.PathLike, task_id: str) -> Path:
    return Path(campaign_dir_path) / TASKS_DIR / f'{_safe_stem(task_id)}.yaml'


def load_task(campaign_dir_path: str | os.PathLike, task_id: str) -> dict | None:
    """按任务 id 读取单个任务；兼容旧文件缺 revision/input_fingerprints。"""
    path = task_path(campaign_dir_path, task_id)
    data = _load_yaml(path)
    if not isinstance(data, dict) or str(data.get('id')) != str(task_id):
        return None
    data.setdefault('revision', 0)
    data.setdefault('input_fingerprints', {})
    try:
        validate_task(data)
    except ValueError:
        return None
    return data


@contextmanager
def _task_guard(path: Path):
    """在稳定 inode 上持有单任务 OS byte lock；崩溃时由 OS 自动释放。"""
    guard = path.with_name(path.name + '.cas.lock')
    guard.parent.mkdir(parents=True, exist_ok=True)
    with _TASK_GUARD_THREAD_LOCK, guard.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == 'nt':
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by the Linux CI matrix
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_task(campaign_dir_path: str | os.PathLike, task: dict, *,
              expected_revision: int | None = None) -> Path:
    """保存任务。

    ``expected_revision`` 缺省只允许创建尚不存在的任务文件；覆盖既有任务必须传
    expected revision，函数会走 :func:`persist_task` 的 CAS。
    """
    if expected_revision is not None:
        persist_task(campaign_dir_path, task, expected_revision=expected_revision)
        return task_path(campaign_dir_path, task['id'])
    candidate = copy.deepcopy(task)
    candidate.setdefault('revision', 0)
    candidate.setdefault('input_fingerprints', {})
    validate_task(candidate)
    if candidate.get('rung') == 'accepted':
        raise ValueError(
            'accepted 任务不能通过 create-only save_task 写入；'
            '必须由 states.promote_accepted 使用权威 GateDecision 持久化')
    tid = str(candidate['id'])
    path = task_path(campaign_dir_path, tid)
    with _task_guard(path):
        # An existing but unreadable file is still authoritative evidence that this is not a
        # first creation.  Never replace it under a create-only call.
        current = load_task(campaign_dir_path, tid) if path.exists() else None
        if path.exists():
            current_revision = task_revision(current) if current is not None else None
            raise RevisionConflictError(tid, None, current_revision)
        try:
            _create_yaml_exclusive(path, candidate)
        except FileExistsError as exc:  # non-cooperating writer still cannot be overwritten
            current = load_task(campaign_dir_path, tid)
            current_revision = task_revision(current) if current is not None else None
            raise RevisionConflictError(tid, None, current_revision) from exc
    task.clear()
    task.update(copy.deepcopy(candidate))
    return path


def persist_task(campaign_dir_path: str | os.PathLike, task: dict, *,
                 expected_revision: int, gate_decision=None) -> dict:
    """以 revision CAS 持久化任务并严格 ``revision += 1``。

    返回/回填成功写入的候选任务；冲突时磁盘和传入对象均不改变。
    """
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) \
            or expected_revision < 0:
        raise ValueError('expected_revision 必须是非负整数')
    tid = str(task.get('id') or '')
    if not tid:
        raise ValueError('任务 id 不能为空')
    path = task_path(campaign_dir_path, tid)
    candidate = copy.deepcopy(task)
    candidate['revision'] = expected_revision + 1
    candidate.setdefault('input_fingerprints', {})
    validate_task(candidate)
    if candidate.get('rung') == 'accepted' and gate_decision is None:
        raise ValueError(
            'accepted 任务持久化必须携带权威 GateDecision；'
            '不能通过普通 revision CAS 直写')
    if gate_decision is not None and candidate.get('rung') != 'accepted':
        raise ValueError('GateDecision 事务只允许持久化 accepted 候选任务')

    def _compare_verify_write() -> None:
        current = load_task(campaign_dir_path, tid)
        current_revision = task_revision(current) if current is not None else None
        if current_revision != expected_revision:
            raise RevisionConflictError(tid, expected_revision, current_revision)
        if gate_decision is not None:
            from vcstudio.campaign import gates

            # This is the final authority check, deliberately inside the same campaign ledger
            # lock as the write.  A revocation that completed before this transaction entered
            # the lock therefore wins and acceptance fails closed.
            gates.verify_gate_decision(gate_decision, campaign_dir_path, current)
        _atomic_yaml(path, candidate)

    # Fixed order for accepted transactions: task lock -> campaign ledger lock.  Ledger writers
    # never acquire task locks, so revoke/append cannot form the reverse edge or deadlock.
    with _task_guard(path):
        if gate_decision is None:
            _compare_verify_write()
        else:
            from vcstudio.campaign import ledger

            with ledger.acceptance_guard(campaign_dir_path):
                _compare_verify_write()
    task.clear()
    task.update(copy.deepcopy(candidate))
    return task


def claim_task(campaign_dir_path: str | os.PathLike, task_id: str, *,
               expected_revision: int, owner: str,
               allowed_rungs: tuple[str, ...] = ('pending',)) -> dict:
    """以 revision CAS 将一个任务原子 claim 为 running。

    claim 与 rung/revision 同一次原子替换落盘；因此两个调用即使同时看见 pending，
    也最多一个能成功。自动驾驶外层 run.lock 是 campaign 级第一道互斥，本函数是
    task 级第二道 fail-closed 约束。
    """
    if not owner:
        raise ValueError('claim owner 不能为空')
    path = task_path(campaign_dir_path, task_id)
    with _task_guard(path):
        current = load_task(campaign_dir_path, task_id)
        current_revision = task_revision(current) if current is not None else None
        if current_revision != expected_revision:
            raise RevisionConflictError(task_id, expected_revision, current_revision)
        if current is None:
            raise TaskClaimError(f'任务不存在或损坏:{task_id}')
        if current.get('rung') not in allowed_rungs:
            raise TaskClaimError(
                f'任务 {task_id} 当前 rung={current.get("rung")!r}，不可 claim')
        candidate = copy.deepcopy(current)
        candidate['rung'] = 'running'
        candidate['revision'] = expected_revision + 1
        claimed_at = _now()
        candidate['claim'] = {'owner': owner, 'at': claimed_at,
                              'base_revision': expected_revision}
        candidate.setdefault('rung_history', []).append(
            {'rung': 'running', 'at': claimed_at, 'by': owner, 'note': 'task claim'})
        validate_task(candidate)
        _atomic_yaml(path, candidate)
    return candidate


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
                t.setdefault('revision', 0)
                t.setdefault('input_fingerprints', {})
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
    campaign_path = cdir / CAMPAIGN_YAML
    with _campaign_guard(cdir):
        # Presence is authoritative even when the existing YAML is corrupt.  A create-only
        # initializer must never turn damage or attacker-controlled state into overwrite rights.
        if campaign_path.exists():
            raise CampaignConflictError(campaign_id, campaign_path)
        try:
            _create_yaml_exclusive(campaign_path, meta)
        except FileExistsError as exc:  # a non-cooperating writer still cannot be overwritten
            raise CampaignConflictError(campaign_id, campaign_path) from exc
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
