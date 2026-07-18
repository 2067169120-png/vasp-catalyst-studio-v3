"""ai_paper 复现扩展测试:data_tables 区透传 / attach_data_tables / full_reproduce_plan,
以及**向后兼容回归**(既有 validate_spec/plan_campaign 接口与形状不变)。
"""
import json

from vcstudio.project import ai_paper as ap


def _cell(v, page=1):
    return {'value': v, 'origin': {'page': page, 'quote': 'q'}, 'confidence': 'high'}


def _spec():
    return {
        'systems': [{'substrate': _cell('graphene'),
                     'metals': [_cell('Fe'), _cell('Co')], 'sites': [_cell('N4')]}],
        'adsorbates': [_cell('S8')],
        'methods': {'functional': _cell('PBE'), 'encut': _cell('500 eV'),
                    'kpoints_relax': _cell('3x3x1')},
        'reactions': [_cell('ORR')], 'outputs': [_cell('volcano')], 'mode': _cell('reproduce'),
    }


def _reply(obj):
    return 200, json.dumps({'choices': [{'message': {'content': json.dumps(obj)}}]}).encode()


_TABLES_OBJ = {'tables': [{'label': '表2', 'kind': 'adsorption_energy', 'columns': ['sys', 'E'],
                           'rows': [{'system': 'Fe-N4', 'species': 'Li2S4',
                                     'value_ev': -1.71, 'page_hint': 'p3'}],
                           'origin_snippet': 'Table 2 ...'}]}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 向后兼容回归(既有接口不变)
# ═══════════════════════════════════════════════════════════════════════════════
def test_validate_spec_backward_compat_no_data_tables_key():
    val = ap.validate_spec(_spec())
    assert 'data_tables' not in val['normalized']              # 无此键则不凭空加
    assert 'reference' not in val['normalized']
    # 既有键与归一仍在
    assert val['normalized']['methods']['functional']['normalized'] == 'PBE'
    assert val['normalized']['methods']['encut']['normalized'] == 500.0


def test_validate_spec_passes_through_data_tables():
    spec = _spec()
    spec['data_tables'] = [{'label': '表2', 'kind': 'adsorption_energy', 'rows': []}]
    spec['reference'] = {'entries': []}
    val = ap.validate_spec(spec)
    assert val['normalized']['data_tables'] == spec['data_tables']   # 原样透传
    assert val['normalized']['reference'] == spec['reference']


def test_plan_campaign_backward_compat_shape():
    pc = ap.plan_campaign(_spec())
    assert pc['ok'] is True
    plan = pc['plan']
    # 既有形状不变
    for key in ('slabs', 'adsorbates', 'matrix', 'tasks', 'pilot', 'fingerprint',
                'fingerprint_hash', 'mode', 'methods'):
        assert key in plan
    assert plan['slabs'] == ['Fe@MN4', 'Co@MN4']


# ═══════════════════════════════════════════════════════════════════════════════
# 2. attach_data_tables
# ═══════════════════════════════════════════════════════════════════════════════
def test_attach_data_tables_happy():
    r = ap.attach_data_tables(_spec(), 'paper text with Table 2 ...',
                              transport=lambda *a: _reply(_TABLES_OBJ),
                              config={'allow_external': True, 'api_key': 'k'})
    assert r['ok'] is True and len(r['tables']) == 1
    assert r['reference']['entries'][0]['system'] == 'Fe@N4'   # 归一 M@N4
    assert r['spec']['data_tables'] and r['spec']['reference']
    # 既有键不被破坏(向后兼容)
    assert r['spec']['systems'] == _spec()['systems']


def test_attach_data_tables_gate_off_blocks_and_guides():
    def boom(*a):
        raise AssertionError('联网关闭时绝不外发')
    r = ap.attach_data_tables(_spec(), 'text', transport=boom, config={'allow_external': False})
    assert r['ok'] is False and '设置页' in r['error'] and r['tables'] == []


def test_attach_data_tables_llm_failure_degrades(monkeypatch):
    monkeypatch.setattr(ap, 'RETRY_BACKOFF_SEC', 0)
    r = ap.attach_data_tables(_spec(), 'text',
                              transport=lambda *a: (200, b'{"choices":[{"message":{"content":"x"}}]}'),
                              config={'allow_external': True, 'api_key': 'k'})
    assert r['ok'] is False and r['reference'] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. full_reproduce_plan(复现计划 + 可选变体)
# ═══════════════════════════════════════════════════════════════════════════════
def test_full_reproduce_plan_no_variants():
    fp = ap.full_reproduce_plan(_spec())
    assert fp['ok'] is True and fp['variants'] is None and fp['variant_plan'] is None
    assert fp['plan']['slabs'] == ['Fe@MN4', 'Co@MN4']


def test_full_reproduce_plan_builds_reference_from_data_tables():
    spec = _spec()
    spec['data_tables'] = _TABLES_OBJ['tables']
    fp = ap.full_reproduce_plan(spec)
    assert fp['reference']['entries'][0]['system'] == 'Fe@N4'


def test_full_reproduce_plan_with_variants_from_reference():
    spec = _spec()
    spec['reference'] = {'entries': [{'system': 'Fe@N4', 'species': 'Li2S4',
                                      'quantity': 'E_ads', 'ref_value': -1.8}]}
    fp = ap.full_reproduce_plan(spec, include_variants=True, budget_cap_hours=40)
    assert fp['variants'] and fp['variants']['variants']
    # Fe@N4 已算过 → 变体不含 (Fe, MN4)
    combos = {(v['metal'], v['template']) for v in fp['variants']['variants']}
    assert ('Fe', 'MN4') not in combos
    assert fp['variant_plan']['n_jobs'] == len(fp['variants']['variants'])
    assert fp['variant_plan']['batches']


def test_full_reproduce_plan_variants_fallback_to_spec_systems():
    # 无 reference 时从归一 systems 生成变体(Fe/Co @ MN4 已算过应排除)
    fp = ap.full_reproduce_plan(_spec(), include_variants=True)
    combos = {(v['metal'], v['template']) for v in fp['variants']['variants']}
    assert ('Fe', 'MN4') not in combos and ('Co', 'MN4') not in combos
    assert any(v['metal'] == 'Ru' for v in fp['variants']['variants'])   # Fe 同族 4d
