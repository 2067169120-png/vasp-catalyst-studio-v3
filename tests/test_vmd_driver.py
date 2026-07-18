"""VMD 驱动测试:探测、每个 Tcl 场景生成(断言关键指令+参数注入)、render 编排(假
subprocess)、缺 VMD 降级回传 tcl、未知场景/缺文件角色报错。全程 mock subprocess。"""
import os

from vcstudio.external import vmd_driver as vd


# ── 探测 ─────────────────────────────────────────────────────────────────────
def test_probe_explicit_path(tmp_path):
    exe = tmp_path / 'vmd'
    exe.write_text('#!/bin/sh\n', encoding='utf-8')
    assert vd.probe(str(exe))['available']


def test_probe_missing(monkeypatch):
    monkeypatch.setattr(vd.shutil, 'which', lambda name: None)
    info = vd.probe()
    assert not info['available'] and info['path'] is None and 'VMD' in info['detail']


def test_probe_found_on_path(monkeypatch):
    monkeypatch.setattr(vd.shutil, 'which',
                        lambda name: '/opt/vmd/vmd' if name == 'vmd' else None)
    info = vd.probe()
    assert info['available'] and info['path'] == '/opt/vmd/vmd'


# ── 场景注册表 ───────────────────────────────────────────────────────────────
def test_scenes_registry_shape():
    assert set(vd.SCENES) == {'esp_surface', 'orbital', 'nci', 'structure',
                              'alie_surface', 'iri'}
    for scene in vd.SCENES.values():
        assert callable(scene['build']) and scene['name']
        assert isinstance(scene['files'], tuple) and scene['files']


# ── Tcl 场景生成(纯函数,关键指令断言) ──────────────────────────────────────────
def test_tcl_esp_surface():
    tcl = vd.esp_surface('dens.cub', 'esp.cub', 'out.png', {'iso': 0.002})
    assert 'mol new {dens.cub} type cube' in tcl        # 密度定几何
    assert 'mol addfile {esp.cub} type cube' in tcl     # ESP 定颜色
    assert 'Isosurface 0.002' in tcl                    # 参数注入
    assert 'mol color Volume 1' in tcl
    assert 'color scale method BWR' in tcl              # 蓝白红着色
    assert 'render TachyonInternal {out.png}' in tcl
    assert 'display projection Orthographic' in tcl and 'Background white' in tcl


def test_tcl_esp_surface_with_extrema_spheres():
    extrema = {'minima': [{'value_kcal': -1, 'xyz': [0.0, 0.0, 0.0]}],
               'maxima': [{'value_kcal': 1, 'xyz': [1.0, 2.0, 3.0]}]}
    tcl = vd.esp_surface('d.cub', 'e.cub', 'o.png',
                         {'extrema': extrema, 'extrema_size': 0.1})
    assert 'graphics top color blue' in tcl and 'graphics top color red' in tcl
    assert 'graphics top sphere {0.0 0.0 0.0} radius 0.1' in tcl
    assert 'graphics top sphere {1.0 2.0 3.0} radius 0.1' in tcl


def test_tcl_esp_surface_color_range():
    tcl = vd.esp_surface('d.cub', 'e.cub', 'o.png', {'color_range': (-0.05, 0.05)})
    assert 'mol scaleminmax top 0 -0.05 0.05' in tcl


def test_tcl_orbital_dual_isosurface():
    tcl = vd.orbital('orb.cub', 'orb.png', {'iso': 0.05})
    assert 'mol new {orb.cub} type cube' in tcl
    assert 'Isosurface 0.05' in tcl and 'Isosurface -0.05' in tcl   # ±双色
    assert 'mol color ColorID 0' in tcl and 'mol color ColorID 1' in tcl
    assert 'render TachyonInternal {orb.png}' in tcl


def test_tcl_nci():
    tcl = vd.nci('func2.cub', 'func1.cub', 'nci.png')
    assert 'mol new {func2.cub} type cube' in tcl          # RDG 定几何
    assert 'mol addfile {func1.cub} type cube' in tcl      # sign(λ2)ρ 定色
    assert 'Isosurface 0.5' in tcl
    assert 'mol scaleminmax top 0 -0.035 0.02' in tcl      # 着色 -0.035~0.02
    assert 'mol color Volume 1' in tcl


def test_tcl_alie_surface():
    tcl = vd.alie_surface('dens.cub', 'alie.cub', 'out.png', {'iso': 0.002})
    assert 'mol new {dens.cub} type cube' in tcl        # 密度定几何
    assert 'mol addfile {alie.cub} type cube' in tcl    # ALIE 定颜色
    assert 'Isosurface 0.002' in tcl                    # 参数注入
    assert 'mol color Volume 1' in tcl
    assert 'ALIE' in tcl                                # 色标说明换成 ALIE
    assert 'render TachyonInternal {out.png}' in tcl


def test_tcl_alie_surface_extrema_and_range():
    extrema = {'minima': [{'value_kcal': -1, 'xyz': [0.0, 0.0, 0.0]}], 'maxima': []}
    tcl = vd.alie_surface('d.cub', 'a.cub', 'o.png',
                          {'extrema': extrema, 'color_range': (10.0, 20.0)})
    assert 'graphics top sphere {0.0 0.0 0.0} radius 0.1' in tcl
    assert 'mol scaleminmax top 0 10.0 20.0' in tcl


def test_tcl_iri_default_iso_one():
    tcl = vd.iri('func1.cub', 'func2.cub', 'iri.png')
    assert 'mol new {func1.cub} type cube' in tcl          # IRI 定几何(func1)
    assert 'mol addfile {func2.cub} type cube' in tcl      # sign(λ2)ρ 定色(func2)
    assert 'Isosurface 1.0' in tcl                         # IRI 默认 iso=1.0
    assert 'mol scaleminmax top 0 -0.035 0.02' in tcl      # 与 NCI 同族着色范围
    assert 'color scale method BGR' in tcl


def test_tcl_iri_iso_override():
    assert 'Isosurface 2.0' in vd.iri('a.cub', 'b.cub', 'o.png', {'iso': 2.0})


def test_tcl_structure_xyz_and_pdb():
    tcl = vd.structure('mol.xyz', 'struct.png')
    assert 'mol new {mol.xyz} type xyz' in tcl
    assert 'mol representation CPK' in tcl and 'mol color Name' in tcl
    assert 'mol new {p.pdb} type pdb' in vd.structure('p.pdb', 's.png')


# ── render 编排(假 subprocess) ───────────────────────────────────────────────
def test_render_with_fake_vmd(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'probe',
                        lambda exe=None: {'available': True, 'path': 'vmd', 'detail': ''})
    out_png = str(tmp_path / 'fig.png')
    calls = {}

    def fake_run(cmd, **kw):
        calls['cmd'] = cmd
        with open(out_png, 'wb') as f:
            f.write(b'PNGDATA')

        class P:
            returncode = 0
            stdout = b'rendered\n'
        return P()

    monkeypatch.setattr(vd.subprocess, 'run', fake_run)
    res = vd.render('orbital', {'cube': str(tmp_path / 'orb.cub')}, out_png)
    assert res['ok'] and res['png'] == os.path.abspath(out_png)
    assert calls['cmd'][0] == 'vmd' and calls['cmd'][1:4] == ['-dispdev', 'text', '-e']
    assert os.path.isfile(calls['cmd'][4])                # tcl 脚本落盘
    assert 'Isosurface' in res['tcl']


def test_render_iri_scene_wires_file_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'probe',
                        lambda exe=None: {'available': True, 'path': 'vmd', 'detail': ''})
    out_png = str(tmp_path / 'iri.png')

    def fake_run(cmd, **kw):
        with open(out_png, 'wb') as f:
            f.write(b'PNGDATA')

        class P:
            returncode = 0
            stdout = b'rendered\n'
        return P()

    monkeypatch.setattr(vd.subprocess, 'run', fake_run)
    res = vd.render('iri', {'iri': 'f1.cub', 'sign_lambda2': 'f2.cub'}, out_png)
    assert res['ok']
    assert 'mol new {f1.cub}' in res['tcl'] and 'mol addfile {f2.cub}' in res['tcl']


def test_render_degrades_without_vmd(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'probe',
                        lambda exe=None: {'available': False, 'path': None, 'detail': '未找到 VMD'})
    res = vd.render('structure', {'structure': str(tmp_path / 'm.xyz')}, str(tmp_path / 's.png'))
    assert not res['ok'] and res['png'] is None
    assert 'render TachyonInternal' in res['tcl']         # 回传可手动运行的 tcl
    assert 'vmd -dispdev text -e' in res['error']


def test_render_unknown_scene(tmp_path):
    res = vd.render('no_such_scene', {}, str(tmp_path / 'x.png'))
    assert not res['ok'] and 'no_such_scene' in res['error']


def test_render_missing_file_role(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'probe',
                        lambda exe=None: {'available': True, 'path': 'vmd', 'detail': ''})
    res = vd.render('nci', {'rdg': 'r.cub'}, str(tmp_path / 'n.png'))   # 缺 sign_lambda2
    assert not res['ok'] and 'sign_lambda2' in res['error']


def test_render_no_png_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, 'probe',
                        lambda exe=None: {'available': True, 'path': 'vmd', 'detail': ''})

    def fake_run(cmd, **kw):
        class P:                                          # 退出 0 但没产出 PNG
            returncode = 0
            stdout = b'no image written\n'
        return P()

    monkeypatch.setattr(vd.subprocess, 'run', fake_run)
    res = vd.render('structure', {'structure': str(tmp_path / 'm.xyz')}, str(tmp_path / 's.png'))
    assert not res['ok'] and '未产出 PNG' in res['error']
