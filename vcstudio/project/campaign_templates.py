"""计算活动模板:把"SAC 筛选一键全链"沉淀成可实例化的阶段 DAG + 全链自动推进大脑。

定位(2026-07,一键出图管线的**上游**):用户选一个模板(如"SAC 锂硫筛选(张洪毅式)")
+ 一张体系矩阵(金属×模板×吸附质),本模块据模板的阶段定义,用 campaign.schema 建一份
带**阶段依赖**的任务 DAG——relax 为根,estatic(PDOS/Bader/差分电荷)与 freq 节点
depends_on 对应 relax;分阶段给出机时预估。

全链自动推进的"大脑"是 ``next_derivations``:纯函数扫 campaign 状态,回答"哪些 relax
已 validated、该派生下一阶段作业了"。pipeline_tick 接线时消费它逐拍推进(本模块只判定、
不建作业、不调度);幂等靠 campaign 事件账本(已派生的记一条事件,不重复返回)。

设计红线:纯记录/纯判定,绝不执行远程或调度动作;一切落 ``.vcstudio/campaign/<id>/``
(schema 原子写 + 全量校验)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re

from vcstudio.campaign import ledger, schema

# 依赖"满足"(可派生下一阶段)所需最低 rung:与 campaign.derive 同口径
_DEP_SATISFIED = ('validated', 'accepted')

# 派生事件类型(幂等锚点):consumer 派生后记一条,next_derivations 据此去重
DERIVATION_EVENT = 'derivation_emitted'

# 分阶段机时预估基准(核·时/作业;粗口径,供组会/预算闸,不冒充精确)
CORE_HOURS = {'relax': 48.0, 'static': 24.0, 'freq': 120.0, 'analysis': 0.0}


# ── 模板注册表 ────────────────────────────────────────────────────────────────
TEMPLATES = {
    'sac_lis_screening': {
        'name_zh': 'SAC 锂硫筛选(张洪毅式)',
        'description': '石墨烯基单原子催化剂 × 锂硫中间体:弛豫→静态(PDOS/Bader/差分电荷)'
                       '→吸附中间体频率,汇总 ΔE/16e 台阶/火山。',
        'stages': [
            {'key': 'relax', 'derive': None},
            {'key': 'estatic', 'derive': 'estatic',
             'kinds': ['pdos', 'bader', 'chgdiff'], 'after': 'relax'},
            {'key': 'freq', 'derive': 'freq', 'after': 'relax', 'only': 'adsorbates'},
        ],
        'analyses': ['delta_e', 'ladder_lis', 'volcano'],
        'figures_scenario': 'lis',
    },
    'adsorption_basic': {
        'name_zh': '吸附能基础(仅弛豫)',
        'description': '清洁表面 + 吸附构型弛豫,汇总 ΔE(柱状 + 三线表)。',
        'stages': [
            {'key': 'relax', 'derive': None},
        ],
        'analyses': ['delta_e'],
        'figures_scenario': 'general',
    },
    'full_characterization': {
        'name_zh': '全表征(能量 + 电子结构 + 频率)',
        'description': '弛豫→静态电子结构(PDOS/Bader/差分电荷)→吸附中间体频率,'
                       '汇总 ΔE 与自由能台阶。',
        'stages': [
            {'key': 'relax', 'derive': None},
            {'key': 'estatic', 'derive': 'estatic',
             'kinds': ['pdos', 'bader', 'chgdiff'], 'after': 'relax'},
            {'key': 'freq', 'derive': 'freq', 'after': 'relax', 'only': 'adsorbates'},
        ],
        'analyses': ['delta_e', 'ladder_lis'],
        'figures_scenario': 'general',
    },
}

_SLUG_RE = re.compile(r'[^A-Za-z0-9]+')


def list_templates() -> dict:
    """全部模板 {key: 浅信息}(name_zh/description/figures_scenario/阶段数)。"""
    return {k: {'name_zh': v['name_zh'], 'description': v.get('description', ''),
                'figures_scenario': v.get('figures_scenario'),
                'n_stages': len(v['stages']), 'analyses': list(v.get('analyses') or [])}
            for k, v in TEMPLATES.items()}


def get_template(template_key: str) -> dict:
    """按 key 取模板;未知 → 中文 ValueError 点名可选项。"""
    try:
        return TEMPLATES[template_key]
    except KeyError:
        raise ValueError(
            f'未知计算活动模板 {template_key!r},可选:{", ".join(sorted(TEMPLATES))}') from None


def _slug(name) -> str:
    s = _SLUG_RE.sub('_', str(name)).strip('_')
    return s or 'x'


def _relax_task(task_id, jobs_root, *, is_adsorbate, adsorbate=None, system=None):
    t = schema.new_task(task_id, 'relax', depends_on=[],
                        job_dir=os.path.join(jobs_root, task_id))
    t['stage'] = 'relax'
    t['is_adsorbate'] = bool(is_adsorbate)
    if adsorbate is not None:
        t['adsorbate'] = str(adsorbate)
    if system is not None:
        t['system'] = str(system)
    return t


def _derived_task(task_id, kind, parent_id, jobs_root, *, derive, kinds):
    t = schema.new_task(task_id, kind, depends_on=[parent_id],
                        job_dir=os.path.join(jobs_root, task_id))
    t['stage'] = derive
    t['derive'] = derive
    t['derive_kinds'] = list(kinds)
    return t


def _analysis_task(task_id, analysis, deps):
    t = schema.new_task(task_id, 'analysis', depends_on=list(deps))
    t['stage'] = 'analysis'
    t['analysis'] = str(analysis)
    return t


def _estimate(stage_counts: dict) -> dict:
    """分阶段机时预估 {stage: 核时, 'total': 合计}(核·时,粗口径)。"""
    est = {}
    total = 0.0
    for stage, n in stage_counts.items():
        h = CORE_HOURS.get(stage, 0.0) * int(n)
        est[stage] = round(h, 1)
        total += h
    est['total'] = round(total, 1)
    return est


def instantiate(template_key: str, matrix_spec: dict, out_root,
                *, campaign_id: str | None = None, title: str | None = None,
                hypothesis: str = '') -> dict:
    """据模板 + 体系矩阵建带阶段依赖的任务 DAG,落 campaign,返回概要。

    matrix_spec::

        {'systems': ['Fe@MN4', 'Co@MN4', ...],   # 催化剂/基底(必需,≥1)
         'adsorbates': ['Li2S8', 'Li2S6', ...],  # 吸附中间体(可选;每体系每吸附质一个弛豫)
         'clean': True,                          # 是否含清洁表面弛豫(默认 True)
         'campaign_id': '...'}                   # 可选,缺省用 template_key

    DAG:每 (体系[,吸附质]) 一个 relax 根;estatic/freq 节点 depends_on 对应 relax
    (freq 仅挂吸附中间体);analyses 汇总节点 depends_on 相关 relax。

    返回 ``{'campaign_dir','stages':{stage:计数},'n_jobs':计算作业数,
            'estimate':{stage:核时,'total':...},'campaign_id','figures_scenario'}``。
    """
    tpl = get_template(template_key)
    systems = list(matrix_spec.get('systems') or [])
    if not systems:
        raise ValueError('matrix_spec 至少需要一个 system(体系/催化剂)')
    adsorbates = list(matrix_spec.get('adsorbates') or [])
    include_clean = matrix_spec.get('clean', True)

    cid = campaign_id or matrix_spec.get('campaign_id') or template_key
    cdir = schema.campaign_dir(out_root, cid)
    jobs_root = str(cdir / 'jobs')

    tasks: list = []
    relax_ids: list = []
    ads_relax_ids: list = []

    for sysname in systems:
        s = _slug(sysname)
        if include_clean or not adsorbates:
            rid = f'{s}__clean__relax'
            tasks.append(_relax_task(rid, jobs_root, is_adsorbate=False, system=sysname))
            relax_ids.append(rid)
        for ads in adsorbates:
            rid = f'{s}__{_slug(ads)}__relax'
            tasks.append(_relax_task(rid, jobs_root, is_adsorbate=True,
                                     adsorbate=ads, system=sysname))
            relax_ids.append(rid)
            ads_relax_ids.append(rid)

    stage_counts = {'relax': len(relax_ids)}
    for stage in tpl['stages']:
        derive = stage.get('derive')
        if derive == 'estatic':
            for rid in relax_ids:
                sid = rid[:-len('__relax')] + '__estatic'
                tasks.append(_derived_task(sid, 'static', rid, jobs_root,
                                           derive='estatic',
                                           kinds=stage.get('kinds') or []))
            stage_counts['static'] = len(relax_ids)
        elif derive == 'freq':
            targets = ads_relax_ids if stage.get('only') == 'adsorbates' else relax_ids
            for rid in targets:
                fid = rid[:-len('__relax')] + '__freq'
                tasks.append(_derived_task(fid, 'freq', rid, jobs_root,
                                           derive='freq', kinds=[]))
            stage_counts['freq'] = len(targets)

    n_analysis = 0
    for an in (tpl.get('analyses') or []):
        deps = ads_relax_ids if (an == 'ladder_lis' and ads_relax_ids) else relax_ids
        tasks.append(_analysis_task(f'analysis__{_slug(an)}', an, deps))
        n_analysis += 1
    if n_analysis:
        stage_counts['analysis'] = n_analysis

    estimate = _estimate(stage_counts)
    schema.init_campaign(out_root, cid, tasks=tasks,
                         title=title or tpl['name_zh'], hypothesis=hypothesis,
                         status='active', budget_core_hours=estimate['total'])

    n_jobs = sum(stage_counts.get(k, 0) for k in ('relax', 'static', 'freq'))
    return {'campaign_dir': str(cdir.resolve()), 'stages': stage_counts,
            'n_jobs': n_jobs, 'estimate': estimate, 'campaign_id': cid,
            'figures_scenario': tpl.get('figures_scenario')}


# ── 全链自动推进大脑:哪些 relax 已 validated、该派生下一阶段了(纯判定,幂等)──

def mark_derived(campaign_dir, src_id: str, derive: str) -> dict:
    """记一条派生事件(consumer 派生下一阶段作业后调用),供 next_derivations 幂等去重。"""
    return ledger.record_event(campaign_dir, DERIVATION_EVENT, src_id,
                               {'src': str(src_id), 'derive': str(derive)})


def _emitted_pairs(campaign_dir) -> set:
    """已派生 (src_id, derive) 集合(读事件账本;缺账本 → 空集)。"""
    out = set()
    try:
        for ev in ledger.read_events(campaign_dir, kind=DERIVATION_EVENT):
            d = ev.get('detail') or {}
            src = d.get('src', ev.get('task_id'))
            if src is not None and d.get('derive') is not None:
                out.add((str(src), str(d['derive'])))
    except Exception:                      # noqa: BLE001 账本读失败 → 不去重(宁可多判)
        pass
    return out


def next_derivations(campaign_dir) -> list:
    """扫 campaign 状态,返回"已 validated 的 relax 该派生的下一阶段作业"清单。

    每项 ``{'src_id','src_dir','derive','kinds','task_id'}``:derive ∈ {'estatic','freq'},
    src_dir 为该 relax 的作业目录(供 consumer 据以派生静态/频率作业)。

    判定规则(纯函数,不落盘):
    - 父 relax 的 rung ∈ {validated, accepted}(不建在未验证结果上);
    - 子派生节点(kind static/freq、带 derive 标记)仍为 pending;
    - 该 (src, derive) 未在事件账本记过 DERIVATION_EVENT(幂等:已派生不重复返回)。
    consumer 派生后应调 ``mark_derived`` 记事件,下拍即不再返回该项。
    """
    campaign = schema.load_campaign(campaign_dir)
    if not campaign:
        return []
    by_id = {t.get('id'): t for t in (campaign.get('tasks') or []) if t.get('id')}
    emitted = _emitted_pairs(campaign_dir)

    out, seen = [], set()
    for child in (campaign.get('tasks') or []):
        derive = child.get('derive')
        if derive not in ('estatic', 'freq'):
            continue
        if child.get('rung', 'pending') != 'pending':
            continue
        for parent_id in (child.get('depends_on') or []):
            parent = by_id.get(parent_id)
            if parent is None or parent.get('kind') != 'relax':
                continue
            if parent.get('rung', 'pending') not in _DEP_SATISFIED:
                continue
            key = (str(parent_id), str(derive))
            if key in emitted or key in seen:
                continue
            seen.add(key)
            out.append({
                'src_id': str(parent_id),
                'src_dir': parent.get('job_dir'),
                'derive': str(derive),
                'kinds': list(child.get('derive_kinds') or []),
                'task_id': child.get('id'),
            })
    return out
