"""freeenergy 测试:μ_Li/ΔG/PDS/U_L 公式 + OSZICAR 解析 + 旧目录扫描 + 物种匹配
+ 通用 CHE/ΔG 台阶引擎(free_energy_path/ladder_at_potentials/u_eq_check)。"""
import copy

import pytest

from vcstudio.project import freeenergy as fe
from vcstudio.project import reactions as R
from vcstudio.shared import manifest as manifest_mod

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


def test_pds_consistent_with_ladder_when_last_step_many_electrons():
    """末步电子数大(8 e⁻)、原始 ΔG 最大但每电子 ΔG 小:PDS/U_L 与图必须同源。

    构造:ΔG 阶梯 [0, −1, −0.5, −1, −1.2, −0.4],Δn = [2,2,2,2,8]
    → 每电子 [−0.5, +0.25, −0.25, −0.1, +0.1]:PDS = 步 1(0.25),U_L = −0.25;
    而原始 ΔG 最大是末步(+0.8)——旧 ladder_data 自行重算会高亮错那一步。
    """
    sys_e = {'S8': -100.0, 'Li2S8': -105.0, 'Li2S6': -96.5,
             'Li2S4': -89.0, 'Li2S2': -81.2, 'Li2S': -76.4}
    out = fe.discharge_path(sys_e, _MOL, mu_li=-2.0)
    gs = [s['G'] for s in out['steps']]
    assert gs == pytest.approx([0.0, -1.0, -0.5, -1.0, -1.2, -0.4])
    assert out['per_electron'] == pytest.approx([-0.5, 0.25, -0.25, -0.1, 0.1])
    assert out['pds_index'] == 1                       # 逐电子口径,不是末步(4)
    assert out['u_l'] == pytest.approx(-0.25)          # U_L = −max(ΔG/Δn e)
    # 接图:pds_index 透传后,图内高亮步 == U_L 对应步
    from vcstudio.project import charts
    lad = charts.ladder_data(out['steps'], u_l=out['u_l'], pds_index=out['pds_index'])
    assert lad['pds_index'] == 1 and lad['u_l'] == pytest.approx(-0.25)
    svg = charts.render_ladder_svg(lad)
    assert 'ΔG = +0.50 eV' in svg and 'U_L = -0.25 V' in svg


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


def test_molecule_scan_rejects_managed_not_done_or_implausible_energy(tmp_path):
    for species, state, energy in (
            ('S8', 'DONE', -32.0),
            ('Li2S', 'NEEDS_HUMAN', -8.0),
            ('Li2S2', 'DONE', 4.0)):
        d = tmp_path / f'mol_{species}'
        d.mkdir()
        (d / 'OSZICAR').write_text(f' 1 F= {energy} E0= {energy}\n', encoding='utf-8')
        m = manifest_mod.new_manifest(
            job_id=species, system=species, task_type='relax', calc_type='molecule', inputs={})
        manifest_mod.set_state(m, state)
        m['results'] = {'energy_e0_eV': energy}
        manifest_mod.save_manifest(d, m)

    assert fe.load_molecule_energies(tmp_path) == {'S8': pytest.approx(-32.0)}


def test_molecule_scan_does_not_treat_corrupt_managed_manifest_as_legacy(tmp_path):
    d = tmp_path / 'mol_Li2S'
    d.mkdir()
    (d / 'OSZICAR').write_text(
        ' 1 F= -8.0 E0= -8.000000E+00\n', encoding='utf-8')
    # Existing but malformed job.yaml identifies a managed result whose state
    # cannot be audited.  Its otherwise valid E0 must not enter μLi/ΔG.
    (d / 'job.yaml').write_text('state: [DONE\n', encoding='utf-8')

    assert fe.load_molecule_energies(tmp_path) == {}


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
    rows = [{'name': f'ads_{sp}_a', 'e_config': e, 'state': 'DONE'} for sp, e in
            (('S8', -100.0), ('Li2S8', -105.0), ('Li2S6', -95.0),
             ('Li2S4', -100.0), ('Li2S2', -103.0), ('Li2S', -104.0))]
    rows.append({'name': 'ads_Li2S8_b', 'e_config': -104.0, 'state': 'DONE'})  # 次稳忽略
    # 非 DONE(BAD_ENERGY 等)的能量绝不漏进 ΔG(审查#3):更低能量但 NEEDS_HUMAN → 忽略
    rows.append({'name': 'ads_Li2S8_bad', 'e_config': -200.0, 'state': 'NEEDS_HUMAN'})
    out = fe.path_from_project_and_molecules(rows, e_slab=-90.0,
                                             molecules_dir=tmp_path)
    assert out['steps'][1]['G'] == pytest.approx(-1.0)            # 取了 -105(DONE 里最稳)


# ═════════════════════════════════════════════════════════════════════════════
# 通用 CHE/ΔG 台阶引擎(F16)
# ═════════════════════════════════════════════════════════════════════════════
def _lis16e_energies(sys_e, mol):
    """把 discharge_path 的双字典能量转成 free_energy_path 的扁平能量:
    吸附态加 '*' 后缀(与同名沉淀分子区分),再并入 Li2S/Li2S2 分子能量。"""
    energies = {f'{k}*': v for k, v in sys_e.items()}
    energies['Li2S2'] = mol['Li2S2']
    energies['Li2S'] = mol['Li2S']
    return energies


def test_free_energy_path_regression_matches_discharge():
    """LIS_16E 走通用引擎与旧 discharge_path 数值必须逐字一致(回归对拍)。"""
    sys_e = {'S8': -100.0, 'Li2S8': -105.0, 'Li2S6': -95.0,
             'Li2S4': -100.0, 'Li2S2': -103.0, 'Li2S': -104.0}
    old = fe.discharge_path(sys_e, _MOL, mu_li=-2.0)
    new = fe.free_energy_path(R.LIS_16E, _lis16e_energies(sys_e, _MOL), mu_e=-2.0)
    assert [s['G'] for s in new['steps']] == [s['G'] for s in old['steps']]
    assert new['per_electron'] == old['per_electron']
    assert new['pds_index'] == old['pds_index']
    assert new['u_l'] == old['u_l']
    assert new['mu_e'] == old['mu_li']


def test_free_energy_path_regression_manyelectron_last_step():
    """末步 8 e⁻:逐电子口径 pds/u_l 与 discharge_path 同源(不被末步大 ΔG 误导)。"""
    sys_e = {'S8': -100.0, 'Li2S8': -105.0, 'Li2S6': -96.5,
             'Li2S4': -89.0, 'Li2S2': -81.2, 'Li2S': -76.4}
    old = fe.discharge_path(sys_e, _MOL, mu_li=-2.0)
    new = fe.free_energy_path(R.LIS_16E, _lis16e_energies(sys_e, _MOL), mu_e=-2.0)
    assert new['per_electron'] == pytest.approx([-0.5, 0.25, -0.25, -0.1, 0.1])
    assert new['pds_index'] == 1 and old['pds_index'] == 1
    assert new['u_l'] == pytest.approx(-0.25) and old['u_l'] == pytest.approx(-0.25)


def test_orr_ideal_analytic():
    """理想 ORR 催化剂各步 −1.23 eV(U=0)→ U_L=1.23、U_eq=1.23、η=0(文献解析解)。"""
    e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23,
         'O2': 4.92, 'H2O': 0.0, 'H2': 0.0}
    r = fe.free_energy_path(R.ORR_4E, e)
    assert [s['G'] for s in r['steps']] == pytest.approx([0, -1.23, -2.46, -3.69, -4.92])
    assert r['per_electron'] == pytest.approx([-1.23, -1.23, -1.23, -1.23])
    assert r['u_l'] == pytest.approx(1.23)
    assert r['u_eq'] == pytest.approx(1.23)
    assert r['eta'] == pytest.approx(0.0)
    assert r['electrode'] == 'RHE' and r['direction'] == 'reduction'


def test_orr_half_h2_and_water_trick_reference():
    """非零 H2/H2O:μ_e=½G(H2);O2 用 water trick 时 U_eq 精确落在 1.23(参考态口径正确)。"""
    h, w = -6.77, -14.22
    o2 = 2 * w - 2 * h + 4 * 1.23                # G(O2)=2G(H2O)−2G(H2)+4×1.23
    g0 = o2                                       # G_0 = E(*)+E(O2),E(*)=0
    e = {'*': 0.0, 'H2': h, 'H2O': w, 'O2': o2,
         'OOH*': (g0 - 1.23) + 0.5 * h,           # 反解各态,使各步恰 −1.23
         'O*': (g0 - 2.46) - w + h,
         'OH*': (g0 - 3.69) - w + 1.5 * h}
    r = fe.free_energy_path(R.ORR_4E, e)
    assert r['mu_e'] == pytest.approx(0.5 * h)
    assert r['u_eq'] == pytest.approx(1.23)
    assert r['per_electron'] == pytest.approx([-1.23, -1.23, -1.23, -1.23])


def test_oer_ideal_analytic_oxidation():
    """理想 OER(析氧/氧化类)各步 +1.23 eV → U_L=1.23、η=U_L−U_eq=0;含非零 ½H2 项。"""
    h, w = -6.77, -14.22
    o2 = 2 * w - 2 * h + 4 * 1.23
    g0 = 2 * w                                    # G_0 = E(*)+2E(H2O),E(*)=0
    e = {'*': 0.0, 'H2': h, 'H2O': w, 'O2': o2,
         'OH*': (g0 + 1.23) - w - 0.5 * h,        # 氧化类 +n·½h,反解各态
         'O*': (g0 + 2.46) - w - h,
         'OOH*': (g0 + 3.69) - 1.5 * h}
    r = fe.free_energy_path(R.OER_4E, e)
    assert r['direction'] == 'oxidation'
    assert r['per_electron'] == pytest.approx([1.23, 1.23, 1.23, 1.23])
    assert r['u_l'] == pytest.approx(1.23)
    assert r['u_eq'] == pytest.approx(1.23)
    assert r['eta'] == pytest.approx(0.0)


def test_her_manual_hand_calc():
    """HER 手算:ΔG(*H)=+0.10 → 台阶 [0, 0.10, 0]、U_L=−0.10、U_eq=0、η=0.10。"""
    h2 = -6.77
    e = {'*': 0.0, 'H2': h2, 'H*': 0.10 + 0.5 * h2}   # G_1 = E(H*)−½h = 0.10
    r = fe.free_energy_path(R.HER, e)
    assert [s['G'] for s in r['steps']] == pytest.approx([0.0, 0.10, 0.0])
    assert r['per_electron'] == pytest.approx([0.10, -0.10])
    assert r['u_l'] == pytest.approx(-0.10)
    assert r['u_eq'] == pytest.approx(0.0)
    assert r['eta'] == pytest.approx(0.10)
    assert r['mu_e'] == pytest.approx(0.5 * h2)


def test_co2rr_chemical_final_step_excluded_from_u_l():
    """CO2RR 末步 *CO 脱附为化学步(Δn=0):不计入 U_L,并给出中文提示。"""
    e = {'*': 0.0, 'COOH*': 0.5, 'CO*': -0.3,
         'CO2': 0.0, 'H2O': 0.0, 'CO': 0.0, 'H2': 0.0}
    r = fe.free_energy_path(R.CO2RR_TO_CO, e)
    assert r['per_electron'] == [0.5, -0.8, None]
    assert any('化学步' in w for w in r['warnings'])
    assert r['pds_index'] == 0                    # 逐电子最不利步 = 首个 PCET
    assert r['u_l'] == pytest.approx(-0.5)         # 仅两个电化学步定 U_L


def test_ladder_at_potentials_orr_flat_at_ueq():
    """多电位台阶:理想 ORR 在 U_eq 各态齐平(全热中性);键=电位,值=多体系入参。"""
    e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23,
         'O2': 4.92, 'H2O': 0.0, 'H2': 0.0}
    r = fe.free_energy_path(R.ORR_4E, e)
    lad = fe.ladder_at_potentials(r, R.ORR_4E, [0.0, r['u_eq']])
    assert set(lad) == {0.0, 1.23}
    assert [d['G'] for d in lad[0.0]] == pytest.approx([0, -1.23, -2.46, -3.69, -4.92])
    assert [d['G'] for d in lad[1.23]] == pytest.approx([0, 0, 0, 0, 0])
    assert [d['label'] for d in lad[0.0]] == ['O2', '*OOH', '*O', '*OH', 'H2O']


def test_ladder_at_potentials_lis_8e_last_step_shift():
    """Li-S 多电位平移含末步 8 e⁻:ΔG_i(U)=ΔG_i(0)+n_i·U(还原类,末态移 16·U)。"""
    sys_e = {'S8': -100.0, 'Li2S8': -105.0, 'Li2S6': -95.0,
             'Li2S4': -100.0, 'Li2S2': -103.0, 'Li2S': -104.0}
    r = fe.free_energy_path(R.LIS_16E, _lis16e_energies(sys_e, _MOL), mu_e=-2.0)
    base = [s['G'] for s in r['steps']]
    ns = [0, 2, 4, 6, 8, 16]
    lad = fe.ladder_at_potentials(r, R.LIS_16E, [0.1])
    assert [d['G'] for d in lad[0.1]] == pytest.approx([base[i] + ns[i] * 0.1 for i in range(6)])


def test_u_eq_check_within_no_warning():
    e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23,
         'O2': 4.92, 'H2O': 0.0, 'H2': 0.0}
    r = fe.free_energy_path(R.ORR_4E, e)
    chk = fe.u_eq_check(r, R.ORR_4E)
    assert chk['within'] is True and chk['warnings'] == []


def test_u_eq_check_none_expected_branch():
    e = {'*': 0.0, 'COOH*': 0.5, 'CO*': -0.3,
         'CO2': 0.0, 'H2O': 0.0, 'CO': 0.0, 'H2': 0.0}
    r = fe.free_energy_path(R.CO2RR_TO_CO, e)
    chk = fe.u_eq_check(r, R.CO2RR_TO_CO)
    assert chk['expected'] is None and chk['within'] is None and chk['warnings'] == []


def test_u_eq_check_deviation_scientific_warning():
    """张洪毅体系:计算 U_eq≈1.66 V 偏离实验 2.15–2.24;告警须科学准确(电子能口径差)。"""
    e = {'Li2S3*': 0.0, 'LiS2*': 4.34, 'Li2S2*': 0.68, 'Li2S': -8.0}
    r = fe.free_energy_path(R.LIS_ASSOC_LIS2, e, mu_e=-2.0)
    assert r['u_eq'] == pytest.approx(1.66)
    chk = fe.u_eq_check(r, R.LIS_ASSOC_LIS2)
    assert chk['within'] is False
    w = chk['warnings'][0]
    assert '电子能' in w and '2.15' in w and '1.66' in w
    assert '不代表计算出错' in w                  # 不误导用户以为算错
    assert any('电子能' in x for x in r['warnings'])   # 引擎自身 warnings 也带该提示


def test_g_corr_superposition():
    """g_corr 逐态叠加到能量:仅含该物种的态抬高对应校正,其余不变。"""
    base_e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23,
              'O2': 4.92, 'H2O': 0.0, 'H2': 0.0}
    r0 = fe.free_energy_path(R.ORR_4E, base_e)
    r1 = fe.free_energy_path(R.ORR_4E, base_e, g_corr={'OH*': 0.30})
    assert r1['thermo_corrected'] is True and r0['thermo_corrected'] is False
    g0 = [s['G'] for s in r0['steps']]
    g1 = [s['G'] for s in r1['steps']]
    assert g1[3] == pytest.approx(g0[3] + 0.30)   # *OH 态(index 3)抬高 0.30
    assert g1[0] == pytest.approx(g0[0])
    assert g1[1] == pytest.approx(g0[1])


def test_rhe_u_dependence_shifts_steps():
    """展示电位 u:还原类 ΔG_i(u)=ΔG_i(0)+n_i·u;u_l/u_eq 为内禀量与 u 无关。"""
    e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23,
         'O2': 4.92, 'H2O': 0.0, 'H2': 0.0}
    r0 = fe.free_energy_path(R.ORR_4E, e, u=0.0)
    rU = fe.free_energy_path(R.ORR_4E, e, u=0.5)
    ns = [0, 1, 2, 3, 4]
    g0 = [s['G'] for s in r0['steps']]
    assert [s['G'] for s in rU['steps']] == pytest.approx([g0[i] + ns[i] * 0.5 for i in range(5)])
    assert rU['u_l'] == pytest.approx(r0['u_l'])
    assert rU['u_eq'] == pytest.approx(r0['u_eq'])


def test_free_energy_path_missing_species_named():
    e = {'*': 0.0, 'OOH*': 3.69, 'O2': 4.92, 'H2O': 0.0, 'H2': 0.0}   # 缺 O*/OH*
    with pytest.raises(ValueError, match=r'O\*'):
        fe.free_energy_path(R.ORR_4E, e)


def test_free_energy_path_missing_molecule_named():
    e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23, 'H2O': 0.0, 'H2': 0.0}  # 缺 O2
    with pytest.raises(ValueError, match='O2'):
        fe.free_energy_path(R.ORR_4E, e)


def test_free_energy_path_missing_h2_for_rhe_named():
    e = {'*': 0.0, 'OOH*': 3.69, 'O*': 2.46, 'OH*': 1.23, 'O2': 4.92, 'H2O': 0.0}  # 缺 H2
    with pytest.raises(ValueError, match='H2'):
        fe.free_energy_path(R.ORR_4E, e)          # 无 mu_e 又无 H2 → 点名


def test_free_energy_path_validates_spec():
    """引擎入口对非法 spec 防御:破坏守恒的 spec 直接中文报错,不静默算错。"""
    bad = copy.deepcopy(R.ORR_4E)
    bad['steps'][2]['coadsorbates_or_gas'] = []
    with pytest.raises(ValueError, match='不守恒'):
        fe.free_energy_path(bad, {'*': 0, 'OOH*': 0, 'O*': 0, 'OH*': 0,
                                  'O2': 0, 'H2O': 0, 'H2': 0})
