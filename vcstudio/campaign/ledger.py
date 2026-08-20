"""append-only 账本(蓝图 E5/E7):decisions.jsonl + events.jsonl,唯一事实源的一部分。

- decisions.jsonl:每个自主默认值、每次自愈决定、每处人工确认签字、每处方法学 diff 签字。
  `record_decision(...)` 返回 decision_id,供门禁豁免(waive)反向引用(gate 的 waiver.decision_id)。
- events.jsonl:rung 变更、门禁裁决、提交/失败等事件;驱动派生就绪 + 仪表盘 feed + 断点续跑。

安全红线(E7):账本会被打包/分享/进 git,**绝不写入密钥/口令/令牌**。每条记录写入前过
`sanitize`——正则扫描所有字符串(含 dict 键),命中 password/token/api_key/ghp_ 等即抛
ValueError,拒绝落盘。密钥只走 keyring,状态文件只存路径+版本。

JSONL 每条记录以单次 O_APPEND write + fsync 追加；文件不存在自动创建。
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from vcstudio.shared.secrets import classify_credential

DECISIONS_NAME = 'decisions.jsonl'
EVENTS_NAME = 'events.jsonl'

MADE_BY_VALUES = ('user', 'auto', 'ai-proposal')
WAIVER_KIND = 'gate-waiver'
GATE_DECISION_KIND = 'gate-decision'
REVOCATION_KIND = 'decision-revoked'
COORDINATION_LOCK_NAME = '.ledger-acceptance.lock'

# See acceptance_guard: the persistent OS-lock inode is campaign-scoped.  This process lock
# prevents separate thread-owned file handles from bypassing each other on platforms where byte
# lock semantics are process-oriented.
_COORDINATION_THREAD_LOCK = threading.Lock()

def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _iter_strings(obj):
    """深度遍历,产出所有字符串(含 dict 键),供敏感串扫描。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for it in obj:
            yield from _iter_strings(it)


def is_sensitive(text: str):
    """返回命中的敏感类别描述;无命中返回 None。"""
    return classify_credential(text, include_field_names=True)


def sanitize(record: dict) -> dict:
    """写入前守卫:任一字符串命中敏感正则即抛 ValueError;干净则原样返回。"""
    for s in _iter_strings(record):
        hit = is_sensitive(s)
        if hit:
            raise ValueError(f'账本拒绝写入疑似敏感信息({hit});'
                             f'密钥/口令/令牌绝不落盘,只存路径+版本(走 keyring)')
    return record


@contextmanager
def acceptance_guard(campaign_dir):
    """Serialize ledger append/revoke with the final accepted task transaction.

    The lock file is never unlinked: all processes must continue to address the same stable inode,
    while the OS automatically releases the byte lock if a writer exits or crashes.
    """
    lock_path = Path(campaign_dir) / COORDINATION_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _COORDINATION_THREAD_LOCK, lock_path.open('a+b') as handle:
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


def _append_jsonl(path: Path, record: dict) -> dict:
    """校验后以单次 ``O_APPEND`` write 追加完整 JSON 行。"""
    sanitize(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    with acceptance_guard(path.parent):
        fd = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError(f'账本短写:{written}/{len(payload)} bytes')
            os.fsync(fd)
        finally:
            os.close(fd)
    return record


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    out = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # 手改坏一行不拖垮整本账本读取
    except (OSError, UnicodeDecodeError):
        return out
    return out


# ── 决策账本 ──────────────────────────────────────────────────────────────────
def record_decision(campaign_dir, kind: str, detail, made_by: str,
                    context: dict | None = None) -> str:
    """记一条决策,返回 decision_id(门禁豁免用它反向引用)。

    made_by ∈ {'user','auto','ai-proposal'}:人工确认 / 自主默认 / AI 提案(待人签)。
    """
    if made_by not in MADE_BY_VALUES:
        raise ValueError(f'made_by 非法: {made_by!r};合法值: {", ".join(MADE_BY_VALUES)}')
    decision_id = 'dec-' + uuid.uuid4().hex[:12]
    record = {
        'id': decision_id,
        'ts': _now(),
        'kind': str(kind),
        'made_by': made_by,
        'detail': detail,
        'context': context or {},
    }
    _append_jsonl(Path(campaign_dir) / DECISIONS_NAME, record)
    return decision_id


def read_decisions(campaign_dir, *, kind: str | None = None,
                   made_by: str | None = None) -> list:
    rows = _read_jsonl(Path(campaign_dir) / DECISIONS_NAME)
    if kind is not None:
        rows = [r for r in rows if r.get('kind') == kind]
    if made_by is not None:
        rows = [r for r in rows if r.get('made_by') == made_by]
    return rows


def find_decision(campaign_dir, decision_id: str):
    """按 id 取一条决策;不存在 → None(门禁校验豁免引用有效性时用)。"""
    for r in _read_jsonl(Path(campaign_dir) / DECISIONS_NAME):
        if r.get('id') == decision_id:
            return r
    return None


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def decision_digest(record: dict) -> str:
    """账本决策整行的稳定 SHA-256；GateDecision 用它检测替换/篡改。"""
    if not isinstance(record, dict):
        raise ValueError('decision record 必须是 dict')
    return hashlib.sha256(_canonical_json(record).encode('utf-8')).hexdigest()


def value_digest(value) -> str:
    """任意 JSON-compatible 绑定值的稳定 SHA-256。"""
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _campaign_id(campaign_dir) -> str:
    """从 named campaign 的 campaign.yaml 读取 id；缺失/损坏拒绝授权。"""
    from vcstudio.campaign import schema
    campaign = schema.load_campaign(campaign_dir)
    cid = ((campaign or {}).get('meta') or {}).get('id')
    if not cid:
        raise ValueError('named campaign 缺失或损坏，不能解析权威决策账本')
    return str(cid)


def record_waiver_decision(campaign_dir, *, campaign_id: str, task_id: str,
                           gate: str, task_revision: int,
                           input_fingerprints: dict, detail,
                           actor: str) -> str:
    """记录只对指定 campaign/task/revision/gate/fingerprints 生效的人工豁免。"""
    named = _campaign_id(campaign_dir)
    if named != str(campaign_id):
        raise ValueError(f'waiver campaign 不匹配:named={named}, supplied={campaign_id}')
    if not task_id or not gate or not actor:
        raise ValueError('waiver task_id/gate/actor 均不能为空')
    if isinstance(task_revision, bool) or not isinstance(task_revision, int) \
            or task_revision < 0:
        raise ValueError('waiver task_revision 必须是非负整数')
    if not isinstance(input_fingerprints, dict):
        raise ValueError('waiver input_fingerprints 必须是 dict')
    context = {
        'authority_schema': 'vcstudio.gate-waiver/v1',
        'scope': 'task',
        'campaign_id': named,
        'task_id': str(task_id),
        'gate': str(gate),
        'task_revision': int(task_revision),
        'input_fingerprints': dict(input_fingerprints or {}),
        'input_fingerprints_digest': value_digest(dict(input_fingerprints or {})),
        'actor': str(actor),
    }
    return record_decision(campaign_dir, WAIVER_KIND, detail, 'user', context=context)


def revoke_decision(campaign_dir, decision_id: str, *, actor: str, reason: str) -> str:
    """append-only 撤销；不修改原决策，后续校验只要看见有效撤销就 fail-closed。"""
    if not actor or not reason:
        raise ValueError('撤销决策必须给 actor 与 reason')
    campaign_id = _campaign_id(campaign_dir)
    if find_decision(campaign_dir, decision_id) is None:
        raise ValueError(f'待撤销 decision_id 不存在:{decision_id}')
    return record_decision(
        campaign_dir, REVOCATION_KIND, reason, 'user',
        context={'authority_schema': 'vcstudio.decision-revocation/v1',
                 'campaign_id': campaign_id, 'decision_id': str(decision_id),
                 'actor': str(actor)})


def is_decision_revoked(campaign_dir, decision_id: str, *, campaign_id: str | None = None) -> bool:
    """是否存在同一 named campaign 中有效的人工撤销记录。"""
    named = _campaign_id(campaign_dir)
    if campaign_id is not None and named != str(campaign_id):
        return True
    for row in read_decisions(campaign_dir, kind=REVOCATION_KIND, made_by='user'):
        ctx = row.get('context') or {}
        if (ctx.get('authority_schema') == 'vcstudio.decision-revocation/v1'
                and str(ctx.get('campaign_id')) == named
                and str(ctx.get('decision_id')) == str(decision_id)):
            return True
    return False


def validate_waiver_decision(campaign_dir, decision_id: str, *, campaign_id: str,
                             task_id: str, gate: str, task_revision: int,
                             input_fingerprints: dict) -> tuple[dict, str]:
    """从 named campaign ledger 解析并验证 task-bound waiver；任一漂移即拒绝。"""
    named = _campaign_id(campaign_dir)
    if named != str(campaign_id):
        raise ValueError(f'waiver campaign 不匹配:named={named}, expected={campaign_id}')
    record = find_decision(campaign_dir, decision_id)
    if record is None:
        raise ValueError(f'waiver decision_id 在 named campaign ledger 中不存在:{decision_id}')
    if record.get('kind') != WAIVER_KIND:
        raise ValueError(f'waiver kind 非法:{record.get("kind")!r};必须为 {WAIVER_KIND}')
    if record.get('made_by') != 'user':
        raise ValueError('waiver 必须是 made_by=user 的人工决策')
    ctx = record.get('context') or {}
    if type(ctx.get('task_revision')) is not int:  # bool 也不得冒充 revision
        raise ValueError('waiver task_revision 类型非法')
    expected_fps = dict(input_fingerprints or {})
    checks = (
        ('authority_schema', ctx.get('authority_schema'), 'vcstudio.gate-waiver/v1'),
        ('scope', ctx.get('scope'), 'task'),
        ('campaign_id', str(ctx.get('campaign_id')), named),
        ('task_id', str(ctx.get('task_id')), str(task_id)),
        ('gate', str(ctx.get('gate')), str(gate)),
        ('task_revision', ctx.get('task_revision'), int(task_revision)),
        ('input_fingerprints_digest', ctx.get('input_fingerprints_digest'),
         value_digest(expected_fps)),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                f'waiver scope 绑定不匹配:{name} actual={actual!r}, expected={expected!r}')
    if ctx.get('input_fingerprints') != expected_fps:
        raise ValueError('waiver input_fingerprints 与当前任务输入不一致')
    if is_decision_revoked(campaign_dir, decision_id, campaign_id=named):
        raise ValueError(f'waiver decision 已撤销:{decision_id}')
    return record, decision_digest(record)


def record_gate_decision(campaign_dir, *, gate: str, status: str,
                         campaign_id: str, task_id: str, task_revision: int,
                         input_fingerprints: dict, checks_digest: str,
                         actor: str, waiver: dict | None = None) -> tuple[dict, str]:
    """把 gate 的 task-bound 最终裁决落到 named campaign ledger 并返回整行摘要。"""
    named = _campaign_id(campaign_dir)
    if named != str(campaign_id):
        raise ValueError(f'gate decision campaign 不匹配:named={named}, expected={campaign_id}')
    if not task_id or not gate or not actor:
        raise ValueError('gate decision task_id/gate/actor 均不能为空')
    if isinstance(task_revision, bool) or not isinstance(task_revision, int) \
            or task_revision < 0:
        raise ValueError('gate decision task_revision 必须是非负整数')
    if not isinstance(input_fingerprints, dict):
        raise ValueError('gate decision input_fingerprints 必须是 dict')
    context = {
        'authority_schema': 'vcstudio.gate-decision/v1',
        'scope': 'task',
        'campaign_id': named,
        'task_id': str(task_id),
        'gate': str(gate),
        'status': str(status),
        'task_revision': int(task_revision),
        'input_fingerprints': dict(input_fingerprints or {}),
        'input_fingerprints_digest': value_digest(dict(input_fingerprints or {})),
        'checks_digest': str(checks_digest),
        'actor': str(actor),
        'waiver': dict(waiver or {}),
    }
    decision_id = record_decision(
        campaign_dir, GATE_DECISION_KIND,
        f'{gate} status={status} task={task_id} revision={task_revision}',
        'user' if status == 'waived' else 'auto', context=context)
    record = find_decision(campaign_dir, decision_id)
    if record is None:  # pragma: no cover - append 成功后磁盘消失才可能发生
        raise OSError('gate decision 写入后无法从账本读回')
    return record, decision_digest(record)


# ── 事件账本 ──────────────────────────────────────────────────────────────────
def record_event(campaign_dir, kind: str, task_id, detail=None) -> dict:
    record = {'ts': _now(), 'kind': str(kind), 'task_id': task_id, 'detail': detail}
    _append_jsonl(Path(campaign_dir) / EVENTS_NAME, record)
    return record


def read_events(campaign_dir, *, task_id=None, kind: str | None = None) -> list:
    rows = _read_jsonl(Path(campaign_dir) / EVENTS_NAME)
    if task_id is not None:
        rows = [r for r in rows if r.get('task_id') == task_id]
    if kind is not None:
        rows = [r for r in rows if r.get('kind') == kind]
    return rows
