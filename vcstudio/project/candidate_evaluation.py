"""Deterministic Li-S adsorption-screen evaluation.

This module sits *above* :mod:`vcstudio.project.adsorption`.  It never reads
files, recomputes total energies, or asks an LLM to decide whether a catalyst
is good.  Instead it turns an already-audited ``delta_e_rows`` summary into a
versioned, JSON-friendly screening assessment.

The default adsorption-energy bands are deliberately labelled a Sabatier
*screening heuristic*.  They are not universal activity thresholds.  Likewise,
the Zhang thesis volcano descriptors are exposed only through strict,
path-specific opt-in strategies that require adsorption **free energy** for
``*LiS2``; an arbitrary polysulfide ``ΔE_ads`` can never enter those rules.
"""
from __future__ import annotations

import math
from collections import defaultdict


SCHEMA = 'vcstudio.candidate-evaluation/v1'
DEFAULT_POLICY_ID = 'lis_eads_sabatier_screen_v1'
DEFAULT_DEADBAND_EV = 0.15
DEFAULT_POTENTIAL_TOLERANCE_V = 0.02

LIS_SEQUENCE = ('Li2S8', 'Li2S6', 'Li2S4', 'Li2S2', 'Li2S')
LONG_CHAIN_SPECIES = ('Li2S8', 'Li2S6', 'Li2S4')
SHORT_CHAIN_SPECIES = ('Li2S2', 'Li2S')

_SUBSCRIPT_TRANS = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')
_SPECIES_LOOKUP = {species.casefold(): species for species in ('S8', *LIS_SEQUENCE, 'LiS2')}

_CATEGORY_ZH = {
    'unfavorable_or_unbound': '不利或未稳定吸附',
    'weak_anchor': '偏弱锚定',
    'moderate_anchor': '中等吸附',
    'strong_check_kinetics': '强吸附，需动力学验证',
    'overstrong_alert': '过强警戒',
}
_CATEGORY_SEVERITY = {
    'unfavorable_or_unbound': 'high',
    'weak_anchor': 'medium',
    'moderate_anchor': 'low',
    'strong_check_kinetics': 'medium',
    'overstrong_alert': 'high',
}

_PAPER_STRATEGIES = {
    'zhang_lis_assoc_volcano_v1': {
        'reaction_path_id': 'LIS_ASSOC_LIS',
        'descriptor_species': 'LiS2',
        'optimum_eV': -2.85,
        'description_zh': '*LiS 缔合路径的论文情境火山描述符',
    },
    'zhang_lis2_assoc_volcano_v1': {
        'reaction_path_id': 'LIS_ASSOC_LIS2',
        'descriptor_species': 'LiS2',
        'optimum_eV': -1.90,
        'description_zh': '*LiS2 缔合路径的论文情境火山描述符',
    },
}

_PRIORITY_ORDER = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}


def _finite_number(value) -> float | None:
    """Return a finite non-boolean float, otherwise ``None``."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_species(value) -> str:
    """Normalise common Li-S labels without merging arbitrary aliases."""
    raw = str(value or '').translate(_SUBSCRIPT_TRANS).strip().replace(' ', '')
    raw = raw.strip('*')
    return _SPECIES_LOOKUP.get(raw.casefold(), raw)


def _canonical_quantity(value) -> str:
    raw = str(value or '').strip().replace('Δ', 'delta_').replace('∆', 'delta_')
    compact = raw.lower().replace(' ', '').replace('-', '_')
    aliases = {
        'delta_e_ads': 'delta_E_ads',
        'delta_eads': 'delta_E_ads',
        'eads': 'delta_E_ads',
        'e_ads': 'delta_E_ads',
        'adsorption_energy': 'delta_E_ads',
        'delta_g_ads': 'delta_G_ads',
        'delta_gads': 'delta_G_ads',
        'dg_ads': 'delta_G_ads',
        'g_ads': 'delta_G_ads',
        'adsorption_free_energy': 'delta_G_ads',
    }
    return aliases.get(compact, str(value or '').strip())


def _method_status(row: dict) -> str:
    check = row.get('method_check')
    if isinstance(check, dict):
        status = str(check.get('status') or '').strip().lower()
        if status in {'verified', 'unverified', 'incompatible'}:
            return status
    return ''


def _row_name(row: dict, index: int) -> str:
    return str(row.get('config_id') or row.get('name') or f'config-{index + 1}')


def _classify_adsorption(value: float, deadband: float) -> dict:
    """Classify one canonical negative-is-stronger electronic adsorption energy."""
    if value >= 0.0:
        category = 'unfavorable_or_unbound'
    elif value > -0.5:
        category = 'weak_anchor'
    elif value >= -2.0:
        category = 'moderate_anchor'
    elif value >= -3.0:
        category = 'strong_check_kinetics'
    else:
        category = 'overstrong_alert'
    boundaries = (-3.0, -2.0, -0.5, 0.0)
    nearest = min(boundaries, key=lambda boundary: abs(value - boundary))
    distance = abs(value - nearest)
    return {
        'category': category,
        'category_zh': _CATEGORY_ZH[category],
        'severity': _CATEGORY_SEVERITY[category],
        'near_boundary': distance <= deadband + 1e-12,
        'nearest_boundary_eV': nearest,
        'boundary_distance_eV': round(distance, 6),
    }


def _normalise_options(options: dict | None) -> dict:
    opts = dict(options or {})
    policy_id = str(opts.get('policy_id') or DEFAULT_POLICY_ID)
    if policy_id != DEFAULT_POLICY_ID:
        raise ValueError(
            f'未知候选评价 policy_id={policy_id!r}；当前仅支持 {DEFAULT_POLICY_ID}')
    deadband = _finite_number(opts.get('deadband_eV', DEFAULT_DEADBAND_EV))
    if deadband is None or deadband < 0.0:
        raise ValueError('deadband_eV 必须是非负有限数')
    potential_tolerance = _finite_number(
        opts.get('potential_tolerance_V', DEFAULT_POTENTIAL_TOLERANCE_V))
    if potential_tolerance is None or potential_tolerance < 0.0:
        raise ValueError('potential_tolerance_V 必须是非负有限数')
    opts['policy_id'] = policy_id
    opts['deadband_eV'] = deadband
    opts['potential_tolerance_V'] = potential_tolerance
    opts['quantity'] = _canonical_quantity(opts.get('quantity') or 'delta_E_ads')
    opts['sign_convention'] = str(
        opts.get('sign_convention') or 'negative_is_stronger').strip()
    return opts


def _usable_rows(delta_summary: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Split rows into usable, incompatible, and invalid/unresolved records."""
    usable, incompatible, invalid = [], [], []
    for index, raw in enumerate(delta_summary.get('rows') or []):
        if not isinstance(raw, dict):
            invalid.append({'name': f'row-{index + 1}', 'reason': 'row_not_object'})
            continue
        row = dict(raw)
        row['_name'] = _row_name(row, index)
        row['_species'] = _canonical_species(row.get('species'))
        row['_value'] = _finite_number(row.get('delta_e'))
        row['_method_status'] = _method_status(row)
        if row['_method_status'] == 'incompatible':
            incompatible.append(row)
        elif not row['_species'] or row['_value'] is None:
            invalid.append({
                'name': row['_name'],
                'species': row['_species'] or None,
                'reason': 'missing_species' if not row['_species'] else 'missing_or_nonfinite_delta_e',
            })
        else:
            usable.append(row)
    return usable, incompatible, invalid


def _select_species_rows(rows: list[dict], deadband: float) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row['_species']].append(row)

    assessments = []
    order = {species: index for index, species in enumerate(('S8', *LIS_SEQUENCE))}
    for species, members in sorted(
            grouped.items(), key=lambda item: (order.get(item[0], 999), item[0])):
        members = sorted(members, key=lambda row: (row['_value'], row['_name']))
        selected = members[0]
        minimum = selected['_value']
        co_minima = [
            row['_name'] for row in members
            if row['_value'] - minimum <= deadband + 1e-12
        ]
        co_minimum_rows = co_minima_rows(members, minimum, deadband)
        method_statuses = {row['_method_status'] for row in co_minimum_rows}
        selected_method = (
            'unverified' if 'unverified' in method_statuses
            else 'missing' if '' in method_statuses
            else 'verified'
        )
        classification = _classify_adsorption(minimum, deadband)
        reason_codes = {
            'unfavorable_or_unbound': ['ADSORPTION_NONFAVORABLE'],
            'weak_anchor': ['ADSORPTION_WEAK'],
            'moderate_anchor': ['ADSORPTION_MODERATE'],
            'strong_check_kinetics': ['ADSORPTION_STRONG_CHECK_KINETICS'],
            'overstrong_alert': ['ADSORPTION_OVERSTRONG_ALERT'],
        }[classification['category']]
        if classification['near_boundary']:
            reason_codes.append('HEURISTIC_BOUNDARY_NEAR')
        if len(co_minima) > 1:
            reason_codes.append('CONFIGS_INDISTINGUISHABLE_WITHIN_DEADBAND')
        assessments.append({
            'species': species,
            'selected_config': selected['_name'],
            'value_eV': round(minimum, 6),
            **classification,
            'config_count': len(members),
            'co_minima': co_minima,
            'unique_minimum': len(co_minima) == 1,
            'method_status': selected_method,
            'reference_valid': selected.get('reference_valid'),
            'reason_codes': reason_codes,
        })
    return assessments


def co_minima_rows(members: list[dict], minimum: float, deadband: float) -> list[dict]:
    """Return records indistinguishable from the numerical minimum."""
    return [
        row for row in members
        if row['_value'] - minimum <= deadband + 1e-12
    ]


def _comparability(delta_summary: dict, species_rows: list[dict],
                   incompatible_rows: list[dict]) -> dict:
    selected_statuses = {row.get('method_status') for row in species_rows}
    summary_check = delta_summary.get('method_consistency') or {}
    summary_status = str(summary_check.get('status') or '').strip().lower()

    blockers = []
    warnings = []
    notices = []
    if not species_rows:
        blockers.append({
            'code': 'NO_USABLE_ADSORPTION_ENERGY',
            'message': '没有可用于评价的有限吸附能。',
        })
    reference_values = {row.get('reference_valid') for row in species_rows}
    explicit_missing_reference = (
        delta_summary.get('reference_mode') == 'none'
        or delta_summary.get('has_ref') is False
    )
    if species_rows and (
            explicit_missing_reference or False in reference_values):
        blockers.append({
            'code': 'ADSORBATE_REFERENCE_MISSING',
            'message': (
                '至少一个入选值缺少有效吸附物参考能，'
                'E(slab+ads)-E(slab) 不能作为吸附能评价。'),
        })
    elif species_rows and None in reference_values and delta_summary.get('has_ref') is not True:
        warnings.append({
            'code': 'ADSORBATE_REFERENCE_UNVERIFIED',
            'message': '吸附物参考态证据不完整，评价仅为暂定。',
        })
    if incompatible_rows:
        notices.append({
            'code': 'UNUSED_INCOMPATIBLE_CONFIGS',
            'message': '部分方法不兼容构型已从评价中排除。',
            'configs': [row['_name'] for row in incompatible_rows],
        })
    if 'unverified' in selected_statuses:
        warnings.append({
            'code': 'SELECTED_METHOD_UNVERIFIED',
            'message': '入选能量的方法证据不完整，评价仅为暂定。',
        })
    if 'missing' in selected_statuses:
        if summary_status == 'incompatible':
            blockers.append({
                'code': 'METHOD_INCOMPATIBLE',
                'message': '项目方法门未通过，且部分入选构型没有逐行证据可隔离冲突。',
                'issues': list(summary_check.get('issues') or []),
            })
        elif summary_status != 'verified':
            warnings.append({
                'code': 'METHOD_PROVENANCE_MISSING',
                'message': '缺少可核验的方法一致性证据。',
            })
    if blockers:
        status = 'incompatible'
    elif warnings:
        status = 'unverified'
    else:
        status = 'verified'
    return {
        'status': status,
        'scope': 'selected_operands',
        'blocking': blockers,
        'warnings': warnings,
        'notices': notices,
        'excluded_incompatible_configs': [row['_name'] for row in incompatible_rows],
    }


def _trend(species_rows: list[dict], deadband: float) -> dict:
    values = {row['species']: row['value_eV'] for row in species_rows}
    pairs = []
    strengthening = weakening = plateau = 0
    for left, right in zip(LIS_SEQUENCE, LIS_SEQUENCE[1:]):
        if left not in values or right not in values:
            continue
        change = values[right] - values[left]
        if change < -deadband - 1e-12:
            verdict = 'strengthening'
            strengthening += 1
        elif change > deadband + 1e-12:
            verdict = 'weakening'
            weakening += 1
        else:
            verdict = 'plateau'
            plateau += 1
        pairs.append({
            'from': left,
            'to': right,
            'delta_binding_profile_eV': round(change, 6),
            'verdict': verdict,
        })
    if len(pairs) < 2:
        status = 'insufficient'
    elif weakening == 0:
        status = 'progressive_or_plateau'
    elif strengthening > weakening:
        status = 'mostly_progressive'
    elif strengthening == 0 and weakening > 0:
        status = 'reversed'
    else:
        status = 'mixed'
    return {
        'status': status,
        'pairs': pairs,
        'n_pairs': len(pairs),
        'counts': {
            'strengthening': strengthening,
            'plateau': plateau,
            'weakening': weakening,
        },
        'interpretation': (
            '相邻吸附强度剖面，仅用于 Sabatier 初筛；'
            '不同 Li-S 物种吸附能之差不是配平反应自由能。'),
    }


def _long_chain_anchor(species_rows: list[dict]) -> dict:
    by_species = {row['species']: row for row in species_rows}
    present = [by_species[species] for species in LONG_CHAIN_SPECIES if species in by_species]
    categories = {row['category'] for row in present}
    if not present:
        status = 'unknown'
    elif categories & {'unfavorable_or_unbound', 'weak_anchor'}:
        status = 'weak_or_unfavorable'
    elif 'overstrong_alert' in categories:
        status = 'excessive'
    elif 'strong_check_kinetics' in categories:
        status = 'strong'
    else:
        status = 'adequate'
    return {
        'status': status,
        'species': [
            {'species': row['species'], 'category': row['category'],
             'value_eV': row['value_eV']}
            for row in present
        ],
    }


def _short_chain_risk(species_rows: list[dict], deadband: float) -> tuple[dict, dict | None]:
    by_species = {row['species']: row for row in species_rows}
    present = [by_species[species] for species in SHORT_CHAIN_SPECIES if species in by_species]
    categories = {row['category'] for row in present}
    if not present:
        risk = 'unknown'
    elif 'overstrong_alert' in categories:
        risk = 'high'
    elif 'strong_check_kinetics' in categories:
        risk = 'medium'
    elif categories & {'unfavorable_or_unbound', 'weak_anchor'}:
        risk = 'weak_terminal_binding'
    else:
        risk = 'low'

    jump = None
    if all(species in by_species for species in SHORT_CHAIN_SPECIES):
        value = by_species['Li2S']['value_eV'] - by_species['Li2S2']['value_eV']
        if value < -deadband - 1e-12:
            relation = 'li2s_stronger'
        elif value > deadband + 1e-12:
            relation = 'li2s_weaker'
        else:
            relation = 'indistinguishable'
        jump = {
            'value_eV': round(value, 6),
            'relation': relation,
            'interpretation': (
                '吸附强度剖面差，不是 Li2S2→Li2S 的反应自由能或能垒。'),
        }
    return {
        'status': risk,
        'species': [
            {'species': row['species'], 'category': row['category'],
             'value_eV': row['value_eV']}
            for row in present
        ],
    }, jump


def _paper_strategy(options: dict) -> dict:
    raw = options.get('paper_strategy')
    if isinstance(raw, dict):
        request = dict(raw)
    else:
        request = {}
        if raw:
            request['id'] = raw
    strategy_id = str(
        request.get('id') or options.get('paper_strategy_id') or '').strip()
    if not strategy_id:
        return {'status': 'not_requested', 'applicable': False}
    strategy = _PAPER_STRATEGIES.get(strategy_id)
    if strategy is None:
        return {
            'status': 'not_applicable',
            'applicable': False,
            'strategy_id': strategy_id,
            'reason_codes': ['UNKNOWN_PAPER_STRATEGY'],
        }

    quantity = _canonical_quantity(
        request.get('quantity') or options.get('paper_quantity') or options.get('quantity'))
    path_id = str(
        request.get('reaction_path_id')
        or options.get('reaction_path_id') or '').strip()
    descriptor_species = _canonical_species(
        request.get('descriptor_species')
        or options.get('descriptor_species'))
    descriptor_value = _finite_number(
        request.get('descriptor_value_eV')
        if 'descriptor_value_eV' in request
        else options.get('descriptor_value_eV'))
    sign = str(
        request.get('sign_convention')
        or options.get('sign_convention') or '').strip()

    reasons = []
    if quantity != 'delta_G_ads':
        reasons.append('PAPER_STRATEGY_REQUIRES_DELTA_G_ADS')
    if path_id != strategy['reaction_path_id']:
        reasons.append('PAPER_STRATEGY_PATH_MISMATCH')
    if descriptor_species != strategy['descriptor_species']:
        reasons.append('PAPER_STRATEGY_DESCRIPTOR_MISMATCH')
    if descriptor_value is None:
        reasons.append('PAPER_STRATEGY_DESCRIPTOR_VALUE_MISSING')
    if sign != 'negative_is_stronger':
        reasons.append('PAPER_STRATEGY_SIGN_CONVENTION_MISMATCH')
    if reasons:
        return {
            'status': 'not_applicable',
            'applicable': False,
            'strategy_id': strategy_id,
            'reason_codes': reasons,
            'required': {
                'quantity': 'delta_G_ads',
                'reaction_path_id': strategy['reaction_path_id'],
                'descriptor_species': strategy['descriptor_species'],
                'sign_convention': 'negative_is_stronger',
            },
        }
    optimum = strategy['optimum_eV']
    return {
        'status': 'applicable',
        'applicable': True,
        'strategy_id': strategy_id,
        'description_zh': strategy['description_zh'],
        'descriptor': {
            'quantity': 'delta_G_ads',
            'species': 'LiS2',
            'value_eV': round(descriptor_value, 6),
            'optimum_eV': optimum,
            'distance_to_context_optimum_eV': round(abs(descriptor_value - optimum), 6),
        },
        'source': {
            'document': '张洪毅硕士论文',
            'figure': '图3.13',
            'pdf_page': 40,
        },
        'disclaimer': (
            '该峰值仅适用于论文对应反应路径与自由能定义，不是普适最佳吸附能。'),
    }


def _thermodynamics(fed: dict | None, options: dict) -> dict:
    if not isinstance(fed, dict):
        return {
            'status': 'missing',
            'thermo_corrected': False,
            'solvation_corrected': False,
            'reason_codes': ['FREE_ENERGY_PATH_MISSING'],
        }
    result = {
        'status': 'available',
        'thermo_corrected': bool(fed.get('thermo_corrected')),
        'solvation_corrected': bool(
            fed.get('solvation_corrected') or options.get('solvation_complete')),
        'reaction_path_id': (
            fed.get('reaction_path_id') or options.get('reaction_path_id')),
        'u_l_V': _finite_number(fed.get('u_l')),
        'u_eq_V': _finite_number(fed.get('u_eq')),
        'eta_V': _finite_number(fed.get('eta')),
        'pds_index': fed.get('pds_index'),
        'reason_codes': [],
    }
    steps = fed.get('steps') or []
    step_values = [
        _finite_number(step.get('G')) for step in steps if isinstance(step, dict)
    ]
    if len(step_values) == len(steps) and len(step_values) >= 2:
        changes = [step_values[index + 1] - step_values[index]
                   for index in range(len(step_values) - 1)]
        result['delta_g_max_step_eV'] = round(max(changes), 6)
    per_e = [_finite_number(value) for value in (fed.get('per_electron') or [])]
    per_e = [value for value in per_e if value is not None]
    if per_e:
        result['delta_g_max_per_electron_eV'] = round(max(per_e), 6)

    u_eq, u_l, eta = result['u_eq_V'], result['u_l_V'], result['eta_V']
    if u_eq is not None and u_l is not None and eta is not None:
        direction = str(fed.get('direction') or options.get('reaction_direction')
                        or 'reduction').strip().lower()
        expected = u_l - u_eq if direction == 'oxidation' else u_eq - u_l
        difference = abs(eta - expected)
        result['potential_identity'] = {
            'ok': difference <= options['potential_tolerance_V'] + 1e-12,
            'expected_eta_V': round(expected, 6),
            'reported_eta_V': round(eta, 6),
            'difference_V': round(difference, 6),
            'tolerance_V': options['potential_tolerance_V'],
        }
        if not result['potential_identity']['ok']:
            result['status'] = 'inconsistent'
            result['reason_codes'].append('POTENTIAL_IDENTITY_FAILED')
    else:
        result['potential_identity'] = {
            'ok': None,
            'reason': 'U_eq、U_L、eta 未同时提供，无法核验恒等式。',
        }
    method = fed.get('method_consistency') or {}
    if isinstance(method, dict) and method.get('status') == 'incompatible':
        result['status'] = 'inconsistent'
        result['reason_codes'].append('FREE_ENERGY_METHOD_INCOMPATIBLE')
    return result


def _evidence(species_rows: list[dict], comparability: dict,
              thermodynamics: dict, options: dict, invalid_rows: list[dict]) -> dict:
    present = {row['species'] for row in species_rows}
    missing = [species for species in LIS_SEQUENCE if species not in present]
    single_config = [
        row['species'] for row in species_rows
        if row['species'] in LIS_SEQUENCE and row['config_count'] < 2
    ]
    geometry_verified = bool(options.get('geometry_verified'))
    neb_complete = bool(options.get('neb_complete'))
    if comparability['status'] == 'incompatible' or not species_rows:
        data_quality = 'blocked'
    elif comparability['status'] == 'verified' and not missing and not invalid_rows:
        data_quality = 'high' if not single_config and geometry_verified else 'medium'
    elif len(present & set(LIS_SEQUENCE)) >= 3:
        data_quality = 'medium' if comparability['status'] == 'verified' else 'low'
    else:
        data_quality = 'low'

    if neb_complete:
        ceiling = 'kinetically_supported'
    elif thermodynamics.get('solvation_corrected') and thermodynamics.get('thermo_corrected'):
        ceiling = 'solvated_thermodynamics'
    elif thermodynamics.get('thermo_corrected'):
        ceiling = 'corrected_thermodynamics'
    else:
        ceiling = 'electronic_adsorption_screen'
    return {
        'data_quality': data_quality,
        'claim_ceiling': ceiling,
        'coverage': {
            'required_species': list(LIS_SEQUENCE),
            'present_species': [species for species in LIS_SEQUENCE if species in present],
            'missing_species': missing,
            'n_present': len(present & set(LIS_SEQUENCE)),
            'n_required': len(LIS_SEQUENCE),
        },
        'single_config_species': single_config,
        'geometry_verified': geometry_verified,
        'thermochemistry_available': thermodynamics.get('thermo_corrected', False),
        'solvation_available': thermodynamics.get('solvation_corrected', False),
        'neb_available': neb_complete,
        'invalid_or_unresolved_rows': invalid_rows,
    }


def _profile_verdict(trend: dict, long_chain: dict, short_chain: dict,
                     evidence: dict) -> str:
    if evidence['coverage']['n_present'] < 2:
        return 'unavailable'
    if (short_chain['status'] == 'high'
            or long_chain['status'] in {'weak_or_unfavorable', 'excessive'}
            or trend['status'] == 'reversed'):
        return 'risk_signals'
    if (trend['status'] in {'progressive_or_plateau', 'mostly_progressive'}
            and long_chain['status'] in {'adequate', 'strong'}
            and short_chain['status'] in {'low', 'medium'}):
        return 'promising_for_followup'
    return 'mixed_or_incomplete'


def _decision(profile_verdict: str, evidence: dict, comparability: dict,
              thermodynamics: dict) -> dict:
    if comparability['status'] == 'incompatible' or evidence['data_quality'] == 'blocked':
        priority = 'blocked'
        summary = '能量或方法可比性门未通过，当前不能作候选判断。'
    elif profile_verdict == 'risk_signals':
        priority = 'lower_priority'
        summary = '吸附谱存在长链锚定不足、短链过强或趋势反向信号，暂不列为优先候选。'
    elif (profile_verdict == 'promising_for_followup'
          and comparability['status'] == 'verified'
          and not evidence['coverage']['missing_species']
          and thermodynamics.get('status') != 'inconsistent'):
        priority = 'advance'
        summary = '长链锚定与短链增强趋势较合理，建议优先进入自由能、溶剂化和关键势垒计算。'
    else:
        priority = 'hold_for_evidence'
        summary = '当前结果可保留，但方法、物种覆盖或后续热力学/动力学证据仍不足。'
    return {
        'priority': priority,
        'profile_verdict': profile_verdict,
        'data_quality': evidence['data_quality'],
        'claim_ceiling': evidence['claim_ceiling'],
        'summary_zh': summary,
    }


def _recommendations(evidence: dict, comparability: dict, short_chain: dict,
                     thermodynamics: dict, options: dict) -> list[dict]:
    rows = []

    def add(priority, code, action, reason, targets=None):
        rows.append({
            'priority': priority,
            'code': code,
            'action_zh': action,
            'reason': reason,
            'targets': list(targets or []),
        })

    blocking_codes = {
        item['code'] for item in comparability.get('blocking') or []
    }
    if 'METHOD_INCOMPATIBLE' in blocking_codes:
        add('P0', 'RECONCILE_METHOD_CONFLICTS', '统一或重新核验能量操作数的方法口径',
            '已知方法冲突使总能相减不可用。')
    elif comparability['status'] == 'unverified':
        add('P0', 'AUDIT_METHOD_PROVENANCE', '补齐实际输出的方法指纹与可比性证据',
            '当前方法证据不完整，候选分级只能是暂定。')
    if 'ADSORBATE_REFERENCE_MISSING' in blocking_codes:
        add('P0', 'CALC_VALID_ADSORBATE_REFERENCE', '补算并核验对应吸附物参考态',
            '缺少参考能时不能按 E(slab+ads)-E(slab)-E(adsorbate) 评价吸附能。')
    if 'NO_USABLE_ADSORPTION_ENERGY' in blocking_codes:
        add('P0', 'RESOLVE_ADSORPTION_ENERGIES', '完成或修复至少一组可用吸附能',
            '当前没有通过数值与方法门的吸附能。')
    missing = evidence['coverage']['missing_species']
    if missing:
        add('P1', 'CALC_MISSING_LIS_SPECIES', '补算缺失的 Li-S 吸附物种',
            '完整序列才能判断长链锚定、短链增强与终产物风险。', missing)
    if evidence['single_config_species']:
        add('P1', 'SAMPLE_MORE_CONFIGURATIONS', '补充吸附位点和取向采样',
            '单一构型不足以证明已找到全局最稳吸附态。',
            evidence['single_config_species'])
    if not evidence['geometry_verified']:
        add('P1', 'VERIFY_ADSORPTION_GEOMETRY', '核验吸附物完整性与表面重构',
            '异常解离或表面重构会让“最负吸附能”失去同类比较意义。')
    if not thermodynamics.get('thermo_corrected'):
        add('P1', 'CALC_ZPE_ENTROPY', '对路径关键态补频率、ZPE 与熵校正',
            '电子吸附能不能替代吸附自由能或反应自由能。')
    if not thermodynamics.get('solvation_corrected'):
        add('P1', 'CALC_SOLVATION_LONG_CHAIN', '补充同口径溶剂化修正',
            'Li2S8/Li2S6 在 DOL/DME 中的真空吸附能可能高估或低估实际锚定。',
            ['Li2S8', 'Li2S6'])
    if thermodynamics.get('status') == 'missing':
        add('P1', 'BUILD_BALANCED_FREE_ENERGY_PATH', '建立配平的自由能路径与工作电势台阶',
            '不同 Li2Sx 的吸附能不能直接相减得到反应台阶。')
    elif thermodynamics.get('status') == 'inconsistent':
        add('P0', 'AUDIT_FREE_ENERGY_METRICS', '复核 U_eq、U_L、eta 与方法口径',
            '自由能路径或电位恒等式不自洽，不能用于活性排序。')
    if short_chain['status'] in {'medium', 'high'}:
        add('P1', 'NEB_LI2S_CHARGE_DECOMPOSITION', '计算 Li2S 脱锂/分解充电势垒',
            '短链强吸附可能形成终产物陷阱。', ['Li2S'])
    if not evidence['neb_available']:
        add('P2', 'NEB_DISCHARGE_KEY_STEPS', '计算放电关键 S-S 断裂与 Li2S2→Li2S 势垒',
            '吸附能只反映热力学稳定性，不能替代动力学。',
            ['S-S cleavage', 'Li2S2→Li2S'])
    catalyst_kind = str(
        options.get('catalyst_kind') or options.get('candidate_kind') or '').upper()
    if catalyst_kind == 'DAC':
        add('P2', 'ADD_DAC_BASELINES', '补充裸 slab 与两个对应 SAC 基准',
            '仅有 DAC 吸附能不能证明双位点协同。')
    unique = {}
    for row in rows:
        unique.setdefault(row['code'], row)
    return sorted(
        unique.values(),
        key=lambda row: (_PRIORITY_ORDER.get(row['priority'], 99), row['code']))


def _candidate_identity(project: dict | None, options: dict) -> dict:
    project = project if isinstance(project, dict) else {}
    name = str(
        options.get('candidate_name') or project.get('name') or 'candidate')
    project_id = (
        options.get('project_id') or project.get('project_uuid')
        or project.get('root') or project.get('project_path'))
    return {'project_id': str(project_id) if project_id else None, 'name': name}


def evaluate_candidate(delta_summary, *, project=None, fed=None, options=None) -> dict:
    """Evaluate one Li-S adsorption project without I/O or model calls.

    ``delta_summary`` is the dictionary returned by
    :func:`vcstudio.project.adsorption.delta_e_rows`.  The function tolerates
    partial historical summaries, but missing method provenance lowers the
    evidence grade and prevents ``advance``.
    """
    if not isinstance(delta_summary, dict):
        raise ValueError('delta_summary 必须是 adsorption.delta_e_rows 返回的 dict')
    opts = _normalise_options(options)
    if opts['quantity'] != 'delta_E_ads':
        raise ValueError(
            '默认 Sabatier 吸附谱评价只接受 quantity=delta_E_ads；'
            '论文自由能描述符请通过 paper_strategy 单独核验')
    if opts['sign_convention'] != 'negative_is_stronger':
        raise ValueError(
            '评价器要求先把数值规范为 negative_is_stronger；不得静默翻转符号')

    usable, incompatible, invalid = _usable_rows(delta_summary)
    species_rows = _select_species_rows(usable, opts['deadband_eV'])
    comparability = _comparability(delta_summary, species_rows, incompatible)
    trend = _trend(species_rows, opts['deadband_eV'])
    long_chain = _long_chain_anchor(species_rows)
    short_chain, terminal_jump = _short_chain_risk(
        species_rows, opts['deadband_eV'])
    thermo = _thermodynamics(fed, opts)
    evidence = _evidence(
        species_rows, comparability, thermo, opts, invalid)
    profile_verdict = _profile_verdict(
        trend, long_chain, short_chain, evidence)
    decision = _decision(
        profile_verdict, evidence, comparability, thermo)
    paper = _paper_strategy(opts)
    recommendations = _recommendations(
        evidence, comparability, short_chain, thermo, opts)
    reason_codes = sorted({
        code
        for row in species_rows for code in row.get('reason_codes') or []
    } | {
        item['code'] for item in comparability['blocking']
    } | {
        item['code'] for item in comparability['warnings']
    } | {
        item['code'] for item in comparability['notices']
    } | set(thermo.get('reason_codes') or [])
      | set(paper.get('reason_codes') or []))

    return {
        'schema': SCHEMA,
        'candidate': _candidate_identity(project, opts),
        'basis': {
            'quantity': 'delta_E_ads',
            'definition': 'E_slab_ads-E_slab-E_adsorbate',
            'sign_convention': 'negative_is_stronger',
            'zpe': False,
            'entropy': False,
            'solvation': False,
        },
        'decision': decision,
        'comparability': comparability,
        'species': species_rows,
        'profile': {
            'order': list(LIS_SEQUENCE),
            'trend': trend,
            'ranking_deadband_eV': opts['deadband_eV'],
            'long_chain_anchor': long_chain,
            'short_chain_risk': short_chain,
            'terminal_binding_jump': terminal_jump,
        },
        'thermodynamics': thermo,
        'paper_volcano_strategy': paper,
        'evidence': evidence,
        'recommendations': recommendations,
        'audit': {
            'policy_id': opts['policy_id'],
            'policy_version': 1,
            'reason_codes': reason_codes,
            'policy_disclaimer': (
                '区间仅用于同口径 Li-S 电子吸附能的 Sabatier 初筛，'
                '不是普适最佳吸附能或催化活性定论。'),
        },
    }


def _batch_item(item, batch_options: dict) -> tuple[dict, dict | None, dict | None, dict]:
    if not isinstance(item, dict):
        raise ValueError('evaluate_batch 的每个 item 必须是 dict')
    if 'delta_summary' in item:
        delta = item.get('delta_summary')
        project = item.get('project')
        fed = item.get('fed')
        item_options = dict(item.get('options') or {})
        for key in ('candidate_name', 'project_id', 'comparison_protocol_id',
                    'reaction_path_id', 'catalyst_kind'):
            if key in item and key not in item_options:
                item_options[key] = item[key]
    elif 'rows' in item:
        delta, project, fed, item_options = item, None, None, {}
    else:
        raise ValueError('批量 item 缺少 delta_summary（或直接的 rows 汇总）')
    merged = dict(batch_options)
    merged.update(item_options)
    return delta, project, fed, merged


def _cohort_key(evaluation: dict, options: dict) -> tuple[str, bool]:
    protocol = str(options.get('comparison_protocol_id') or '').strip()
    thermo = evaluation['thermodynamics']
    path_id = str(thermo.get('reaction_path_id') or options.get('reaction_path_id') or '')
    correction = (
        'solvated' if thermo.get('solvation_corrected')
        else 'thermo' if thermo.get('thermo_corrected') else 'electronic')
    semantic = '|'.join([
        evaluation['audit']['policy_id'],
        evaluation['basis']['quantity'],
        path_id or 'no-path',
        correction,
    ])
    return (f'{protocol}|{semantic}' if protocol else f'unverified|{semantic}',
            bool(protocol))


def _cohort_result(key: str, members: list[tuple[dict, dict]], protocol_verified: bool) -> dict:
    evaluations = [item[0] for item in members]
    names = [evaluation['candidate']['name'] for evaluation in evaluations]
    decisions = defaultdict(list)
    for evaluation in evaluations:
        decisions[evaluation['decision']['priority']].append(
            evaluation['candidate']['name'])
    all_method_verified = all(
        evaluation['comparability']['status'] == 'verified'
        for evaluation in evaluations)
    thermo_rows = []
    for evaluation in evaluations:
        thermo = evaluation['thermodynamics']
        if (thermo.get('status') == 'available'
                and thermo.get('eta_V') is not None):
            thermo_rows.append({
                'name': evaluation['candidate']['name'],
                'eta_V': thermo['eta_V'],
                'u_l_V': thermo.get('u_l_V'),
            })
    if (protocol_verified and all_method_verified
            and len(thermo_rows) == len(evaluations) and thermo_rows):
        ordered = sorted(thermo_rows, key=lambda row: (row['eta_V'], row['name']))
        ranking = {
            'status': 'ranked_by_eta',
            'metric': 'eta_V_ascending',
            'order': [
                {**row, 'rank': index + 1}
                for index, row in enumerate(ordered)
            ],
        }
    else:
        reasons = []
        if not protocol_verified:
            reasons.append('缺少显式 comparison_protocol_id')
        if not all_method_verified:
            reasons.append('存在方法未核验候选')
        if len(thermo_rows) != len(evaluations):
            reasons.append('并非所有候选都有同路径可用 eta')
        ranking = {
            'status': 'tiered_only',
            'reason': '；'.join(reasons) or '电子吸附能初筛不生成伪精确总排名',
            'tiers': {
                key: values for key, values in sorted(
                    decisions.items(), key=lambda item: (
                        ('advance', 'hold_for_evidence', 'lower_priority', 'blocked').index(
                            item[0]) if item[0] in {
                                'advance', 'hold_for_evidence',
                                'lower_priority', 'blocked'} else 99))
            },
        }
    return {
        'cohort_id': key,
        'members': names,
        'comparison_protocol_verified': protocol_verified,
        'method_verified': all_method_verified,
        'ranking': ranking,
    }


def evaluate_batch(items, options=None) -> dict:
    """Evaluate multiple projects and group only comparable ranking cohorts."""
    if isinstance(items, (str, bytes)) or not hasattr(items, '__iter__'):
        raise ValueError('items 必须是候选项目列表')
    batch_options = dict(options or {})
    evaluations = []
    cohort_members: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    cohort_protocol: dict[str, bool] = {}
    for item in items:
        delta, project, fed, item_options = _batch_item(item, batch_options)
        evaluation = evaluate_candidate(
            delta, project=project, fed=fed, options=item_options)
        evaluations.append(evaluation)
        key, verified = _cohort_key(evaluation, item_options)
        cohort_members[key].append((evaluation, item_options))
        cohort_protocol[key] = verified
    cohorts = [
        _cohort_result(key, cohort_members[key], cohort_protocol[key])
        for key in sorted(cohort_members)
    ]
    return {
        'schema': 'vcstudio.candidate-evaluation-batch/v1',
        'policy_id': DEFAULT_POLICY_ID,
        'evaluations': evaluations,
        'cohorts': cohorts,
        'n_candidates': len(evaluations),
    }


__all__ = [
    'DEFAULT_DEADBAND_EV', 'DEFAULT_POLICY_ID', 'LIS_SEQUENCE', 'SCHEMA',
    'evaluate_batch', 'evaluate_candidate',
]
