"""VASPsol 键测试(incar_builder.vaspsol_keys 独立函数):LSOL/EB_K + advisor 文本。"""
from vcstudio.generate import incar_builder as ib


def test_vaspsol_keys_enabled_default_eb_k():
    keys = ib.vaspsol_keys(True)
    assert keys == {'LSOL': True, 'EB_K': 78.4}


def test_vaspsol_keys_custom_eb_k():
    keys = ib.vaspsol_keys(True, eb_k=37.5)               # 乙腈
    assert keys['LSOL'] is True and keys['EB_K'] == 37.5


def test_vaspsol_keys_disabled_only_lsol_false():
    keys = ib.vaspsol_keys(False)
    assert keys == {'LSOL': False}
    assert 'EB_K' not in keys


def test_vaspsol_keys_default_enabled():
    assert ib.vaspsol_keys()['LSOL'] is True              # 默认 enabled=True


def test_vaspsol_advisory_mentions_patch_and_silent_trap():
    assert '补丁' in ib.VASPSOL_ADVISORY and '编译' in ib.VASPSOL_ADVISORY
    assert '静默' in ib.VASPSOL_ADVISORY                  # 标准 VASP 静默真空陷阱


def test_vaspsol_keys_does_not_mutate_or_leak():
    # 独立函数:返回纯键 dict,不碰任何输入
    a = ib.vaspsol_keys(True)
    b = ib.vaspsol_keys(True, eb_k=20.0)
    assert a['EB_K'] == 78.4 and b['EB_K'] == 20.0        # 两次互不影响
