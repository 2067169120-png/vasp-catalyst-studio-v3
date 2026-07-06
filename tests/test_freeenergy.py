"""freeenergy 测试:μ_Li/ΔG/PDS/U_L 公式 + OSZICAR 解析 + 旧目录扫描 + 物种匹配。"""
import pytest

from vcstudio.project import freeenergy as fe

# 构造能量:让公式可手算。E(S8)=-32, E(Li2S)=-8 → μ_Li = (-8 - (-32/8))/2 = -2
_MOL = {'S8': -32.0, 'Li2S': -8.0, 'Li2S2': -12.0}


def test_mu_li_formula():
    assert fe.mu_li_from_molecules(_MOL) == pytest.approx(-2.0)
    with pytest.raises(ValueError, match='mu_li'):
        fe.mu_li_from_molecules({'S8': -32.0})


def test_discharge_path_reference_and_pds():
    # slab+X 能量:设计成 参照态 G=0,后续可手算
    sys_e = {'S8': -100.0, 'Li2S8': -105.0, 'Li2S6': -95.0,
             'Li2S4': -100.0, 'Li2S2': -103.0, 'Li2S': -104.0}
    out = fe.discharge_path(sys_e, _MOL, mu_li=-2.0)
    steps = out['steps']
    assert steps[0]['label'] == 'S8*' and steps[0]['G'] == 0.0
    # Li2S8*: -105 - 2*(-2) - (-100) = -1
    assert steps[1]['G'] == pytest.approx(-1.0)
    # Li2S6*: -95 + 1*(-12) - 4*(-2) - (-100) = 1
    assert steps[2]['G'] == pytest.approx(1.0)
    assert steps[2]['sub_label'] == '1×Li2S2'
    # PDS 是逐电子最陡上坡:步2 (Li2S8→Li2S6) ΔG=+2 / Δn=2 → +1 每电子,最大
    assert out['pds_index'] == 1
    assert out['u_l'] == pytest.approx(-1.0)
    assert out['mu_li'] == pytest.approx(-2.0)


def test_discharge_path_missing_species_named():
    with pytest.raises(ValueError, match='Li2S6'):
        fe.discharge_path({'S8': -1.0, 'Li2S8': -1.0}, _MOL, mu_li=-2.0)


def test_read_e0_and_scan(tmp_path):
    d = tmp_path / 'mol_S8'
    d.mkdir()
    (d / 'OSZICAR').write_text(
        '   1 F= -.318E+02 E0= -.31800E+02  d E =0.0\n'
        '   2 F= -.320E+02 E0= -.32000E+02  d E =-.2\n', encoding='utf-8')
    (tmp_path / 'molecule_Li2S').mkdir()
    (tmp_path / 'molecule_Li2S' / 'OSZICAR').write_text(
        ' 1 T= 300 E= -8.0 F= -8.0 E0= -8.00000E+00\n', encoding='utf-8')
    (tmp_path / 'not_a_mol').mkdir()                    # 非分子目录忽略
    assert fe.read_e0(d) == pytest.approx(-32.0)
    assert fe.read_e0(tmp_path / 'not_a_mol') is None   # 无 OSZICAR
    scanned = fe.load_molecule_energies(tmp_path)
    assert scanned == {'S8': pytest.approx(-32.0), 'Li2S': pytest.approx(-8.0)}


def test_species_matching_no_prefix_collision():
    assert fe._species_in_name('Li2S', 'ads_Li2S_on_slab')
    assert not fe._species_in_name('Li2S', 'ads_Li2S8_on_slab')   # Li2S 不误配 Li2S8(前缀)
    assert not fe._species_in_name('S8', 'ads_Li2S8_on_slab')     # S8 不误配 Li2S8(后缀)
    assert fe._species_in_name('S8', 'ads_S8_on_slab')
    assert fe._species_in_name('Li2S8', 'config_li2s8_top')       # 大小写不敏感


def test_path_from_project_picks_lowest_energy(tmp_path):
    # 旧分子目录
    for name, e0 in (('mol_S8', -32.0), ('mol_Li2S', -8.0), ('mol_Li2S2', -12.0)):
        d = tmp_path / name
        d.mkdir()
        (d / 'OSZICAR').write_text(f' 1 F= {e0} E0= {e0:.5E}\n', encoding='utf-8')
    rows = [{'name': f'ads_{sp}_a', 'e_config': e} for sp, e in
            (('S8', -100.0), ('Li2S8', -105.0), ('Li2S6', -95.0),
             ('Li2S4', -100.0), ('Li2S2', -103.0), ('Li2S', -104.0))]
    rows.append({'name': 'ads_Li2S8_b', 'e_config': -104.0})      # 次稳组态应被忽略
    out = fe.path_from_project_and_molecules(rows, e_slab=-90.0,
                                             molecules_dir=tmp_path)
    assert out['steps'][1]['G'] == pytest.approx(-1.0)            # 取了 -105 那个
