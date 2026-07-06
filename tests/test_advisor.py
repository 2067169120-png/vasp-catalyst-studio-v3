"""方法学顾问测试:每条规则的触发/不触发(纯函数,规则来自网研核对的 VASP Wiki 口径)。"""
from vcstudio.project import advisor


def _names(warns):
    return [n for _, n, _ in warns]


_GAS_O2 = {'elements': ['O'], 'counts': [2],
           'cell': [[12, 0, 0], [0, 12, 0], [0, 0, 12]]}


def test_gas_ref_smearing_p0():
    # VASP 默认(ISMEAR/SIGMA 都缺)正好踩坑 → 报
    w = advisor.advise({'ENCUT': 500, 'ISPIN': 2, 'NUPDOWN': 2}, gas=_GAS_O2)
    assert 'GAS_REF_SMEARING' in _names(w)
    # 正确设置 → 不报
    ok = advisor.advise({'ENCUT': 500, 'ISMEAR': 0, 'SIGMA': 0.05, 'ISPIN': 2,
                         'NUPDOWN': 2}, gas=_GAS_O2)
    assert 'GAS_REF_SMEARING' not in _names(ok)
    # 无气相参考 → 不报
    assert 'GAS_REF_SMEARING' not in _names(advisor.advise({'ENCUT': 500}))


def test_open_shell_spin_p0():
    w = advisor.advise({'ENCUT': 500, 'ISMEAR': 0, 'SIGMA': 0.05}, gas=_GAS_O2)
    assert 'GAS_REF_OPEN_SHELL_SPIN' in _names(w)          # O2 + 默认 ISPIN=1 → 报
    ok = advisor.advise({'ENCUT': 500, 'ISMEAR': 0, 'SIGMA': 0.05, 'ISPIN': 2,
                         'NUPDOWN': 2}, gas=_GAS_O2)
    assert 'GAS_REF_OPEN_SHELL_SPIN' not in _names(ok)
    # 闭壳层分子(H2O)不报
    h2o = {'elements': ['H', 'O'], 'counts': [2, 1], 'cell': _GAS_O2['cell']}
    assert 'GAS_REF_OPEN_SHELL_SPIN' not in _names(
        advisor.advise({'ENCUT': 500, 'ISMEAR': 0, 'SIGMA': 0.05}, gas=h2o))


def test_encut_unify_p0():
    w = advisor.advise({'ISMEAR': 0}, unified_encut=520)
    assert 'ENCUT_UNIFY' in _names(w)
    assert any('520' in m for _, n, m in w if n == 'ENCUT_UNIFY')
    assert 'ENCUT_UNIFY' not in _names(advisor.advise({'ENCUT': 500}, unified_encut=520))


def test_dipole_rules():
    w = advisor.advise({'ENCUT': 500, 'LDIPOL': True})
    assert 'LDIPOL_WITHOUT_IDIPOL' in _names(w)            # P0:LDIPOL 无 IDIPOL
    assert 'LDIPOL_WITHOUT_DIPOL' in _names(w)             # P2:无 DIPOL
    ok = advisor.advise({'ENCUT': 500, 'LDIPOL': True, 'IDIPOL': 3, 'DIPOL': '0.5 0.5 0.45'})
    assert 'LDIPOL_WITHOUT_IDIPOL' not in _names(ok)
    # 有吸附组态但完全没配偶极 → P1 提示
    w2 = advisor.advise({'ENCUT': 500}, has_configs=True)
    assert 'ADS_SLAB_NO_DIPOLE' in _names(w2)
    assert 'ADS_SLAB_NO_DIPOLE' not in _names(
        advisor.advise({'ENCUT': 500, 'IDIPOL': 3}, has_configs=True))


def test_tetrahedron_relax_and_box():
    w = advisor.advise({'ENCUT': 500, 'ISMEAR': -5, 'NSW': 100})
    assert 'TETRAHEDRON_RELAX' in _names(w)
    assert 'TETRAHEDRON_RELAX' not in _names(advisor.advise({'ENCUT': 500, 'ISMEAR': -5}))
    small = {'elements': ['O'], 'counts': [2], 'cell': [[8, 0, 0], [0, 12, 0], [0, 0, 12]]}
    w2 = advisor.advise({'ENCUT': 500, 'ISMEAR': 0, 'SIGMA': 0.05, 'ISPIN': 2,
                         'NUPDOWN': 2}, gas=small)
    assert 'GAS_BOX_TOO_SMALL' in _names(w2)


def test_o2_nupdown_p2_and_priority_order():
    w = advisor.advise({'ENCUT': 500, 'ISMEAR': 0, 'SIGMA': 0.05, 'ISPIN': 2}, gas=_GAS_O2)
    assert 'O2_NUPDOWN' in _names(w)
    pris = [p for p, _, _ in w]
    assert pris == sorted(pris)                            # P0 在前


def test_create_project_returns_advisories(tmp_path):
    """集成:默认 INCAR(无 ENCUT/ISMEAR)建 O2 参考项目 → P0 建议随结果返回。"""
    from vcstudio.project import adsorption
    lib = tmp_path / 'lib'
    for el, enmax in (('C', 273.214), ('O', 400.0)):
        (lib / el).mkdir(parents=True)
        (lib / el / 'POTCAR').write_text(
            f' fake PAW_PBE {el}\n   TITEL  = PAW_PBE {el} 08Apr2002\n'
            f'   ENMAX  =  {enmax}; ENMIN = 200.000 eV\n', encoding='utf-8')
    slab = tmp_path / 'slab.vasp'
    slab.write_text('C slab\n1.0\n10 0 0\n0 10 0\n0 0 14\nC\n1\nCartesian\n0 0 0\n',
                    encoding='utf-8')
    o2 = tmp_path / 'O2.vasp'
    o2.write_text('O2\n1.0\n12 0 0\n0 12 0\n0 0 12\nO\n2\nCartesian\n0 0 0\n0 0 1.2\n',
                  encoding='utf-8')
    incar = tmp_path / 'INCAR'
    incar.write_text('EDIFF = 1E-5\nNSW = 100\nIBRION = 2\n', encoding='utf-8')
    res = adsorption.create_project(tmp_path / 'proj', 'p1', clean_poscar=str(slab),
                                    config_poscars=[str(slab)], incar_path=str(incar),
                                    ref_poscar=str(o2), lib_root=str(lib))
    names = [n for _, n, _ in res['advisories']]
    assert 'ENCUT_UNIFY' in names and 'GAS_REF_SMEARING' in names
    assert 'GAS_REF_OPEN_SHELL_SPIN' in names
    # 统一 ENCUT 按并集 max ENMAX=400 → 1.3×400=520 → 向上取 50 整数倍 = 550(同 incar_builder 公式)
    assert any('550' in m for _, n, m in res['advisories'] if n == 'ENCUT_UNIFY')