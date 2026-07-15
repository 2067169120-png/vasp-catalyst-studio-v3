"""C2 结构 3D 预览纯函数测试:POSCAR 坐标解析 + 分子-衬底间隙分析。数值全部手算。"""
import math

import pytest

from vcstudio.generate import structure_view as sv


def _poscar(coords_block: str, *, elems='C S', counts='4 2', mode='Direct',
            selective=False, scale='1.0',
            cell='10.0 0.0 0.0\n0.0 10.0 0.0\n0.0 0.0 30.0'):
    sel = 'Selective dynamics\n' if selective else ''
    return (f'test system\n{scale}\n{cell}\n{elems}\n{counts}\n'
            f'{sel}{mode}\n{coords_block}')


# 4 个 C @ z=10(面内 2×2),2 个 S @ z=13.0/14.9(S-S 1.9 Å 正常键长)
# → 垂直间隙 3.0,最近对 3.0(正上方)
_DIRECT_OK = _poscar(
    '0.05 0.05 0.333333333333 T T T\n'
    '0.55 0.05 0.333333333333 T T T\n'
    '0.05 0.55 0.333333333333 T T T\n'
    '0.55 0.55 0.333333333333 T T T\n'
    '0.05 0.05 0.433333333333 T T T\n'
    '0.05 0.05 0.496666666667 T T T\n', selective=True)


def test_parse_positions_direct_selective():
    p = sv.parse_positions(_DIRECT_OK)
    assert p['elements'] == ['C', 'C', 'C', 'C', 'S', 'S']
    assert p['coords'][0] == pytest.approx([0.5, 0.5, 10.0])
    assert p['coords'][4] == pytest.approx([0.5, 0.5, 13.0])
    assert p['coords'][5] == pytest.approx([0.5, 0.5, 14.9])
    assert p['cell'][2] == pytest.approx([0.0, 0.0, 30.0])


def test_parse_positions_cartesian_with_scale():
    text = _poscar('0.25 0.25 5.0\n0.25 0.25 6.5\n',
                   elems='C S', counts='1 1', mode='Cartesian', scale='2.0')
    p = sv.parse_positions(text)
    # Cartesian 坐标 × 缩放因子 2.0
    assert p['coords'][0] == pytest.approx([0.5, 0.5, 10.0])
    assert p['coords'][1] == pytest.approx([0.5, 0.5, 13.0])
    # 晶格也 ×2(read_cell_vectors 既有语义)
    assert p['cell'][0][0] == pytest.approx(20.0)


def test_parse_positions_insufficient_coord_lines():
    text = _poscar('0.1 0.1 0.1\n', elems='C S', counts='1 2')  # 需 3 行只给 1
    with pytest.raises(ValueError):
        sv.parse_positions(text)


def test_parse_positions_garbage_and_vasp4():
    with pytest.raises(ValueError):
        sv.parse_positions('random\ntext\n')
    # VASP4(第6行是数字,无元素符号)→ 无法着色/出 XYZ,显式报错
    vasp4 = 'sys\n1.0\n10 0 0\n0 10 0\n0 0 30\n4 2\nDirect\n' + '0.1 0.1 0.1\n' * 6
    with pytest.raises(ValueError):
        sv.parse_positions(vasp4)


# ── 间隙分析 ────────────────────────────────────────────────────────────────
def _gap_of(text):
    p = sv.parse_positions(text)
    return sv.analyze_gap(p['elements'], p['coords'], p['cell'])


def test_analyze_gap_ok_3angstrom():
    g = _gap_of(_DIRECT_OK)
    assert g['separated'] is True
    assert g['n_mol'] == 2 and g['n_slab'] == 4
    assert g['mol_formula'] == 'S2'
    assert g['vertical_gap'] == pytest.approx(3.0)
    assert g['min_dist'] == pytest.approx(3.0)
    assert g['pair']['elem_i'] == 'S' and g['pair']['elem_j'] == 'C'
    assert g['level'] == 'ok'


def test_analyze_gap_warn_below_2():
    # S @ z=11.9 → 垂直间隙 1.9(≥1.8 可分离),最近对 1.9 < 2.0 → warn
    text = _poscar('0.05 0.05 0.333333333333\n0.05 0.05 0.396666666667\n',
                   elems='C S', counts='1 1')
    g = _gap_of(text)
    assert g['separated'] is True
    assert g['min_dist'] == pytest.approx(1.9, abs=1e-6)
    assert g['level'] == 'warn'


def test_analyze_gap_crash_fused_s8_case():
    # 历史事故复刻:S 距骨架 S 仅 1.24 Å,z 间隙 <1.8 分离失败 →
    # 共价半径兜底:1.24 < 0.6×(1.05+1.05)=1.26 → crash
    text = _poscar('0.05 0.05 0.333333333333\n0.05 0.05 0.374666666667\n',
                   elems='S S', counts='1 1')
    g = _gap_of(text)
    assert g['separated'] is False
    assert g['level'] == 'crash'
    assert g['min_dist'] == pytest.approx(1.24, abs=1e-6)
    assert any('重叠' in n or '过近' in n for n in g['notes'])


def test_analyze_gap_periodic_image_in_plane():
    # 分子 S 在 x=9.5 贴胞边,衬底 C 在 x=0.5:胞内 dx=9.0,跨像 dx=1.0
    # → 最近对 = sqrt(1²+3²)=√10,不看周期像会错报 sqrt(81+9)
    text = _poscar('0.05 0.05 0.333333333333\n0.95 0.05 0.433333333333\n',
                   elems='C S', counts='1 1')
    g = _gap_of(text)
    assert g['separated'] is True
    assert g['min_dist'] == pytest.approx(math.sqrt(10.0), abs=1e-4)  # 模块 round 4 位


def test_analyze_gap_pure_slab_not_separated():
    # 全部原子同层(z 相近),无 ≥1.8 间隙,也无原子重叠 → 不分离,level None
    text = _poscar('0.1 0.1 0.333\n0.5 0.1 0.334\n0.1 0.5 0.333\n0.5 0.5 0.334\n',
                   elems='C', counts='4')
    g = _gap_of(text)
    assert g['separated'] is False
    assert g['level'] is None
    assert g['min_dist'] is None or g['min_dist'] > 1.0
    assert g['notes']  # 有说明


def test_analyze_gap_bonded_h_not_false_alarm():
    # C-H 键 1.09 Å 是正常成键,共价半径判据 0.6×(0.76+0.31)=0.64 不应误报 crash
    text = _poscar('0.05 0.05 0.333333333333\n0.05 0.05 0.369666666667\n',
                   elems='C H', counts='1 1')
    g = _gap_of(text)
    assert g['level'] != 'crash'


def test_analyze_gap_unwrap_wrapped_slab_atom():
    """CONTCAR 常态:slab 底层原子弛豫越过 z=0 被回卷到 frac≈0.998 →
    必须按周期展开后再分离,否则误把回卷原子当分子、假报 ok。"""
    # slab C4:z=0.5/1.0/2.0 + 回卷原子 29.95(真实位置 -0.05);mol S2:z=5.0/6.9
    text = _poscar(
        '0.05 0.05 0.016666666667\n'
        '0.55 0.05 0.033333333333\n'
        '0.05 0.55 0.066666666667\n'
        '0.55 0.55 0.998333333333\n'   # 回卷的 slab 底层原子
        '0.05 0.05 0.166666666667\n'
        '0.05 0.05 0.230000000000\n')
    g = _gap_of(text)
    assert g['separated'] is True
    assert g['n_mol'] == 2 and g['n_slab'] == 4
    assert g['mol_formula'] == 'S2'
    assert g['vertical_gap'] == pytest.approx(3.0, abs=1e-4)  # 5.0 - 2.0
    assert g['level'] == 'ok'
    assert any('回卷' in n for n in g['notes'])


def test_analyze_gap_crash_across_z_boundary():
    """跨 z 边界撞车:slab S @ z=0.44,mol S 回卷前 @ z=29.2(真实 -0.8 侧),
    真实距离 1.24 Å——展开后必须报 crash,不看周期会假报 28.76 Å 'ok'。"""
    text = _poscar('0.05 0.05 0.014666666667\n0.05 0.05 0.973333333333\n',
                   elems='S S', counts='1 1')
    g = _gap_of(text)
    assert g['separated'] is False
    assert g['level'] == 'crash'
    assert g['min_dist'] == pytest.approx(1.24, abs=1e-4)


def test_analyze_gap_intra_molecule_fusion_still_crashes():
    """分离成功但分子内部融合(S-S 1.24 Å):重叠扫描必须无条件跑,
    否则模板坏在分子内部时安全门漏网。"""
    text = _poscar(
        '0.05 0.05 0.333333333333\n0.55 0.05 0.333333333333\n'
        '0.05 0.55 0.333333333333\n0.55 0.55 0.333333333333\n'
        '0.05 0.05 0.433333333333\n0.05 0.05 0.474666666667\n')  # S@13.0/14.24
    g = _gap_of(text)
    assert g['separated'] is True
    assert g['min_dist'] == pytest.approx(3.0)   # 分子-衬底距离仍如实报告
    assert g['level'] == 'crash'                  # 但内部融合压成 crash
    assert g['clash'] is not None
    assert g['clash']['dist'] == pytest.approx(1.24, abs=1e-4)
    assert g['clash']['elem_i'] == 'S' and g['clash']['elem_j'] == 'S'


# ── structure_view 门面 ─────────────────────────────────────────────────────
def test_structure_view_full():
    v = sv.structure_view(_DIRECT_OK)
    assert v['natoms'] == 6
    assert v['formula'] == 'C4 S2'
    lines = v['xyz'].splitlines()
    assert lines[0].strip() == '6'
    assert len(lines) == 2 + 6
    assert lines[2].split()[0] == 'C'
    assert float(lines[6].split()[3]) == pytest.approx(13.0)  # 第一个 S 的 z
    assert v['gap']['level'] == 'ok'


def test_structure_view_raises_on_garbage():
    with pytest.raises(ValueError):
        sv.structure_view('')
