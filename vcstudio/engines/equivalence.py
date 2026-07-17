"""跨引擎参考态一致性校验——PM 特别裁决的"混引擎先行闸"。

绝对能量跨引擎**不可比**(赝势/基组/总能零点不同);唯一可跨引擎比对的是**相对量**
——反应能 ΔE_rxn = Σ coef·E(species)(系统误差在相减中大幅抵消)。故本模块:

- reference_consistency_check:输入各引擎各物种能量 + 反应定义,逐引擎算反应能再两两
  对比;任一对超容差 → 中文点名"两引擎结果不可混用于同一比较"。
- mixing_gate:一个比较组内混引擎 → **默认拒绝**,除非一致性检查通过并记录在案。

纯函数,零 IO,离线可测。中文注释允许,英文标识符。
"""
from __future__ import annotations


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
    lut: dict = {}
    engines: list = []
    for r in results or []:
        eng = str(r['engine']).lower()
        lut[(eng, r['species'])] = float(r['energy_ev'])
        if eng not in engines:
            engines.append(eng)
    engines = sorted(set(engines))

    if len(engines) < 2:
        only = engines[0] if engines else '无'
        return {'ok': True, 'pairs': [],
                'note': f'仅单一引擎({only}),无跨引擎混用,无需一致性检查。'}

    pairs: list = []
    bad: list = []
    skipped: list = []
    for rxn in reactions or []:
        name = rxn.get('name', '?')
        stoich = rxn.get('stoich') or {}
        rxn_e: dict = {}
        for eng in engines:
            if stoich and all((eng, sp) in lut for sp in stoich):
                rxn_e[eng] = sum(coef * lut[(eng, sp)] for sp, coef in stoich.items())
        if len(rxn_e) < 2:
            skipped.append(name)
            continue
        engs = sorted(rxn_e)
        for i in range(len(engs)):
            for j in range(i + 1, len(engs)):
                ei, ej = engs[i], engs[j]
                delta = abs(rxn_e[ei] - rxn_e[ej])
                ok_pair = delta <= tolerance_ev + 1e-12
                pairs.append({'reaction': name, 'engines': (ei, ej),
                              'delta_ev': round(delta, 6), 'ok': ok_pair})
                if not ok_pair:
                    bad.append(
                        f'引擎 {ei} 与 {ej} 的 {name} 反应能差 {delta:.4g} eV,'
                        f'超容差 {tolerance_ev:g} eV:两引擎结果不可混用于同一比较')

    if not pairs:
        return {'ok': False, 'pairs': [],
                'note': (f'{len(engines)} 个引擎({", ".join(engines)})但无任何共同'
                         f'可算反应(物种缺失或未提供反应),无法确认一致性,先行闸默认不放行。')}

    ok = all(p['ok'] for p in pairs)
    if ok:
        max_delta = max(p['delta_ev'] for p in pairs)
        note = (f'{len(pairs)} 组引擎对反应能一致性检查通过(最大差 {max_delta:.4g} eV '
                f'≤ 容差 {tolerance_ev:g} eV);跨引擎相对量可比。')
    else:
        note = '跨引擎参考态一致性检查未通过:' + ';'.join(bad) + '。'
    if skipped:
        note += f'(另有反应因物种缺失跳过:{", ".join(skipped)})'
    return {'ok': ok, 'pairs': pairs, 'note': note}


def mixing_gate(project_engines, *, consistency_passed: bool = False) -> dict:
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
    if consistency_passed:
        return {'ok': True,
                'note': (f'比较组混用引擎({joined});已通过跨引擎参考态一致性检查'
                         f'(反应能一致)并记录在案,允许混用。')}
    return {'ok': False,
            'note': (f'比较组内混用引擎({joined}),默认拒绝:绝对能量跨引擎不可比;'
                     f'须先通过 reference_consistency_check(反应能一致)并记录在案方可混用。')}
