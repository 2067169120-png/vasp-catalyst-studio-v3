"""POV-Ray 适配器测试:解析/成键/场景文本离线测;真机冒烟(本机有 POV-Ray 时)。"""
import os

import pytest

from vcstudio.external import povray_render as pr

_POSCAR_D = """Li2S molecule
1.0
10.0 0.0 0.0
0.0 10.0 0.0
0.0 0.0 10.0
Li S
2 1
Direct
0.40 0.50 0.50
0.60 0.50 0.50
0.50 0.50 0.62
"""

_POSCAR_CART_SD = """slab
2.0
5.0 0.0 0.0
0.0 5.0 0.0
0.0 0.0 6.0
C O
1 1
Selective dynamics
Cartesian
0.0 0.0 0.0 T T T
0.6 0.0 0.0 F F F
"""


def test_parse_direct_to_cartesian():
    syms, coords = pr.parse_poscar_atoms(_POSCAR_D)
    assert syms == ['Li', 'Li', 'S']
    assert coords[0] == pytest.approx([4.0, 5.0, 5.0])
    assert coords[2] == pytest.approx([5.0, 5.0, 6.2])


def test_parse_cartesian_with_scale_and_seldyn():
    syms, coords = pr.parse_poscar_atoms(_POSCAR_CART_SD)
    assert syms == ['C', 'O']
    assert coords[1] == pytest.approx([1.2, 0.0, 0.0])   # scale=2 放大


def test_parse_rejects_vasp4_and_truncated():
    with pytest.raises(ValueError, match='VASP4'):
        pr.parse_poscar_atoms(_POSCAR_D.replace('Li S\n', '9 9\n', 1).replace('2 1\n', 'Direct\n', 1))
    with pytest.raises(ValueError, match='截断'):
        pr.parse_poscar_atoms('\n'.join(_POSCAR_D.splitlines()[:-1]))
    with pytest.raises(ValueError, match='scale|缩放'):
        pr.parse_poscar_atoms(_POSCAR_D.replace('1.0\n', '-1.0\n', 1))


def test_build_bonds_heuristic():
    syms, coords = pr.parse_poscar_atoms(_POSCAR_D)
    bonds = pr.build_bonds(syms, coords)
    # Li-S 距离 ~1.56Å < 1.1×(1.28+1.05):成键;Li-Li 2.0Å < 1.1×2.56 也成键
    assert (0, 2) in bonds and (1, 2) in bonds


def test_pov_scene_contains_geometry():
    syms, coords = pr.parse_poscar_atoms(_POSCAR_D)
    scene = pr.pov_scene(syms, coords, pr.build_bonds(syms, coords), 'top')
    assert scene.count('sphere {') == 3
    assert 'cylinder {' in scene and 'orthographic' in scene
    assert 'background { color rgb <1,1,1> }' in scene
    with pytest.raises(ValueError):
        pr.pov_scene(syms, coords, [], 'oblique')


def test_camera_aspect_matches_png_aspect():
    """相机 up/right 比必须等于 PNG 高宽比,否则正交投影把球压成椭圆(真机踩过)。"""
    import re
    syms, coords = pr.parse_poscar_atoms(_POSCAR_D)
    for view in ('top', 'side'):
        scene = pr.pov_scene(syms, coords, [], view)
        m = re.search(r"right -x\*([\d.]+) up [yz]\*([\d.]+)", scene)
        assert m, scene
        cam_aspect = float(m.group(2)) / float(m.group(1))
        assert cam_aspect == pytest.approx(pr._aspect(syms, coords, view), abs=1e-6)


def test_pov_ini_fields():
    ini = pr.pov_ini('a.pov', 'a.png', 800, 600)
    assert 'Input_File_Name=a.pov' in ini and 'Width=800' in ini and 'Display=Off' in ini


def test_render_with_fake_runner(tmp_path):
    """注入假 run:验证 ini/pov 落盘、命令形状、缓存命中。"""
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_POSCAR_D, encoding='utf-8')
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        png = os.path.join(str(kw['cwd']), cmd[2].replace('.ini', '.png'))
        with open(png, 'wb') as f:
            f.write(b'PNG')

        class P:
            returncode = 0
        return P()

    out = pr.render_poscar_views(str(poscar), str(tmp_path / 'figs'),
                                 exe='fake.exe', run=fake_run, views=('top',))
    assert out['ok'] and 'top' in out['images']
    assert calls[0][0] == 'fake.exe' and calls[0][1] == '/RENDER'
    assert os.path.isfile(tmp_path / 'figs' / 'POSCAR_top.pov')
    # 第二次:缓存命中,不再调 run
    out2 = pr.render_poscar_views(str(poscar), str(tmp_path / 'figs'),
                                  exe='fake.exe', run=fake_run, views=('top',))
    assert out2['ok'] and len(calls) == 1


def test_render_degrades_without_povray(tmp_path, monkeypatch):
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_POSCAR_D, encoding='utf-8')
    monkeypatch.setattr(pr, 'find_povray', lambda configured='': None)
    out = pr.render_poscar_views(str(poscar), str(tmp_path))
    assert not out['ok'] and 'POV-Ray 未找到' in out['error']


@pytest.mark.skipif(pr.find_povray() is None, reason='本机无 POV-Ray')
def test_real_povray_smoke(tmp_path):
    """真机冒烟:真调 pvengine64 渲一张小图,验证产出非空 PNG。"""
    poscar = tmp_path / 'POSCAR'
    poscar.write_text(_POSCAR_D, encoding='utf-8')
    out = pr.render_poscar_views(str(poscar), str(tmp_path / 'figs'),
                                 views=('top',), width=240, timeout=120)
    assert out['ok'], out['error']
    png = out['images']['top']
    assert os.path.getsize(png) > 1000                  # 真渲染的 PNG 至少 KB 级
