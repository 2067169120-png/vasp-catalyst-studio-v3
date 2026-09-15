"""跨引擎一致性/混引擎闸测试:反应能对比(一致 ok / 超差点名)+ mixing_gate。

核心科学点:绝对能量跨引擎相差巨大(赝势/基组零点不同),唯反应能可比。
合成数据令两引擎绝对能相差几百 eV,但反应能一致 → 应判 ok。
"""
from vcstudio.engines import mixing_gate, reference_consistency_check

# 反应:H2O 生成能 = E(H2O) − E(H2) − ½E(O2)
RXN = [{'name': 'H2O_formation', 'stoich': {'H2O': 1, 'H2': -1, 'O2': -0.5}}]


def _results(vasp, cp2k):
    out = []
    for eng, d in (('vasp', vasp), ('cp2k', cp2k)):
        for sp, e in d.items():
            out.append({'engine': eng, 'species': sp, 'energy_ev': e})
    return out


def test_consistency_single_engine_ok():
    res = [{'engine': 'vasp', 'species': 'H2O', 'energy_ev': -14.2}]
    out = reference_consistency_check(res, RXN)
    assert out['ok'] is True and '单一引擎' in out['note']


def test_consistency_absolute_differ_but_reaction_consistent():
    # 绝对能相差数百 eV(赝势/基组零点不同),但反应能都 = -2.52 eV
    vasp = {'H2O': -14.22, 'H2': -6.77, 'O2': -9.86}       # rxn = -2.52
    cp2k = {'H2O': -467.11, 'H2': -31.60, 'O2': -865.98}   # rxn = -2.52
    out = reference_consistency_check(_results(vasp, cp2k), RXN)
    assert out['ok'] is True
    assert out['pairs'][0]['delta_ev'] < 1e-3
    assert '通过' in out['note']


def test_consistency_superdiff_names_offenders():
    vasp = {'H2O': -14.22, 'H2': -6.77, 'O2': -9.86}       # rxn = -2.52
    cp2k = {'H2O': -470.10, 'H2': -31.60, 'O2': -865.98}   # rxn = -5.51 → diff 2.99
    out = reference_consistency_check(_results(vasp, cp2k), RXN)
    assert out['ok'] is False
    assert '不可并列比较' in out['note']
    assert 'H2O_formation' in out['note']
    assert any('vasp' in str(p['engines']) and 'cp2k' in str(p['engines'])
               for p in out['pairs'])


def test_consistency_tolerance_boundary():
    vasp = {'H2O': -10.0, 'H2': 0.0, 'O2': 0.0}            # rxn = -10
    cp2k = {'H2O': -10.04, 'H2': 0.0, 'O2': 0.0}           # rxn = -10.04 → diff 0.04
    assert reference_consistency_check(_results(vasp, cp2k), RXN,
                                       tolerance_ev=0.05)['ok'] is True
    assert reference_consistency_check(_results(vasp, cp2k), RXN,
                                       tolerance_ev=0.03)['ok'] is False


def test_consistency_missing_species_skipped():
    # cp2k 缺 O2 → 该反应无法对比,跳过
    vasp = {'H2O': -14.22, 'H2': -6.77, 'O2': -9.86}
    cp2k = {'H2O': -467.11, 'H2': -459.66}
    out = reference_consistency_check(_results(vasp, cp2k), RXN)
    assert out['ok'] is False and '无任何共同' in out['note']


def test_consistency_no_reactions_blocks_two_engines():
    vasp = {'H2O': -14.0}
    cp2k = {'H2O': -467.0}
    out = reference_consistency_check(_results(vasp, cp2k), [])
    assert out['ok'] is False and '先行闸默认不放行' in out['note']


def test_consistency_pairs_structure():
    vasp = {'H2O': -10.0, 'H2': 0.0, 'O2': 0.0}
    cp2k = {'H2O': -10.0, 'H2': 0.0, 'O2': 0.0}
    out = reference_consistency_check(_results(vasp, cp2k), RXN)
    p = out['pairs'][0]
    assert set(p) == {'reaction', 'engines', 'delta_ev', 'ok'}
    assert p['reaction'] == 'H2O_formation'
    assert tuple(sorted(p['engines'])) == ('cp2k', 'vasp')


def test_consistency_three_engines_all_pairs():
    res = []
    for eng, base in (('vasp', -10.0), ('cp2k', -400.0), ('castep', -250.0)):
        # 反应能都 = base_H2O - 0 - 0,令三者反应能一致(=-1.0)
        res += [{'engine': eng, 'species': 'H2O', 'energy_ev': base - 1.0},
                {'engine': eng, 'species': 'H2', 'energy_ev': base},
                {'engine': eng, 'species': 'O2', 'energy_ev': 0.0}]
    rxn = [{'name': 'r', 'stoich': {'H2O': 1, 'H2': -1}}]
    out = reference_consistency_check(res, rxn)
    assert out['ok'] is True and len(out['pairs']) == 3      # C(3,2)=3 对


# ── mixing_gate ───────────────────────────────────────────────────────────────
def test_mixing_gate_single_engine_ok():
    out = mixing_gate({'vasp'})
    assert out['ok'] is True and '无混用风险' in out['note']


def test_mixing_gate_multi_reject_default():
    out = mixing_gate({'vasp', 'cp2k'})
    assert out['ok'] is False and '默认拒绝' in out['note']
    assert 'cp2k' in out['note'] and 'vasp' in out['note']


def test_mixing_gate_multi_pass_when_consistency():
    out = mixing_gate({'vasp', 'cp2k'}, consistency_passed=True,
                      comparison_scope='relative_results')
    assert out['ok'] is True and '记录在案' in out['note']


def test_mixing_gate_consistency_never_allows_raw_cross_engine_subtraction():
    out = mixing_gate({'vasp', 'cp2k'}, consistency_passed=True)
    assert out['ok'] is False
    assert '原始能量' in out['note']


def test_consistency_conflicting_duplicate_energy_fails_closed():
    results = _results(
        {'H2O': -14.22, 'H2': -6.77, 'O2': -9.86},
        {'H2O': -467.11, 'H2': -31.60, 'O2': -865.98})
    results.append({'engine': 'cp2k', 'species': 'H2O', 'energy_ev': -999.0})
    out = reference_consistency_check(results, RXN)
    assert out['ok'] is False and '不唯一' in out['note']


def test_mixing_gate_empty_ok():
    assert mixing_gate(set())['ok'] is True


def test_mixing_gate_case_insensitive():
    assert mixing_gate({'VASP', 'vasp'})['ok'] is True       # 去重后单一引擎
