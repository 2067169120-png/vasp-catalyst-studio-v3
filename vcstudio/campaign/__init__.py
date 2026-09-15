"""campaign 文件式控制面 —— 确定性自动驾驶 + 大通量矩阵 + 可复现包的共用地基。

源自 AICC 采纳蓝图 E1–E8(南开 AICC 为 CC BY-NC,本实现全部自研,不复制其代码),
横在「确定性自动驾驶」与「未来 AI autopilot」两者之下的同一层地基,与 AI 解耦。

四条支柱
--------
1. **文件即事实源**:`.vcstudio/campaign/<id>/` 下 campaign.yaml + tasks/*.yaml +
   fingerprint.yaml + budget.yaml +(decisions/events).jsonl + run.lock 即唯一真相。
   「就绪」是纯函数派生量,**永不落盘**(derive.py)。
2. **三态**:completed(执行层自述)≠ validated(确定性 checker)≠ accepted(门禁+人签)。
   只有 accepted 能进 ΔE/图/报告;AI/外部无权直接写 accepted(states.py)。
3. **三门**:submit_gate / accept_gate / report_gate,deny-by-default,只卡 release 不卡
   创作;任何 blocked 可凭账本 decision_id 豁免(gates.py)。
4. **账本**:decisions/events append-only,写入前拦密钥;机时预估 vs 实际有硬上限闸
   (ledger.py / budget.py)。方法指纹对象化保证一次比较同一基组(fingerprint.py)。

为什么没有数据库
----------------
YAML+JSONL 即唯一事实源:一切可 diff、可人工审计、进程崩了从文件续跑(重跑派生函数即可,
零内存状态恢复)。可复现包会被打包/分享/进 git(MIT),数据库既不可 diff 也塞不进压缩包,
还引入常驻进程与迁移负担;而本域天然低写入、强审计,文件存储反而是正解。这与全仓
「零数据库文件存储」(manifest/ledger/registry 皆 yaml/json)同构。

单机锁只做本机互斥(lock.py):过期只是安全信号,绝不自动抢占。
"""
from __future__ import annotations

from vcstudio.campaign import (
    budget,
    derive,
    fingerprint,
    gates,
    ledger,
    lock,
    schema,
    states,
)

# ── schema:数据模型与读写 ────────────────────────────────────────────────────
from vcstudio.campaign.schema import (
    SCHEMA_VERSION,
    TASK_KINDS,
    DEFAULT_ENGINE,
    RevisionConflictError,
    TaskClaimError,
    campaign_dir,
    campaign_root,
    new_campaign,
    new_task,
    init_campaign,
    add_task,
    load_campaign,
    load_fingerprint,
    save_campaign_meta,
    save_task,
    persist_task,
    claim_task,
    load_task,
    task_path,
    task_revision,
    save_fingerprint,
    tasks_by_id,
    validate_campaign,
    validate_campaign_meta,
    validate_task,
    validate_graph,
)

# ── states:三态机 ────────────────────────────────────────────────────────────
from vcstudio.campaign.states import (
    EXEC_STATES,
    RUNG_STATES,
    ALL_RUNGS,
    rung,
    rung_rank,
    is_accepted,
    set_exec_state,
    mark_running,
    mark_failed,
    mark_completed,
    promote_validated,
    promote_accepted,
    downgrade,
)

# ── fingerprint:方法指纹 ─────────────────────────────────────────────────────
from vcstudio.campaign.fingerprint import (
    FINGERPRINT_FIELDS,
    new_fingerprint,
    extract_from_inputs,
    fingerprint_hash,
    check_group_consistency,
)

# ── gates:三门 ───────────────────────────────────────────────────────────────
from vcstudio.campaign.gates import (
    GateDecision,
    submit_gate,
    accept_gate,
    report_gate,
    checks_digest,
    task_input_fingerprints,
    verify_gate_decision,
)

# ── ledger:决策/事件账本 ─────────────────────────────────────────────────────
from vcstudio.campaign.ledger import (
    record_decision,
    read_decisions,
    find_decision,
    record_event,
    read_events,
    sanitize,
    is_sensitive,
    decision_digest,
    value_digest,
    record_waiver_decision,
    validate_waiver_decision,
    revoke_decision,
    is_decision_revoked,
)

# ── budget:机时账本 ──────────────────────────────────────────────────────────
from vcstudio.campaign.budget import (
    estimate_job,
    record_estimate,
    record_actual,
    consumed,
    remaining,
    over_budget,
    load_budget,
)

# ── lock:单机锁 ──────────────────────────────────────────────────────────────
from vcstudio.campaign.lock import acquire, release, is_stale, is_held, read_lock

# ── derive:纯函数派生 ────────────────────────────────────────────────────────
from vcstudio.campaign.derive import derive_ready, campaign_stage, progress_summary

__all__ = [
    # 子模块
    'schema', 'states', 'fingerprint', 'gates', 'ledger', 'budget', 'lock', 'derive',
    # schema
    'SCHEMA_VERSION', 'TASK_KINDS', 'DEFAULT_ENGINE', 'RevisionConflictError',
    'TaskClaimError', 'campaign_dir', 'campaign_root',
    'new_campaign', 'new_task', 'init_campaign', 'add_task', 'load_campaign',
    'load_fingerprint', 'save_campaign_meta', 'save_task', 'persist_task', 'claim_task',
    'load_task', 'task_path', 'task_revision', 'save_fingerprint',
    'tasks_by_id', 'validate_campaign', 'validate_campaign_meta', 'validate_task',
    'validate_graph',
    # states
    'EXEC_STATES', 'RUNG_STATES', 'ALL_RUNGS', 'rung', 'rung_rank', 'is_accepted',
    'set_exec_state', 'mark_running', 'mark_failed', 'mark_completed',
    'promote_validated', 'promote_accepted', 'downgrade',
    # fingerprint
    'FINGERPRINT_FIELDS', 'new_fingerprint', 'extract_from_inputs', 'fingerprint_hash',
    'check_group_consistency',
    # gates
    'GateDecision', 'submit_gate', 'accept_gate', 'report_gate', 'checks_digest',
    'task_input_fingerprints', 'verify_gate_decision',
    # ledger
    'record_decision', 'read_decisions', 'find_decision', 'record_event',
    'read_events', 'sanitize', 'is_sensitive', 'decision_digest', 'value_digest',
    'record_waiver_decision', 'validate_waiver_decision', 'revoke_decision',
    'is_decision_revoked',
    # budget
    'estimate_job', 'record_estimate', 'record_actual', 'consumed', 'remaining',
    'over_budget', 'load_budget',
    # lock
    'acquire', 'release', 'is_stale', 'is_held', 'read_lock',
    # derive
    'derive_ready', 'campaign_stage', 'progress_summary',
]
