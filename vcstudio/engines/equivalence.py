"""跨引擎参考态一致性校验——PM 特别裁决的"混引擎先行闸"。

绝对能量跨引擎**不可比**(赝势/基组/总能零点不同);唯一可跨引擎比对的是**相对量**
——反应能 ΔE_rxn = Σ coef·E(species)(系统误差在相减中大幅抵消)。故本模块:

- reference_consistency_check:输入各引擎各物种能量 + 反应定义,逐引擎算反应能再两两
  对比;任一对超容差 → 中文点名"两引擎结果不可混用于同一比较"。
- mixing_gate:一个比较组内混引擎 → **默认拒绝**,除非一致性检查通过并记录在案。

纯函数,零 IO,离线可测。中文注释允许,英文标识符。
"""
from __future__ import annotations

import math


def reference_consistency_check(results: list, reactions: list, *,
                                tolerance_ev: float = 0.05) -> dict:
    """跨引擎参考态一致性(以反应能为相对量对比)。

    Args:
        results:    [{'engine','species','energy_ev'}, ...] 各引擎各物种能量(eV)。
        reactions:  [{'name','stoich':{species:coef}}, ...] 反应定义,反应能 = Σ coef·E。
        tolerance_ev: 反应能跨引擎最大可接受差(eV),默认 0.05。

    Returns:
        {'ok':bool,
         'pairs':[{'reaction','engines':(e1,e2),'delta_ev','ok'}, ...],
         'note':中文结论}。

    口径:
    - 仅 1 个引擎 → ok=True(无跨引擎比较需求)。
    - ≥2 引擎但**无任何共同可算反应**(物种缺失/未给反应)→ ok=False(无法确认,
      先行闸默认不放行)。
    - 有可算反应 → ok = 所有引擎对反应能差 ≤ 容差;超差逐条中文点名。
    """
    try:
        tolerance = float(tolerance_ev)
    except (TypeError, ValueError):
        tolerance = -1.0
    if not math.isfinite(tolerance) or tolerance < 0:
        return {'ok': False, 'pairs': [], 'scope': 'within_engine_relative_only',
                'note': f'一致性容差必须为有限非负数，收到 {tolerance_ev!r}。'}

    lut: dict = {}
    engines: list = []
    conflicts = []
    invalid = []
    for index, r in enumerate(results or []):
        if not isinstance(r, dict):
            invalid.append(f'第 {index + 1} 条结果不是字典')
            continue
        eng = str(r.get('engine') or '').strip().lower()
        species = str(r.get('species') or '').strip()
        try:
            energy = float(r.get('energy_ev'))
        except (TypeError, ValueError):
            energy = float('nan')
        if not eng or not species or not math.isfinite(energy):
            invalid.append(f'第 {index + 1} 条结果缺引擎/物种或能量非有限')
            continue
        key = (eng, species)
        if key in lut and abs(lut[key] - energy) > 1e-10:
            conflicts.append(
                f'{eng}/{species} 同时出现 {lut[key]:.12g} 与 {energy:.12g} eV')
            continue
        lut[key] = energy
        if eng not in engines:
            engines.append(eng)
    if invalid or conflicts:
        details = invalid + conflicts
        return {'ok': False, 'pairs': [], 'scope': 'within_engine_relative_only',
                'note': '跨引擎一致性输入不唯一或无效：' + '；'.join(details) + '。'}
    engines = sorted(set(engines))

    if len(engines) < 2:
        only = engines[0] if engines else '无'
        return {'ok': True, 'pairs': [], 'scope': 'within_engine_relative_only',
                'note': f'仅单一引擎({only}),无跨引擎混用,无需一致性检查。'}

    pairs: list = []
    bad: list = []
    skipped: list = []
    for rxn in reactions or []:
        if not isinstance(rxn, dict):
            skipped.append('?（反应定义不是字典）')
            continue
        name = rxn.get('name', '?')
        stoich = rxn.get('stoich') or {}
        if not isinstance(stoich, dict) or not stoich:
            skipped.append(f'{name}（化学计量为空）')
            continue
        clean_stoich = {}
        bad_stoich = False
        for species, coefficient in stoich.items():
            try:
                value = float(coefficient)
            except (TypeError, ValueError):
                bad_stoich = True
                break
            if not math.isfinite(value):
                bad_stoich = True
                break
            if value:
                clean_stoich[str(species)] = value
        if bad_stoich or not clean_stoich:
            skipped.append(f'{name}（化学计量系数无效）')
            continue
        rxn_e: dict = {}
        for eng in engines:
            if all((eng, sp) in lut for sp in clean_stoich):
                rxn_e[eng] = sum(coef * lut[(eng, sp)]
                                 for sp, coef in clean_stoich.items())
        if len(rxn_e) < 2:
            skipped.append(name)
            continue
        engs = sorted(rxn_e)
        for i in range(len(engs)):
            for j in range(i + 1, len(engs)):
                ei, ej = engs[i], engs[j]
                delta = abs(rxn_e[ei] - rxn_e[ej])
                ok_pair = delta <= tolerance + 1e-12
                pairs.append({'reaction': name, 'engines': (ei, ej),
                              'delta_ev': round(delta, 6), 'ok': ok_pair})
                if not ok_pair:
                    bad.append(
                        f'引擎 {ei} 与 {ej} 的 {name} 反应能差 {delta:.4g} eV,'
                        f'超容差 {tolerance:g} eV:两引擎各自算出的相对量不可并列比较')

    if not pairs:
        return {'ok': False, 'pairs': [], 'scope': 'within_engine_relative_only',
                'note': (f'{len(engines)} 个引擎({", ".join(engines)})但无任何共同'
                         f'可算反应(物种缺失或未提供反应),无法确认一致性,先行闸默认不放行。')}

    ok = all(p['ok'] for p in pairs)
    if ok:
        max_delta = max(p['delta_ev'] for p in pairs)
        note = (f'{len(pairs)} 组引擎对反应能一致性检查通过(最大差 {max_delta:.4g} eV '
                f'≤ 容差 {tolerance:g} eV);只允许并列比较“各引擎内部独立相减”得到的相对量，'
                '不得把不同引擎的原始总能拼进同一能量差。')
    else:
        note = '跨引擎参考态一致性检查未通过:' + ';'.join(bad) + '。'
    if skipped:
        note += f'(另有反应因物种缺失跳过:{", ".join(skipped)})'
    return {'ok': ok, 'pairs': pairs, 'scope': 'within_engine_relative_only', 'note': note}


def mixing_gate(project_engines, *, consistency_passed: bool = False,
                comparison_scope: str = 'raw_energies') -> dict:
    """比较组混引擎闸:默认拒绝,除非一致性检查通过并记录在案。

    Args:
        project_engines: 该比较组用到的引擎名集合(set/list/iterable)。
        consistency_passed: 是否已通过 reference_consistency_check(反应能一致)并记录在案。

    Returns:
        {'ok':bool, 'note':中文结论}。
    """
    engs = sorted({str(e).lower() for e in project_engines})
    if len(engs) <= 1:
        only = engs[0] if engs else '无'
        return {'ok': True, 'note': f'比较组仅单一引擎({only}),无混用风险。'}

    joined = ', '.join(engs)
    scope = str(comparison_scope or '').strip().lower()
    relative_scopes = {'within_engine_relative', 'relative_results', 'reaction_energies'}
    if consistency_passed and scope in relative_scopes:
        return {'ok': True,
                'note': (f'并列比较引擎({joined})各自内部计算的相对量；已通过跨引擎'
                         '参考态一致性检查并记录在案。原始总能仍不得跨引擎相减。')}
    if consistency_passed:
        return {'ok': False,
                'note': (f'比较组含多个引擎({joined})。一致性检查只能放行“各引擎内部独立相减”'
                         '后的相对量；当前 comparison_scope 仍是原始能量，拒绝跨引擎拼接。')}
    return {'ok': False,
            'note': (f'比较组内混用引擎({joined}),默认拒绝:绝对能量跨引擎不可比;'
                     '如要并列比较反应能，须先在每个引擎内部独立相减，再通过 '
                     'reference_consistency_check，并显式使用 relative_results 范围。')}
