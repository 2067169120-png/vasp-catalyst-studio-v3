"""CI-NEB 生成端测试(generate.neb_builder):插值 wrap 正确性 / 重叠与端点不一致拒绝 /
标准目录布局 / INCAR 只补不改 / IMAGES 一致性 / manifest / preflight 告警。"""
import os

import pytest

from vcstudio.generate import neb_builder as nb
from vcstudio.generate.structure_view import parse_positions
from vcstudio.shared import manifest as mf


# ── 端点构造(6 Å 立方胞,2×H;分数坐标便于校验插值) ──
def _poscar(f0x, f1x, *, comment='NEB', elem='H', cell=6.0):
    return (f'{comment}\n1.0\n{cell} 0 0\n0 {cell} 0\n0 0 {cell}\n{elem}\n2\n'
            f'Direct\n{f0x:.4f} 0.5 0.5\n{f1x:.4f} 0.5 0.5\n')


_INI = _poscar(0.20, 0.80)
_FIN = _poscar(0.30, 0.70)
_INCAR = 'SYSTEM = neb test\nENCUT = 400\nISYM = 0\nNSW = 300\n'


def _fake_potcar(elements):
    """注入用 POTCAR:每物种一段 TITEL(过 _potcar_gate 完整性检查)。"""
    return ''.join(f'  PAW_PBE {e}\n   TITEL  = PAW_PBE {e}\n   ENMAX  = 250.0\n' for e in elements)


# ── interpolate_images:数量 / 线性 / wrap 短路径 / 重叠 / 端点一致性 ──
def test_interpolate_returns_n_intermediate_images():
    imgs = nb.interpolate_images(_INI, _FIN, 5)
    assert len(imgs) == 5                          # 仅中间 image,不含两端


def test_interpolate_linear_midpoint_no_wrap():
    # 单中间 image(t=0.5):atom0 0.20→0.30 → 0.25(cart 1.5);atom1 0.80→0.70 → 0.75
    img = nb.interpolate_images(_INI, _FIN, 1)[0]
    x0 = parse_positions(img)['coords'][0][0]
    assert x0 == pytest.approx(0.25 * 6.0, abs=1e-4)


def test_interpolate_wrap_takes_short_path_across_boundary():
    """atom0 0.95→0.05 应走 +0.1 短路径(midpoint 1.0≡0.0,cart 6.0),而非穿胞到 0.5(cart 3.0)。"""
    ini = _poscar(0.95, 0.50)
    fin = _poscar(0.05, 0.50)
    img = nb.interpolate_images(ini, fin, 1)[0]
    x0 = parse_positions(img)['coords'][0][0]
    assert x0 == pytest.approx(6.0, abs=1e-3) or x0 == pytest.approx(0.0, abs=1e-3)
    assert abs(x0 - 3.0) > 1.0                      # 断非穿胞的错误中点


def test_interpolate_wrap_negative_direction():
    """反向跨界 0.05→0.95 走 −0.1 短路径,midpoint 0.0≡1.0。"""
    ini = _poscar(0.05, 0.50)
    fin = _poscar(0.95, 0.50)
    img = nb.interpolate_images(ini, fin, 1)[0]
    x0 = parse_positions(img)['coords'][0][0]
    assert x0 == pytest.approx(0.0, abs=1e-3) or x0 == pytest.approx(6.0, abs=1e-3)


def test_interpolate_rejects_overlap():
    """末态两原子过近 → 靠近末态的中间 image 原子重叠 → ValueError 点名。"""
    ini = _poscar(0.30, 0.70)
    fin = _poscar(0.50, 0.52)                       # 末态 0.12 Å 间距(端点不校验)
    with pytest.raises(ValueError, match='原子重叠'):
        nb.interpolate_images(ini, fin, 5)


def test_interpolate_rejects_species_mismatch():
    with pytest.raises(ValueError, match='不一致'):
        nb.interpolate_images(_INI, _poscar(0.30, 0.70, elem='He'), 3)


def test_interpolate_rejects_count_mismatch():
    fin = 'NEB\n1.0\n6 0 0\n0 6 0\n0 0 6\nH\n3\nDirect\n0.3 0.5 0.5\n0.7 0.5 0.5\n0.5 0.5 0.5\n'
    with pytest.raises(ValueError, match='不一致'):
        nb.interpolate_images(_INI, fin, 3)


def test_interpolate_rejects_bad_n_images():
    with pytest.raises(ValueError, match='正整数'):
        nb.interpolate_images(_INI, _FIN, 0)


def test_interpolate_preserves_selective_dynamics():
    """端点带 Selective dynamics → 每个 image 逐原子透传冻结标志(slab NEB 冻相同底层)。"""
    ini = ('NEB\n1.0\n6 0 0\n0 6 0\n0 0 6\nH\n2\nSelective dynamics\nDirect\n'
           '0.2 0.5 0.5 T T T\n0.8 0.5 0.5 F F F\n')
    fin = ('NEB\n1.0\n6 0 0\n0 6 0\n0 0 6\nH\n2\nSelective dynamics\nDirect\n'
           '0.3 0.5 0.5 T T T\n0.7 0.5 0.5 F F F\n')
    img = nb.interpolate_images(ini, fin, 2)[0]
    assert 'Selective dynamics' in img
    lines = [ln for ln in img.splitlines() if 'T T T' in ln or 'F F F' in ln]
    assert 'T T T' in lines[0] and 'F F F' in lines[1]


# ── build_neb_dir:布局 / INCAR / manifest / KPOINTS / POTCAR ──
def _build(tmp_path, **kw):
    jd = os.path.join(str(tmp_path), 'neb')
    kw.setdefault('n_images', 3)
    kw.setdefault('kpoints', [3, 3, 1])
    kw.setdefault('potcar_fn', _fake_potcar)
    res = nb.build_neb_dir(jd, _INI, _FIN, _INCAR, **kw)
    return jd, res


def test_build_neb_dir_layout(tmp_path):
    jd, res = _build(tmp_path)
    entries = sorted(os.listdir(jd))
    # 5 帧(00..04)+ 根共享 4 文件 + README + job.yaml
    for fr in ('00', '01', '02', '03', '04'):
        assert fr in entries
        assert os.path.isfile(os.path.join(jd, fr, 'POSCAR'))
    for f in ('INCAR', 'POTCAR', 'KPOINTS', 'README.txt', 'job.yaml'):
        assert f in entries
    assert res['n_images'] == 3


def test_build_neb_dir_incar_only_adds(tmp_path):
    jd, _ = _build(tmp_path)
    incar = open(os.path.join(jd, 'INCAR'), encoding='utf-8').read()
    assert incar.startswith(_INCAR)                # 用户原文一字不改置首
    assert 'IMAGES = 3' in incar
    assert 'SPRING = -5' in incar
    assert 'IBRION = 1' in incar
    assert 'LCLIMB = .TRUE.' in incar
    assert 'VTST' in incar                         # LCLIMB 依赖注释


def test_build_neb_dir_respects_existing_ibrion(tmp_path):
    jd = os.path.join(str(tmp_path), 'neb')
    res = nb.build_neb_dir(jd, _INI, _FIN, 'ENCUT = 400\nIBRION = 2\n',
                           n_images=3, kpoints=[3, 3, 1], potcar_fn=_fake_potcar)
    incar = open(os.path.join(jd, 'INCAR'), encoding='utf-8').read()
    assert 'IBRION = 2' in incar and 'IBRION = 1' not in incar   # 保留用户值
    assert any('IBRION' in w for w in res['warnings'])


def test_build_neb_dir_images_conflict_raises(tmp_path):
    jd = os.path.join(str(tmp_path), 'neb')
    with pytest.raises(ValueError, match='冲突'):
        nb.build_neb_dir(jd, _INI, _FIN, 'ENCUT = 400\nIMAGES = 7\n',
                         n_images=3, kpoints=[3, 3, 1], potcar_fn=_fake_potcar)


def test_build_neb_dir_climbing_false_writes_lclimb_false(tmp_path):
    jd, _ = _build(tmp_path, climbing=False)
    incar = open(os.path.join(jd, 'INCAR'), encoding='utf-8').read()
    assert 'LCLIMB = .FALSE.' in incar


def test_build_neb_dir_manifest(tmp_path):
    jd, _ = _build(tmp_path)
    m = mf.load_manifest(jd)
    assert m['task_type'] == 'neb'
    assert m['inputs']['n_images'] == 3
    assert m['inputs']['climbing'] is True
    assert m['inputs']['elements'] == ['H']
    assert m['inputs']['poscar_ini_sha256'] and m['inputs']['poscar_fin_sha256']
    assert m['inputs']['poscar_ini_sha256'] != m['inputs']['poscar_fin_sha256']


def test_build_neb_dir_kpoints_explicit(tmp_path):
    jd, _ = _build(tmp_path, kpoints=[5, 5, 1])
    kpts = open(os.path.join(jd, 'KPOINTS'), encoding='utf-8').read()
    assert '5 5 1' in kpts


def test_build_neb_dir_potcar_none_warns(tmp_path):
    jd = os.path.join(str(tmp_path), 'neb')
    res = nb.build_neb_dir(jd, _INI, _FIN, _INCAR, n_images=3,
                           kpoints=[3, 3, 1], potcar_fn=None)
    assert not os.path.isfile(os.path.join(jd, 'POTCAR'))
    assert any('POTCAR' in w for w in res['warnings'])


def test_build_neb_dir_endpoints_verbatim(tmp_path):
    jd, _ = _build(tmp_path)
    assert open(os.path.join(jd, '00', 'POSCAR'), encoding='utf-8').read() == _INI
    assert open(os.path.join(jd, '04', 'POSCAR'), encoding='utf-8').read() == _FIN


# ── neb_preflight 告警 ──
def test_neb_preflight_core_warnings():
    warns = nb.neb_preflight(_INI, _FIN, 'ENCUT = 400\nISYM = 2\nNSW = 50\n')
    joined = ' '.join(warns)
    assert '已弛豫' in joined                       # 端点弛豫提醒
    assert '整除' in joined                         # 并行 IMAGES 整除
    assert 'ISYM=0' in joined                       # ISYM 建议
    assert 'NSW' in joined                          # NSW 建议


def test_neb_preflight_isym_zero_no_warning():
    warns = nb.neb_preflight(_INI, _FIN, 'ENCUT = 400\nISYM = 0\nNSW = 300\n')
    assert not any('ISYM=0' in w for w in warns)
    assert not any(w.startswith('建议 NSW') for w in warns)


def test_neb_preflight_cell_mismatch_warns():
    fin_big = _poscar(0.30, 0.70, cell=6.5)         # 末态胞不同
    warns = nb.neb_preflight(_INI, fin_big, _INCAR)
    assert any('晶格' in w for w in warns)
