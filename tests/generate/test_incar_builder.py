"""generate/incar_builder.py:INCAR 文本解析(仅校验用)+ 校验补全决策表 + 序列化。

核心不变量:validate_and_complete_incar 只产【补全项】,从不改/覆盖用户键;输入不被 mutate。
"""
import pytest

from vcstudio.generate import incar_builder as ib


# ── parse_incar ──────────────────────────────────────────────────────────────
def test_parse_int():
    assert ib.parse_incar('ENCUT = 400') == {'ENCUT': 400}


def test_parse_float_exponent():
    d = ib.parse_incar('EDIFF = 1E-5')
    assert d['EDIFF'] == pytest.approx(1e-5)


def test_parse_bool_variants():
    d = ib.parse_incar('LWAVE = .FALSE.\nLCHARG = .TRUE.\nLDIAG = .T.')
    assert d['LWAVE'] is False
    assert d['LCHARG'] is True
    assert d['LDIAG'] is True


def test_parse_magmom_multivalue_kept_verbatim():
    # 多值串整体保留,永不拆分/重排
    assert ib.parse_incar('MAGMOM = 3*5 33*0')['MAGMOM'] == '3*5 33*0'
    assert ib.parse_incar('LDAUU = 4.0 0.0 0.0')['LDAUU'] == '4.0 0.0 0.0'


def test_parse_strips_comments():
    d = ib.parse_incar('ENCUT = 400 # 截断\n! 整行注释\nNSW = 50 ! 步数')
    assert d == {'ENCUT': 400, 'NSW': 50}


def test_parse_lowercase_key_normalized_upper():
    assert ib.parse_incar('encut = 400') == {'ENCUT': 400}


def test_parse_semicolon_multi_assign():
    assert ib.parse_incar('ISTART = 0; ICHARG = 2') == {'ISTART': 0, 'ICHARG': 2}


def test_parse_string_fallback():
    d = ib.parse_incar('ALGO = Fast\nLREAL = Auto')
    assert d['ALGO'] == 'Fast'
    assert d['LREAL'] == 'Auto'


# ── has_magnetic / build_magmom(拷贝,锁定行为) ─────────────────────────────
def test_has_magnetic():
    assert ib.has_magnetic(['Fe', 'C']) is True
    assert ib.has_magnetic(['Mo', 'S']) is False


def test_build_magmom_order():
    assert ib.build_magmom(['Fe', 'C'], [1, 4]) == '1*5 4*0'


def test_build_magmom_none_when_counts_bad():
    assert ib.build_magmom(['Fe', 'C'], []) is None


# ── validate_and_complete_incar ─────────────────────────────────────────────
def test_validate_all_given_no_completion(mini_lib):
    # 非磁 + ENCUT 够 → 无补全无告警
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 500}, ['C', 'N'], [2, 4], lib_root=mini_lib)
    assert comp == {}
    assert warns == []


def test_validate_D1_missing_encut_completed(mini_lib):
    # 缺 ENCUT → 补 ceil(1.3×400/50)×50 = 550
    comp, warns = ib.validate_and_complete_incar(
        {}, ['C', 'N'], [2, 4], lib_root=mini_lib)
    assert comp['ENCUT'] == 550
    assert len(warns) == 1


def test_validate_D2_low_encut_warns_not_changed(mini_lib):
    # ENCUT=300 < C 的 ENMAX400 → 只 warn,不补 ENCUT
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 300}, ['C'], [1], lib_root=mini_lib)
    assert 'ENCUT' not in comp
    assert len(warns) == 1


def test_validate_D3_magnetic_missing_magmom_completed(mini_lib):
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 400}, ['Fe', 'C'], [1, 4], lib_root=mini_lib)
    assert comp['MAGMOM'] == '1*5 4*0'
    assert comp['ISPIN'] == 2
    assert len(warns) == 1


def test_validate_D3prime_counts_missing_no_magmom_but_ispin(mini_lib):
    # 含磁但 counts 缺失:无法补 MAGMOM,但仍补 ISPIN=2 保自旋极化(审轮3 P1-2)
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 400}, ['Fe', 'C'], [], lib_root=mini_lib)
    assert 'MAGMOM' not in comp
    assert comp.get('ISPIN') == 2
    assert len(warns) == 1


def test_incar_dict_to_str_no_duplicate_system():
    # 同时有 SYSTEM 键与 system_name 参数 → 只输出一行 SYSTEM(审轮3 P3-1)
    txt = ib.incar_dict_to_str({'SYSTEM': 'a', 'ENCUT': 400}, 'b')
    assert txt.count('SYSTEM = ') == 1
    assert 'SYSTEM = b' in txt


def test_validate_D4_user_ispin1_respected(mini_lib):
    # 含磁但用户显式 ISPIN=1 → 不补 MAGMOM/ISPIN,只 warn
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 400, 'ISPIN': 1}, ['Fe'], [1], lib_root=mini_lib)
    assert comp == {}
    assert len(warns) == 1


def test_validate_D4_float_ispin_respected(mini_lib):
    # 用户写 ISPIN=1.0(浮点)也应识别为关自旋 → D4,不补 MAGMOM(审 P1-1)
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 400, 'ISPIN': 1.0}, ['Fe'], [1], lib_root=mini_lib)
    assert comp == {}
    assert len(warns) == 1


def test_validate_does_not_mutate_input(mini_lib):
    d = {'ENCUT': 400}
    ib.validate_and_complete_incar(d, ['Fe'], [1], lib_root=mini_lib)
    assert d == {'ENCUT': 400}  # 补全项进 completions,不落输入


def test_validate_user_magmom_not_recompleted(mini_lib):
    # 用户已带 MAGMOM(即使含磁)→ 完全不动,无补全无告警
    comp, warns = ib.validate_and_complete_incar(
        {'ENCUT': 400, 'ISPIN': 2, 'MAGMOM': '2*3 4*0'}, ['Fe', 'C'], [2, 4],
        lib_root=mini_lib)
    assert comp == {}
    assert warns == []


# ── incar_dict_to_str(拷贝) ─────────────────────────────────────────────────
def test_incar_dict_to_str_bool_and_private_keys():
    txt = ib.incar_dict_to_str({'ENCUT': 400, 'LWAVE': False, '_system': 'x'}, 'job')
    assert txt.startswith('SYSTEM = job\n')
    assert 'ENCUT = 400' in txt
    assert 'LWAVE = .FALSE.' in txt
    assert '_system' not in txt  # 私有键跳过
