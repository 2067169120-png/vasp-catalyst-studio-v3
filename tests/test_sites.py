"""吸附位点枚举与摆放测试(F5):语义位点 / 高度 / 旋转 / 太近拒绝 / POSCAR 往返。"""
import sys

import numpy as np
import pytest

from vcstudio.generate import poscar, sac_builder as sb, sites
from vcstudio.generate.structure_view import parse_positions


@pytest.fixture(autouse=True)
def _keep_numpy_in_sys_modules():
    """test_kpoints 会从 sys.modules pop 'numpy';本文件重度用 numpy,排其后运行会重载
    numpy 污染 sys.modules 击穿 native_charts 修复。放回收集期同一对象即可(见
    test_native_charts._heal_numpy_sys_modules 同一手法)。"""
    sys.modules.setdefault('numpy', np)
    yield


def _slab_mn4(metal='Fe'):
    r = sb.build_sac('MN4', metal)
    return r['poscar'], r['site_indices']


def _counts(text):
    els, cnts = poscar.parse_poscar_species(text)
    return dict(zip(els, cnts))


# ── 位点枚举 ─────────────────────────────────────────────────────────────────
def test_enumerate_sites_semantics_mn4():
    slab, si = _slab_mn4()
    ss = sites.enumerate_sac_sites(slab, si)
    kinds = [s['kind'] for s in ss]
    # MN4:1 金属顶 + 4 配位顶 + 4 桥 + 1 hollow = 10
    assert len(ss) == 10
    assert kinds.count('top_metal') == 1
    assert kinds.count('top_coord') == 4
    assert kinds.count('bridge') == 4
    assert kinds.count('hollow') == 1


def test_top_metal_position_matches_metal_frac():
    slab, si = _slab_mn4()
    p = parse_positions(slab)
    metal_frac = sb.cart_to_frac(np.array(p['coords'][si['metal']]), np.array(p['cell']))
    top = [s for s in sites.enumerate_sac_sites(slab, si) if s['kind'] == 'top_metal'][0]
    assert top['position'] == pytest.approx([float(x) for x in metal_frac])


def test_bridge_is_midpoint():
    slab, si = _slab_mn4()
    p = parse_positions(slab)
    cell = np.array(p['cell'])
    ss = sites.enumerate_sac_sites(slab, si)
    bridge = [s for s in ss if s['kind'] == 'bridge'][0]
    # 桥位分数坐标转回笛卡尔应为金属与某配位原子中点
    cart = sb.frac_to_cart(np.array(bridge['position']), cell)
    m = np.array(p['coords'][si['metal']])
    mids = [(m + np.array(p['coords'][c])) / 2 for c in si['coord']]
    assert any(np.allclose(cart, mid) for mid in mids)


# ── 摆放:高度 / 旋转 / 合并 / 往返 ──────────────────────────────────────────
def test_place_adsorbate_rotations_and_merge():
    slab, si = _slab_mn4()
    top = [s for s in sites.enumerate_sac_sites(slab, si) if s['kind'] == 'top_metal'][0]
    outs = sites.place_adsorbate(slab, 'Li2S4', top, height=2.2,
                                 orientation='s_down', rotations=(0, 90, 180))
    assert len(outs) == 3
    base = _counts(slab)
    for text in outs:
        c = _counts(text)
        # 合并 = slab + Li2S4(Li2 S4)
        assert c['Li'] == 2 and c['S'] == base.get('S', 0) + 4
        assert c['Fe'] == 1 and c['C'] == base['C']
        p = parse_positions(text)                          # 往返解析合法
        assert len(p['coords']) == sum(base.values()) + 6


def test_place_adsorbate_height_controls_clearance():
    slab, si = _slab_mn4()
    top = [s for s in sites.enumerate_sac_sites(slab, si) if s['kind'] == 'top_metal'][0]
    text = sites.place_adsorbate(slab, 'CO', top, height=3.0, orientation='auto')[0]
    p = parse_positions(text)
    slab_z = max(c[2] for c in p['coords'][:sum(_counts(slab).values())])
    mol_z = min(c[2] for c in p['coords'][sum(_counts(slab).values()):])
    # 分子最低原子在 slab 顶之上 ~height(金属顶 z=10.4,间距 3.0)
    assert mol_z - 10.4 == pytest.approx(3.0, abs=1e-3)
    assert mol_z > slab_z - 1.0


def test_place_adsorbate_rejects_when_too_close():
    slab, si = _slab_mn4()
    top = [s for s in sites.enumerate_sac_sites(slab, si) if s['kind'] == 'top_metal'][0]
    with pytest.raises(ValueError, match='拒绝'):
        sites.place_adsorbate(slab, 'Li2S4', top, height=0.3,
                              orientation='s_down', rotations=(0,))


def test_place_adsorbate_min_surface_distance_respected():
    slab, si = _slab_mn4()
    top = [s for s in sites.enumerate_sac_sites(slab, si) if s['kind'] == 'top_metal'][0]
    text = sites.place_adsorbate(slab, 'S8', top, height=2.5, orientation='auto')[0]
    p = parse_positions(text)
    n_slab = sum(_counts(slab).values())
    cell = np.array(p['cell'])
    mol = np.array(p['coords'][n_slab:])
    sl = np.array(p['coords'][:n_slab])
    dmin = sites._min_mol_slab_dist(mol, sl, cell)
    assert dmin >= sites.MIN_SURFACE_DIST


# ── 取向 ─────────────────────────────────────────────────────────────────────
def test_orientation_anchor_selection():
    els = ['Li', 'Li', 'S', 'S', 'S', 'S']
    assert els[sites._choose_anchor(els, 's_down')] == 'S'
    assert els[sites._choose_anchor(els, 'li_down')] == 'Li'
    assert els[sites._choose_anchor(els, 'auto')] == 'S'       # 含 S 优先 S
    assert sites._choose_anchor(els, {'anchor_index': 1}) == 1
    assert els[sites._choose_anchor(els, {'anchor_element': 'Li'})] == 'Li'


def test_orientation_auto_picks_li_when_no_s():
    els = ['Li', 'O', 'H']
    assert els[sites._choose_anchor(els, 'auto')] == 'Li'


def test_orientation_invalid_raises():
    with pytest.raises(ValueError, match='未知 orientation'):
        sites._choose_anchor(['S'], 'sideways')
    with pytest.raises(ValueError, match='不含锚定元素'):
        sites._choose_anchor(['C', 'O'], 's_down')


# ── 批量 ─────────────────────────────────────────────────────────────────────
def test_adsorption_batch_family_and_naming():
    slab, si = _slab_mn4()
    n_sites = len(sites.enumerate_sac_sites(slab, si))     # 10
    batch = sites.adsorption_batch(slab, si, ['Li2S4', 'S8'],
                                   height=2.5, rotations=(0, 180))
    assert len(batch) == n_sites * 2 * 2                   # 位点×分子×旋转
    assert batch[0]['name'] == 'Li2S4@top_metal_r0'
    assert batch[1]['name'] == 'Li2S4@top_metal_r180'
    for e in batch:
        assert e['poscar'] is not None                     # height 足够,全 accepted
        assert e['molecule'] in ('Li2S4', 'S8')


def test_adsorption_batch_records_rejected():
    slab, si = _slab_mn4()
    batch = sites.adsorption_batch(slab, si, ['Li2S4'], height=0.3, rotations=(0,))
    # height 太小 → 全部拒绝,poscar=None 且带 note
    assert all(e['poscar'] is None for e in batch)
    assert all('拒绝' in e['note'] for e in batch)
