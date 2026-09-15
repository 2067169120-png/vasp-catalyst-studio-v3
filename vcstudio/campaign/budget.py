"""机时账本(蓝图 E8):预估 vs 实际核时,超限硬闸(自动化自造风险的安全带)。

- campaign.yaml 的 `budget_core_hours`(None = 不限)是硬上限。
- `estimate_job(...)`:粗估某作业核时(**数量级估算,非精确基准**,系数可配)。
- `record_actual(...)` / `consumed(...)`:实际消耗累计,落 budget.yaml(campaign 目录内)。
- `remaining(campaign)`:剩余预算;超限判定 `over_budget(...)` 给 submit_gate 消费。

预算文件与 decisions/events 账本分开(那本是 JSONL,这本是可覆写的 yaml 汇总),
避免语义混淆。原子写(tmp + os.replace)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import yaml

BUDGET_YAML = 'budget.yaml'

# 粗估系数(数量级,可被调用方覆盖)。核心标度:DFT 对角化 ~ O(原子^3),
# 乘 k 点数与等效离子步/单点数;并行通信开销随核数弱增长(核时随核数缓慢上升)。
_DEFAULT_COEFF = {
    'base': 2.0e-7,             # 核时 /(原子^3 · k点 · 步)量级基准
    'parallel_overhead': 0.05,  # 每翻倍核数的核时惩罚(并行效率损耗)
    'kind_steps': {             # 各任务类型的等效离子步/单点数(量级)
        'static': 1,
        'relax': 40,
        'neb': 160,             # ~8 image × 20 步量级
        'aimd': 2000,           # ~2000 MD 步量级
        'analysis': 0,          # 纯分析节点不耗机时
        # 'freq' 特判:6·原子 个有限位移单点
    },
}


def estimate_job(natoms: int, nkpts: int, task_kind: str, cores: int,
                 *, coeff: dict | None = None) -> float:
    """粗估作业核时(**数量级估算**,用于机时闸的预算比对,不作为真实基准)。

    公式:core_hours ≈ base · 原子³ · k点数 · 等效步数 ·(1 + overhead·log2(核数))。
    freq 走 6·原子 个有限位移单点;analysis 记 0。系数经 coeff 覆盖。
    """
    c = dict(_DEFAULT_COEFF)
    if coeff:
        c = {**c, **coeff}
        if 'kind_steps' in (coeff or {}):
            c['kind_steps'] = {**_DEFAULT_COEFF['kind_steps'], **coeff['kind_steps']}
    natoms = max(int(natoms or 1), 1)
    nkpts = max(int(nkpts or 1), 1)
    cores = max(int(cores or 1), 1)

    if task_kind == 'freq':
        steps = 6 * natoms
    else:
        steps = c['kind_steps'].get(task_kind, c['kind_steps']['relax'])
    if not steps:
        return 0.0

    raw = c['base'] * (natoms ** 3) * nkpts * steps
    penalty = 1.0 + c['parallel_overhead'] * math.log2(cores)
    return round(raw * penalty, 3)


# ── 预算文件读写 ──────────────────────────────────────────────────────────────
def _budget_path(campaign_dir) -> Path:
    return Path(campaign_dir) / BUDGET_YAML


def load_budget(campaign_dir) -> dict:
    """读 budget.yaml;缺失/畸形 → {'estimates':{}, 'actuals':{}}。"""
    p = _budget_path(campaign_dir)
    if p.is_file():
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            data.setdefault('estimates', {})
            data.setdefault('actuals', {})
            return data
    return {'estimates': {}, 'actuals': {}}


def _save_budget(campaign_dir, data: dict) -> Path:
    p = _budget_path(campaign_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.yaml.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, p)
    return p


def record_estimate(campaign_dir, task_id: str, core_hours: float) -> Path:
    """登记某任务的预估核时(可选;submit_gate 也可直接吃 estimate_job 结果)。"""
    data = load_budget(campaign_dir)
    data['estimates'][str(task_id)] = float(core_hours)
    return _save_budget(campaign_dir, data)


def record_actual(campaign_dir, task_id: str, core_hours: float) -> Path:
    """累计某任务的实际核时(多次续算按次累加,反映真实消耗)。"""
    data = load_budget(campaign_dir)
    prev = float(data['actuals'].get(str(task_id), 0.0) or 0.0)
    data['actuals'][str(task_id)] = prev + float(core_hours)
    return _save_budget(campaign_dir, data)


def consumed(campaign_dir) -> float:
    """已消耗核时合计(actuals 求和)。"""
    return float(sum(float(v or 0.0) for v in load_budget(campaign_dir)['actuals'].values()))


def _meta_and_dir(campaign):
    """兼容传入复合结构 {'meta','dir'} 或直接传 meta dict。"""
    if isinstance(campaign, dict) and 'meta' in campaign:
        return campaign.get('meta') or {}, campaign.get('dir')
    return (campaign or {}), None


def remaining(campaign):
    """剩余预算 = budget_core_hours − 已消耗;预算为 None(不限)时返回 None。"""
    meta, cdir = _meta_and_dir(campaign)
    total = meta.get('budget_core_hours')
    if total is None:
        return None
    spent = consumed(cdir) if cdir else 0.0
    return float(total) - spent


def over_budget(campaign, estimated_core_hours: float) -> bool:
    """预估叠加后是否超剩余预算;不限预算恒 False。供 submit_gate 判定。"""
    rem = remaining(campaign)
    if rem is None:
        return False
    return float(estimated_core_hours or 0.0) > rem
