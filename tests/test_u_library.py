"""DFT+U 值库测试(project.u_library):建议表/l 量子数/LDAU 键按元素序组装/来源标注。"""
from vcstudio.project import u_library as ul


def test_suggest_u_returns_registered_only():
    sug = ul.suggest_u(['Fe', 'O', 'Ni'])           # O 未登记 → 跳过
    els = [s['element'] for s in sug]
    assert els == ['Fe', 'Ni']
    fe = next(s for s in sug if s['element'] == 'Fe')
    assert fe['u'] == 4.0 and fe['l'] == 2 and fe['orbital'] == '3d'


def test_suggest_u_dedup_preserves_order():
    sug = ul.suggest_u(['Ni', 'Fe', 'Ni', 'Fe'])
    assert [s['element'] for s in sug] == ['Ni', 'Fe']


def test_suggest_u_ce_is_f_shell_l3():
    sug = ul.suggest_u(['Ce'])
    assert sug[0]['l'] == 3 and sug[0]['orbital'] == '4f' and sug[0]['u'] == 5.0


def test_suggest_u_source_and_note_present():
    sug = ul.suggest_u(['Fe'])
    assert 'Materials Project' in sug[0]['source']
    assert '敏感性' in sug[0]['note']                 # 发表前敏感性测试警示


def test_suggest_u_unregistered_all_skipped():
    assert ul.suggest_u(['C', 'H', 'O']) == []


def test_ldau_keys_element_order_alignment():
    sug = ul.suggest_u(['Fe', 'Ni'])
    keys = ul.ldau_keys(sug, ['Fe', 'O', 'Ni'])       # POSCAR 序:Fe O Ni
    assert keys['LDAU'] is True and keys['LDAUTYPE'] == 2
    assert keys['LDAUL'] == '2 -1 2'                  # Fe d / O 无 U / Ni d
    assert keys['LDAUU'] == '4 0 6.4'
    assert keys['LDAUJ'] == '0 0 0'                   # Dudarev:J=0


def test_ldau_keys_order_matters():
    sug = ul.suggest_u(['Fe', 'Ni'])
    k1 = ul.ldau_keys(sug, ['Fe', 'Ni'])
    k2 = ul.ldau_keys(sug, ['Ni', 'Fe'])
    assert k1['LDAUU'] == '4 6.4' and k2['LDAUU'] == '6.4 4'   # 顺序反转 → U 序反转


def test_ldau_keys_empty_suggestions_all_off():
    keys = ul.ldau_keys([], ['C', 'O'])
    assert keys['LDAUL'] == '-1 -1' and keys['LDAUU'] == '0 0'
    assert keys['LDAU'] is True and keys['LDAUTYPE'] == 2


def test_ldau_keys_mixed_registered_unregistered():
    sug = ul.suggest_u(['Ti'])
    keys = ul.ldau_keys(sug, ['Ti', 'O'])
    assert keys['LDAUL'] == '2 -1' and keys['LDAUU'] == '4 0'
