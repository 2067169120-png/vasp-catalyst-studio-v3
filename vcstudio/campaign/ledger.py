"""append-only 账本(蓝图 E5/E7):decisions.jsonl + events.jsonl,唯一事实源的一部分。

- decisions.jsonl:每个自主默认值、每次自愈决定、每处人工确认签字、每处方法学 diff 签字。
  `record_decision(...)` 返回 decision_id,供门禁豁免(waive)反向引用(gate 的 waiver.decision_id)。
- events.jsonl:rung 变更、门禁裁决、提交/失败等事件;驱动派生就绪 + 仪表盘 feed + 断点续跑。

安全红线(E7):账本会被打包/分享/进 git,**绝不写入密钥/口令/令牌**。每条记录写入前过
`sanitize`——正则扫描所有字符串(含 dict 键),命中 password/token/api_key/ghp_ 等即抛
ValueError,拒绝落盘。密钥只走 keyring,状态文件只存路径+版本。

JSONL 追加即写(单机 append 原子性足够);文件不存在自动创建。中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

DECISIONS_NAME = 'decisions.jsonl'
EVENTS_NAME = 'events.jsonl'

MADE_BY_VALUES = ('user', 'auto', 'ai-proposal')

# 敏感串正则:命中即拒写。宁可误伤(deny-by-default)也不让密钥进入可分享的账本。
_SECRET_PATTERNS = (
    ('口令字段', re.compile(r'pass(word|wd|phrase)', re.I)),
    ('密钥字段', re.compile(r'secret', re.I)),
    ('令牌字段', re.compile(r'\btoken\b', re.I)),
    ('API Key', re.compile(r'api[_-]?key', re.I)),
    ('GitHub PAT', re.compile(r'gh[pousr]_[A-Za-z0-9]{16,}')),
    ('AWS AccessKey', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('Slack Token', re.compile(r'xox[baprs]-[A-Za-z0-9-]+')),
    ('私钥 PEM', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('Bearer 凭据', re.compile(r'\bbearer\s+[A-Za-z0-9._\-]{8,}', re.I)),
)


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
    for desc, pat in _SECRET_PATTERNS:
        if pat.search(text or ''):
            return desc
    return None


def sanitize(record: dict) -> dict:
    """写入前守卫:任一字符串命中敏感正则即抛 ValueError;干净则原样返回。"""
    for s in _iter_strings(record):
        hit = is_sensitive(s)
        if hit:
            raise ValueError(f'账本拒绝写入疑似敏感信息({hit});'
                             f'密钥/口令/令牌绝不落盘,只存路径+版本(走 keyring)')
    return record


def _append_jsonl(path: Path, record: dict) -> dict:
    """校验(sanitize)通过后追加一行 JSON;目录/文件不存在自动创建。"""
    sanitize(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
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
