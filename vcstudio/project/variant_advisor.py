"""材料变体推荐(确定性规则,**不用 LLM**)。

为什么不用 LLM:变体空间是**组合枚举**——金属换周期表、配位环境换六类模板、掺杂开关——
其边界是确定的化学集合,规则枚举既**可穷尽又可复现**,且天然满足「去重排除论文已算过的」;
让 LLM 生成变体只会引入不可复现的随机性与「幻觉出周期表里没有的元素/模板」的风险。故本模块
**零 LLM**:母版识别、同族/邻族金属替换、配位环境变体、掺杂变体、去重、分批,全为纯确定性规则。

输出 matrix_spec 直接喂 generate.sac_builder.sac_matrix(metals × templates)。母版从抽取的体系
(paper_data 的 reference_dataset,或 ai_paper 归一 spec 的 systems)识别。中文注释,英文标识符。
"""
from __future__ import annotations

import re

from vcstudio.campaign import budget
from vcstudio.project import ai_paper

# ── 元素集合(确定性枚举边界) ─────────────────────────────────────────────────
#: 3d 过渡金属(母版金属替换的「全扫」集合)。
TM_3D = ('Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn')

#: 3d → 同族 4d congener(「同族 4d」替换)。
GROUP_4D = {'Sc': 'Y', 'Ti': 'Zr', 'V': 'Nb', 'Cr': 'Mo', 'Mn': 'Tc',
            'Fe': 'Ru', 'Co': 'Rh', 'Ni': 'Pd', 'Cu': 'Ag', 'Zn': 'Cd'}

#: SAC 六类模板全集(sac_builder 规范名)。
ALL_TEMPLATES = ('MN4', 'MN3', 'MP1N3', 'MS1N3', 'MB1N3', 'MN4+B')
#: 配位环境变体候选(杂原子调控第一配位层 + 配位数变体),掺杂单列。
_COORD_TEMPLATES = ('MN4', 'MN3', 'MP1N3', 'MS1N3', 'MB1N3')
#: 掺杂变体(次近邻 B 掺杂)。
_DOPANT_OF = {'MN4': 'MN4+B'}

#: 机时粗估默认(与 ai_paper 口径一致,仅供预算比对,非真实基准)。
_SLAB_NATOMS = 32
_DEFAULT_NK = 9
_DEFAULT_CORES = 64


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 母版识别(从 reference_dataset / ai_paper spec / 体系名列表)
# ═══════════════════════════════════════════════════════════════════════════════
def _split_system(canon):
    """规范体系名 'Fe@N4' → (metal, template_full)。解析不出 → (None, None)。"""
    s = str(canon or '').strip()
    if '@' in s:
        metal, env = s.split('@', 1)
    else:                                                    # 兜底:元素前缀 + 其余
        m = re.match(r'([A-Z][a-z]?)(.*)', s)
        if not m:
            return None, None
        metal, env = m.group(1), m.group(2)
    metal = metal.strip()
    if metal not in ai_paper._ELEMENTS:
        return None, None
    key = re.sub(r'[\s\-_]', '', env).upper()
    tmpl = ai_paper._TEMPLATE_ALIASES.get(key) or ai_paper._TEMPLATE_ALIASES.get('M' + key)
    return metal, (tmpl or None)


def _parents_from(spec_table):
    """识别母版 → (parents, computed_set, ranking)。

    parents:去重保序的 (metal, template) 列表(母版);computed_set:论文已算过的
    (metal, template) 集合(用于去重排除);ranking:{(metal,template): best_E_ads} 若有吸附能
    (供「文献最优体系邻域优先」的确定性锚定,越负越优)。

    接受三种 spec_table:paper_data.build_reference_dataset 的 {'entries':[...]};ai_paper 归一
    spec 的 {'systems':[{'metals','sites'}...]}(cell 或裸值皆可);或体系名字符串列表。
    """
    parents, computed, ranking = [], set(), {}

    def _add(metal, tmpl, e_ads=None):
        if not metal or not tmpl:
            return
        pair = (metal, tmpl)
        computed.add(pair)
        if pair not in parents:
            parents.append(pair)
        if e_ads is not None:
            cur = ranking.get(pair)
            if cur is None or e_ads < cur:                   # 取最负(最强吸附)作体系代表值
                ranking[pair] = e_ads

    if isinstance(spec_table, dict) and spec_table.get('entries') is not None:
        for e in spec_table['entries']:
            metal, tmpl = _split_system(e.get('system'))
            ev = e.get('ref_value') if e.get('quantity') in ('E_ads', 'adsorption_energy') else None
            _add(metal, tmpl, ev if isinstance(ev, (int, float)) and not isinstance(ev, bool) else None)
    elif isinstance(spec_table, dict) and spec_table.get('systems') is not None:
        for sysentry in (spec_table.get('systems') or []):
            metals = _collect_vals(sysentry.get('metals'))
            tmpls = [t for t in (_norm_tmpl(x) for x in _collect_vals(sysentry.get('sites'))) if t]
            for metal in metals:
                if metal not in ai_paper._ELEMENTS:
                    continue
                for tmpl in tmpls:
                    _add(metal, tmpl)
    elif isinstance(spec_table, (list, tuple)):
        for s in spec_table:
            metal, tmpl = _split_system(s)
            _add(metal, tmpl)

    return parents, computed, ranking


def _collect_vals(cells):
    """一列叶子(cell {'value','normalized'} 或裸值)→ 值列表(优先 normalized)。"""
    out = []
    for c in (cells or []):
        if isinstance(c, dict):
            v = c.get('normalized', c.get('value'))
        else:
            v = c
        if v not in (None, ''):
            out.append(v)
    return out


def _norm_tmpl(v):
    """裸模板值 → 规范模板名(MN4…);不认得 → None。"""
    key = re.sub(r'[\s\-_]', '', str(v or '')).upper()
    return ai_paper._TEMPLATE_ALIASES.get(key)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 变体推荐(金属替换 / 配位环境 / 掺杂;去重排除已算过的)
# ═══════════════════════════════════════════════════════════════════════════════
def _anchor(parents, ranking):
    """锚定「文献最优体系」:有吸附能则取最负者,否则取母版首个(文档序);无母版 → None。"""
    if not parents:
        return None
    ranked = [p for p in parents if p in ranking]
    if ranked:
        return min(ranked, key=lambda p: ranking[p])
    return parents[0]


def suggest_variants(spec_table):
    """母版体系 → 变体推荐 {'variants':[...], 'matrix_spec':{'metals','templates'}}。

    规则(确定性):
      · metal_swap:母版金属 → 3d 全扫 + 同族 4d congener(排除母版自身与论文已算过的组合);
      · coordination_swap:配位环境 → 其余五类 SAC 模板(杂原子/配位数调控,金属不变);
      · dopant:引入次近邻掺杂(如 MN4 → MN4+B,金属与第一配位层不变)。
    每个变体带 rationale_zh / template / metal / from / to / parent / priority / kind。priority 供
    分批:0 = 锚定体系的近邻(同族金属 + 配位/掺杂),1 = 锚定体系的 3d 远扫,2 = 其余母版。
    matrix_spec 直接喂 sac_matrix(metals × templates 的并集)。
    """
    parents, computed, ranking = _parents_from(spec_table)
    anchor = _anchor(parents, ranking)
    variants, seen = [], set()

    def _emit(kind, frm, to, metal, template, parent, priority, rationale):
        if metal not in ai_paper._ELEMENTS or template not in ALL_TEMPLATES:
            return
        if (metal, template) in computed:                    # 去重:排除论文已算过的组合
            return
        key = (metal, template)
        if key in seen:                                      # 去重:变体间同 (metal,template) 只留一
            return
        seen.add(key)
        variants.append({'kind': kind, 'from': frm, 'to': to, 'metal': metal,
                         'template': template, 'parent': f'{parent[0]}@{parent[1]}',
                         'priority': priority, 'rationale_zh': rationale})

    for parent in parents:
        p_metal, p_tmpl = parent
        is_anchor = (parent == anchor)
        congener = GROUP_4D.get(p_metal)

        # ── metal_swap:3d 全扫 + 同族 4d(锚定近邻里同族优先) ──────────────
        swap_metals = [m for m in TM_3D if m != p_metal]
        if congener:
            swap_metals.append(congener)
        for m in swap_metals:
            near = is_anchor and (m == congener)             # 同族 congener 记近邻(priority 0)
            priority = 0 if near else (1 if is_anchor else 2)
            tag = '同族 4d congener' if m == congener else '同周期 3d'
            _emit('metal_swap', p_metal, m, m, p_tmpl, parent, priority,
                  f'金属中心替换:{p_metal}→{m}({tag}),扫描 d 电子数对结合强度的影响,'
                  f'定位活性—吸附关系(火山图)上的位置。')

        # ── coordination_swap:配位环境变体(金属不变) ─────────────────────
        for tmpl in _COORD_TEMPLATES:
            if tmpl == p_tmpl:
                continue
            priority = 0 if is_anchor else 2
            _emit('coordination_swap', p_tmpl, tmpl, p_metal, tmpl, parent, priority,
                  f'配位环境变体:{p_tmpl}→{tmpl}(调控第一配位层的杂原子/配位数),'
                  f'考察配位场对活性位点电子结构的调制。')

        # ── dopant:次近邻掺杂(金属与第一配位层不变) ─────────────────────
        dop = _DOPANT_OF.get(p_tmpl)
        if dop:
            priority = 0 if is_anchor else 2
            _emit('dopant', p_tmpl, dop, p_metal, dop, parent, priority,
                  f'次近邻掺杂:{p_tmpl}→{dop}(引入 B 作为第二掺杂位),探索长程电子调控。')

    metals = sorted({v['metal'] for v in variants})
    templates = sorted({v['template'] for v in variants})
    return {'variants': variants,
            'matrix_spec': {'metals': metals, 'templates': templates}}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 变体计算活动分批(按优先级 + 机时预算)
# ═══════════════════════════════════════════════════════════════════════════════
def _variant_hours(n_jobs):
    """n 个变体(每个 = 1 个 slab 弛豫)的粗估核时。"""
    per = budget.estimate_job(_SLAB_NATOMS, _DEFAULT_NK, 'relax', _DEFAULT_CORES)
    return round(per * max(int(n_jobs), 0), 3)


def variant_campaign_plan(variants, *, budget_cap_hours=None):
    """变体列表 → 分批计划 {'n_jobs','estimate_hours','batches','note'}。

    分批策略:先按 priority 升序分组(第一批 = 锚定「文献最优体系」的近邻);若给了
    budget_cap_hours,组内再按机时上限切成小批(每小批不超上限)。每批列变体标识 M@T 与估时。
    """
    variants = list(variants or [])
    n_jobs = len(variants)
    estimate_hours = _variant_hours(n_jobs)
    per = budget.estimate_job(_SLAB_NATOMS, _DEFAULT_NK, 'relax', _DEFAULT_CORES)

    # 按 priority 分组(保序)
    tiers = {}
    for v in variants:
        tiers.setdefault(int(v.get('priority', 1)), []).append(v)

    batches = []
    cap = float(budget_cap_hours) if budget_cap_hours else None
    per_batch = max(1, int(cap // per)) if (cap and per > 0) else None
    for pr in sorted(tiers):
        group = tiers[pr]
        chunks = ([group[i:i + per_batch] for i in range(0, len(group), per_batch)]
                  if per_batch else [group])
        for chunk in chunks:
            names = [f'{v["metal"]}@{v["template"]}' for v in chunk]
            batches.append({
                'batch': len(batches) + 1,
                'priority': pr,
                'jobs': names,
                'n_jobs': len(chunk),
                'estimate_hours': round(per * len(chunk), 3),
                'reason': ('锚定体系近邻(优先复现邻域)' if pr == 0
                           else ('锚定体系 3d 远扫' if pr == 1 else '其余母版邻域')),
            })

    note = (f'共 {n_jobs} 个变体、约 {estimate_hours:g} 核时;'
            f'第一批优先计算锚定体系(文献报道吸附最强者)的近邻。')
    if cap:
        note += f'已按机时上限 {cap:g} 核时切分小批(每批 ≤ {per_batch} 个作业)。'
    return {'n_jobs': n_jobs, 'estimate_hours': estimate_hours,
            'batches': batches, 'note': note}
