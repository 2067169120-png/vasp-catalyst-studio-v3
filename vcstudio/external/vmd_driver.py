"""VMD 批渲染驱动:探测 → 生成 Tcl 场景脚本 → 调用 → 降级。

定位(对标 starpivot-DFT ⑥可视化):VMD(Visual Molecular Dynamics)是把 Multiwfn 产出的
cube 体数据渲成出版级图的事实标准——ESP 着色分子表面、前线轨道等值面、NCI 弱相互作用
等值面、分子结构 CPK。本模块把每类图写成纯函数 Tcl 生成器,经 `vmd -dispdev text -e
script.tcl` 无头批渲染,内置 Tachyon 光线追踪出 PNG。

对齐 povray_render / multiwfn_driver 适配器口径:
- Tcl 生成器是纯函数,离线可单测(断言 mol new/Isosurface/render 等关键指令存在);
- subprocess 超时保护,测试经 monkeypatch 替身(不真跑 VMD);
- 找不到 VMD → 结构化 error + 回传 Tcl 文本(用户可自行 `vmd -dispdev text -e` 跑),绝不崩。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import shutil
import subprocess

_EXE_CANDIDATES = ('vmd', 'vmd.exe', 'VMD', 'VMD.exe')


# ── 探测 ─────────────────────────────────────────────────────────────────────
def probe(exe: str | None = None) -> dict:
    """探测 VMD 可执行文件。返回 {'available','path','detail'}(口径同 multiwfn_driver.probe)。"""
    if exe:
        if os.path.isfile(exe):
            return {'available': True, 'path': exe, 'detail': f'使用指定的 VMD:{exe}'}
        found = shutil.which(exe)
        if found:
            return {'available': True, 'path': found, 'detail': f'在 PATH 找到:{found}'}
        return {'available': False, 'path': None, 'detail': f'指定的 VMD 不存在:{exe}'}
    for cand in _EXE_CANDIDATES:
        found = shutil.which(cand)
        if found:
            return {'available': True, 'path': found, 'detail': f'在 PATH 找到 VMD:{found}'}
    return {'available': False, 'path': None,
            'detail': ('未找到 VMD:请到 https://www.ks.uiuc.edu/Research/vmd 下载安装,'
                       '把 vmd 加入 PATH,或调用时用 exe= 指定可执行文件路径。')}


# ── Tcl 场景生成(纯函数) ────────────────────────────────────────────────────
def _tcl_footer(out_png: str) -> list:
    """所有场景共用的收尾:白底 + 正交投影 + Tachyon 内置渲染 PNG + 退出。"""
    return [
        'color Display Background white',        # 白底(出版惯例)
        'display projection Orthographic',       # 正交投影(工程制图感,无透视变形)
        'display depthcue off',                  # 关景深雾化,保色准
        'axes location off',                     # 去坐标轴标
        f'render TachyonInternal {{{out_png}}}',  # Tachyon 内置光追渲染出 PNG(花括号防路径空格)
        'exit',                                  # 文本模式跑完退出,subprocess 才返回
    ]


def esp_surface(density_cube: str, esp_cube: str, out_png: str,
                params: dict | None = None) -> str:
    """ESP 着色分子表面:电子密度等值面(iso,默认 0.001)按静电势(BWR 蓝白红)着色。

    params:iso(密度等值面值)、color_range=(min,max)(ESP 着色范围,可选)、
    extrema(可选,extrema_parse 输出 {'minima':[{'xyz'..}],'maxima':[..]},标注极小蓝/极大红球)、
    extrema_size(标注球半径,默认 0.1)。
    """
    p = params or {}
    iso = float(p.get('iso', 0.001))
    color_range = p.get('color_range')
    size = float(p.get('extrema_size', 0.1))
    extrema = p.get('extrema')
    lines = [
        f'# ESP 着色分子表面:密度 {iso} 等值面按静电势(BWR)着色',
        f'mol new {{{density_cube}}} type cube waitfor all',      # vol 0:电子密度(定几何)
        f'mol addfile {{{esp_cube}}} type cube waitfor all',      # vol 1:ESP(定颜色)
        'mol delrep 0 top',
        f'mol representation Isosurface {iso} 0 0 0 1 1',         # 密度等值面(实体面)
        'mol color Volume 1',                                     # 按 vol 1(ESP)着色
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
        'color scale method BWR',                                 # 蓝白红:负→正 ESP
    ]
    if color_range:
        lines.append(f'mol scaleminmax top 0 {float(color_range[0])} {float(color_range[1])}')
    if extrema:                                                   # 极值标注球:极小蓝、极大红
        for pt in extrema.get('minima', []):
            x, y, z = pt['xyz']
            lines.append('graphics top color blue')
            lines.append(f'graphics top sphere {{{x} {y} {z}}} radius {size} resolution 20')
        for pt in extrema.get('maxima', []):
            x, y, z = pt['xyz']
            lines.append('graphics top color red')
            lines.append(f'graphics top sphere {{{x} {y} {z}}} radius {size} resolution 20')
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


def orbital(cube: str, out_png: str, params: dict | None = None) -> str:
    """分子轨道:±iso(默认 0.05)双色等值面(正相位蓝、负相位红)。"""
    p = params or {}
    iso = float(p.get('iso', 0.05))
    lines = [
        f'# 分子轨道:±{iso} 双色等值面(正相位蓝、负相位红)',
        f'mol new {{{cube}}} type cube waitfor all',
        'mol delrep 0 top',
        f'mol representation Isosurface {iso} 0 0 0 1 1',    # 正相位等值面
        'mol color ColorID 0',                               # ColorID 0 = blue
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
        f'mol representation Isosurface {-iso} 0 0 0 1 1',   # 负相位等值面
        'mol color ColorID 1',                               # ColorID 1 = red
        'mol addrep top',
    ]
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


def nci(rdg_cube: str, sign_lambda2_cube: str, out_png: str,
        params: dict | None = None) -> str:
    """NCI 弱相互作用:RDG 等值面(iso,默认 0.5)按 sign(λ2)ρ 着色(默认范围 -0.035~0.02 a.u.)。

    对应 Multiwfn nci_rdg 产物:rdg_cube=func2.cub(RDG),sign_lambda2_cube=func1.cub(sign(λ2)ρ)。
    着色蓝=吸引(氢键)、绿=vdW、红=位阻排斥。
    """
    p = params or {}
    iso = float(p.get('iso', 0.5))
    lo, hi = p.get('color_range', (-0.035, 0.02))
    lines = [
        f'# NCI:RDG={iso} 等值面按 sign(λ2)ρ 着色(蓝=吸引/氢键,绿=vdW,红=位阻)',
        f'mol new {{{rdg_cube}}} type cube waitfor all',               # vol 0:RDG(定几何)
        f'mol addfile {{{sign_lambda2_cube}}} type cube waitfor all',  # vol 1:sign(λ2)ρ(定色)
        'mol delrep 0 top',
        f'mol representation Isosurface {iso} 0 0 0 1 1',
        'mol color Volume 1',                                          # 按 vol 1 着色
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
        f'mol scaleminmax top 0 {float(lo)} {float(hi)}',              # 着色范围 -0.035~0.02 a.u.
        'color scale method BGR',                                      # 蓝绿红(NCI 通行色标)
    ]
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


def alie_surface(density_cube: str, alie_cube: str, out_png: str,
                 params: dict | None = None) -> str:
    """ALIE 着色分子表面:电子密度等值面(iso,默认 0.001)按 ALIE(平均局域离子化能)着色。

    与 esp_surface 同构,只把着色 cube 换成 ALIE:色标低值(蓝)=电子束缚弱=亲电敏感位点,
    高值(红)=电子束缚强。params:iso(密度等值面值)、color_range=(min,max)(ALIE 着色范围,
    单位 eV,可选)、extrema(可选,extrema_parse 输出,标注极小蓝/极大红球)、extrema_size。
    """
    p = params or {}
    iso = float(p.get('iso', 0.001))
    color_range = p.get('color_range')
    size = float(p.get('extrema_size', 0.1))
    extrema = p.get('extrema')
    lines = [
        f'# ALIE 着色分子表面:密度 {iso} 等值面按 ALIE(平均局域离子化能)着色',
        f'mol new {{{density_cube}}} type cube waitfor all',      # vol 0:电子密度(定几何)
        f'mol addfile {{{alie_cube}}} type cube waitfor all',     # vol 1:ALIE(定颜色)
        'mol delrep 0 top',
        f'mol representation Isosurface {iso} 0 0 0 1 1',         # 密度等值面(实体面)
        'mol color Volume 1',                                     # 按 vol 1(ALIE)着色
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
        'color scale method BWR',                                 # 蓝白红:低 ALIE(亲电敏感)→高 ALIE
    ]
    if color_range:
        lines.append(f'mol scaleminmax top 0 {float(color_range[0])} {float(color_range[1])}')
    if extrema:                                                   # 极值标注球:极小蓝、极大红
        for pt in extrema.get('minima', []):
            x, y, z = pt['xyz']
            lines.append('graphics top color blue')
            lines.append(f'graphics top sphere {{{x} {y} {z}}} radius {size} resolution 20')
        for pt in extrema.get('maxima', []):
            x, y, z = pt['xyz']
            lines.append('graphics top color red')
            lines.append(f'graphics top sphere {{{x} {y} {z}}} radius {size} resolution 20')
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


def iri(iri_cube: str, sign_lambda2_cube: str, out_png: str,
        params: dict | None = None) -> str:
    """IRI 相互作用区域:IRI 等值面(iso,默认 1.0)按 sign(λ2)ρ 着色(默认范围 -0.035~0.02 a.u.)。

    对应 Multiwfn iri 产物:iri_cube=func1.cub(IRI),sign_lambda2_cube=func2.cub(sign(λ2)ρ)。
    着色与 NCI 同族:蓝=吸引(氢键/成键)、绿=vdW、红=位阻排斥;IRI 比 NCI 多显化学键区。
    """
    p = params or {}
    iso = float(p.get('iso', 1.0))
    lo, hi = p.get('color_range', (-0.035, 0.02))
    lines = [
        f'# IRI:IRI={iso} 等值面按 sign(λ2)ρ 着色(蓝=吸引/成键,绿=vdW,红=位阻)',
        f'mol new {{{iri_cube}}} type cube waitfor all',               # vol 0:IRI(定几何)
        f'mol addfile {{{sign_lambda2_cube}}} type cube waitfor all',  # vol 1:sign(λ2)ρ(定色)
        'mol delrep 0 top',
        f'mol representation Isosurface {iso} 0 0 0 1 1',
        'mol color Volume 1',                                          # 按 vol 1 着色
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
        f'mol scaleminmax top 0 {float(lo)} {float(hi)}',              # 着色范围 -0.035~0.02 a.u.
        'color scale method BGR',                                      # 蓝绿红(NCI/IRI 通行色标)
    ]
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


def structure(struct_file: str, out_png: str, params: dict | None = None) -> str:
    """分子结构:CPK 球棍模型;按扩展名判 xyz/pdb。"""
    ext = os.path.splitext(str(struct_file))[1].lower()
    ftype = 'pdb' if ext == '.pdb' else 'xyz'
    lines = [
        f'# 分子结构:CPK 球棍模型(读取 {ftype})',
        f'mol new {{{struct_file}}} type {ftype} waitfor all',
        'mol delrep 0 top',
        'mol representation CPK 1.0 0.3 12 12',   # 球半径/键半径/球细分/键细分
        'mol color Name',                         # 按元素着色
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
    ]
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


def igmh(dg_cube: str, sign_lambda2_cube: str, out_png: str,
         params: dict | None = None) -> str:
    """IGMH 片段相互作用:δg 等值面(iso,默认 0.01)按 sign(λ2)ρ 着色(默认 ±0.05 a.u.)。

    对应 Multiwfn igmh 产物:dg_cube=func1.cub(δg),sign_lambda2_cube=func2.cub
    (sign(λ2)ρ)。着色蓝=吸引、绿=vdW、红=排斥;「片段间/片段内」由所选 δg cube 决定
    (Multiwfn 侧导出哪个就渲染哪个,本场景不替用户猜)。
    """
    p = params or {}
    iso = float(p.get('iso', 0.01))
    lo, hi = p.get('color_range', (-0.05, 0.05))
    lines = [
        f'# IGMH:δg={iso} 等值面按 sign(λ2)ρ 着色(蓝=吸引,绿=vdW,红=排斥)',
        f'mol new {{{dg_cube}}} type cube waitfor all',                # vol 0:δg(定几何)
        f'mol addfile {{{sign_lambda2_cube}}} type cube waitfor all',  # vol 1:sign(λ2)ρ(定色)
        'mol delrep 0 top',
        f'mol representation Isosurface {iso} 0 0 0 1 1',
        'mol color Volume 1',                                          # 按 vol 1 着色
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
        f'mol scaleminmax top 0 {float(lo)} {float(hi)}',              # IGMH 惯用 ±0.05 a.u.
        'color scale method BGR',                                      # 蓝绿红(IGM 族通行色标)
    ]
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


# AIM 临界点类型 → 标注色(BCP 键橙 / RCP 环绿 / CCP 笼紫 / NCP 核灰,默认不画 NCP)
_CP_COLORS = {'(3,-1)': 'orange', '(3,+1)': 'green', '(3,+3)': 'purple', '(3,-3)': 'gray'}


def aim(struct_file: str, cpprop_file: str, out_png: str,
        params: dict | None = None) -> str:
    """AIM 临界点标注:结构细棍 CPK + CPprop.txt 各临界点画球并标「编号:ρ」。

    - cpprop_file:Multiwfn 拓扑分析导出(本软件 aim_cp 分析收集为 aim_cp_CPprop.txt);
      经 multiwfn_driver.cpprop_parse 解析,坐标 Bohr→Å 后落在结构坐标系上。
    - params:cp_size(标注球半径,默认 0.12)、labels('both' 编号+ρ / 'index' / 'rho' /
      'none')、show_ncp(默认 False:核临界点与原子重合,不画)。
    - 文件读不到 / 一个 CP 都解析不出 → ValueError 中文报错(render 转结构化 error),
      绝不渲染一张"看着成功"的空标注图。
    """
    from vcstudio.external.multiwfn_driver import cpprop_parse
    p = params or {}
    size = float(p.get('cp_size', 0.12))
    labels = str(p.get('labels', 'both')).lower()
    show_ncp = bool(p.get('show_ncp', False))
    try:
        with open(cpprop_file, 'r', encoding='utf-8', errors='replace') as f:
            cps = cpprop_parse(f.read())
    except OSError as e:
        raise ValueError(f'CPprop 文件无法读取:{e}') from None
    drawn = [c for c in cps if c.get('xyz_angst')
             and (show_ncp or c.get('type') != '(3,-3)')]
    if not drawn:
        raise ValueError('CPprop.txt 未解析出可标注的临界点(文件为空、版本格式差异,'
                         '或仅含核临界点;可在参数开 show_ncp 核对)')
    ext = os.path.splitext(str(struct_file))[1].lower()
    ftype = 'pdb' if ext == '.pdb' else 'xyz'
    lines = [
        f'# AIM 临界点标注:结构 CPK + {len(drawn)} 个 CP(BCP 橙/RCP 绿/CCP 紫)',
        f'mol new {{{struct_file}}} type {ftype} waitfor all',
        'mol delrep 0 top',
        'mol representation CPK 0.6 0.2 12 12',   # 细棍:让位给 CP 标注
        'mol color Name',
        'mol selection {all}',
        'mol material Opaque',
        'mol addrep top',
    ]
    for cp in drawn:
        x, y, z = cp['xyz_angst']
        color = _CP_COLORS.get(cp.get('type'), 'orange')
        lines.append(f'graphics top color {color}')
        lines.append(f'graphics top sphere {{{x} {y} {z}}} radius {size} resolution 20')
        if labels != 'none':
            rho = cp.get('rho')
            tag = {'index': str(cp['index']),
                   'rho': (f'{rho:.3f}' if rho is not None else '?')}.get(
                labels, f'{cp["index"]}:' + (f'{rho:.3f}' if rho is not None else '?'))
            lines.append('graphics top color black')
            lines.append(
                f'graphics top text {{{x} {y + size * 1.6} {z}}} "{tag}" size 0.9 thickness 2')
    lines += _tcl_footer(out_png)
    return '\n'.join(lines) + '\n'


# ── 场景注册表 ───────────────────────────────────────────────────────────────
# key → {name(中文), files(render 需要的文件角色键), build(files,out,params)->tcl, note}
SCENES = {
    'esp_surface': {
        'name': 'ESP 着色分子表面',
        'files': ('density', 'esp'),
        'build': lambda files, out, p: esp_surface(files['density'], files['esp'], out, p),
        'note': '密度等值面按 ESP 着色(BWR);需 density + esp 两个 cube;可选 extrema 标注球。',
    },
    'orbital': {
        'name': '分子轨道等值面',
        'files': ('cube',),
        'build': lambda files, out, p: orbital(files['cube'], out, p),
        'note': '单个轨道 cube 的 ±iso 双色等值面。',
    },
    'nci': {
        'name': 'NCI 弱相互作用',
        'files': ('rdg', 'sign_lambda2'),
        'build': lambda files, out, p: nci(files['rdg'], files['sign_lambda2'], out, p),
        'note': 'RDG 等值面按 sign(λ2)ρ 着色;rdg=func2.cub、sign_lambda2=func1.cub。',
    },
    'alie_surface': {
        'name': 'ALIE 着色分子表面',
        'files': ('density', 'alie'),
        'build': lambda files, out, p: alie_surface(files['density'], files['alie'], out, p),
        'note': '密度等值面按 ALIE 着色(BWR);需 density + alie 两个 cube;低 ALIE=亲电敏感位点。',
    },
    'iri': {
        'name': 'IRI 相互作用区域',
        'files': ('iri', 'sign_lambda2'),
        'build': lambda files, out, p: iri(files['iri'], files['sign_lambda2'], out, p),
        'note': 'IRI 等值面(默认 iso=1.0)按 sign(λ2)ρ 着色;iri=func1.cub、sign_lambda2=func2.cub。',
    },
    'structure': {
        'name': '分子结构 CPK',
        'files': ('structure',),
        'build': lambda files, out, p: structure(files['structure'], out, p),
        'note': 'xyz/pdb 结构的 CPK 球棍渲染。',
    },
    # ── v3.3.0 对齐 starpivot 可视化 tab:IGMH / Fukui / AIM 三场景 ──
    'igmh': {
        'name': 'IGMH 片段相互作用',
        'files': ('dg', 'sign_lambda2'),
        'build': lambda files, out, p: igmh(files['dg'], files['sign_lambda2'], out, p),
        'note': ('IGMH δg 等值面(默认 iso=0.01)按 sign(λ2)ρ 着色(惯用 ±0.05 a.u.);'
                 'dg=func1.cub、sign_lambda2=func2.cub;片段间/片段内取决于所选 δg cube。'),
    },
    'fukui': {
        'name': 'Fukui / CDFT 等值面',
        'files': ('cube',),
        'build': lambda files, out, p: orbital(files['cube'], out, p),
        'note': ('f+ / f− / f0 / 双描述符任一 cube 的 ±iso 双色等值面(默认 0.05);'
                 'cube 来自波函数页 Fukui/CDFT 分析产物(fukui_cdft_*.cub)。'),
    },
    'aim': {
        'name': 'AIM 临界点标注',
        'files': ('structure', 'cpprop'),
        'build': lambda files, out, p: aim(files['structure'], files['cpprop'], out, p),
        'note': ('结构 CPK + CPprop.txt 临界点标注(BCP 橙 / RCP 绿 / CCP 紫,标签=编号:ρ);'
                 'cpprop 用 AIM 分析产物 aim_cp_CPprop.txt,structure 给 xyz/pdb。'),
    },
}


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def _tail(text: str, n: int = 40) -> str:
    return '\n'.join(text.splitlines()[-n:])


def render(scene_key: str, files: dict, out_png: str, *, exe: str | None = None,
           params: dict | None = None, timeout: int = 600) -> dict:
    """渲染一个场景。返回 {'ok','png','tcl','stdout_tail','error'}。

    files:文件角色 → 路径(角色见 SCENES[scene_key]['files'])。
    生成 Tcl → `vmd -dispdev text -e script.tcl`。VMD 缺失 → ok=False + 中文指引,并把
    Tcl 文本放入 'tcl' 字段(用户可存成 script.tcl 自行在 VMD 里跑)。全程超时保护,绝不抛。
    """
    if scene_key not in SCENES:
        return {'ok': False, 'png': None, 'tcl': '', 'stdout_tail': '',
                'error': f'未知场景 {scene_key!r};可选:{", ".join(SCENES)}'}
    scene = SCENES[scene_key]
    out_png = os.path.abspath(str(out_png))
    # 生成 Tcl(缺文件角色 → KeyError → 结构化报错)
    try:
        tcl = scene['build'](files or {}, out_png, params or {})
    except KeyError as e:
        return {'ok': False, 'png': None, 'tcl': '', 'stdout_tail': '',
                'error': f'场景 {scene_key} 缺少输入文件角色 {e};需要 {scene["files"]}'}
    except (ValueError, OSError) as e:                    # 场景构建期输入问题(如 CPprop 解析)
        return {'ok': False, 'png': None, 'tcl': '', 'stdout_tail': '',
                'error': f'场景 {scene_key} 构建失败:{e}'}
    info = probe(exe)
    if not info['available']:
        return {'ok': False, 'png': None, 'tcl': tcl, 'stdout_tail': '',
                'error': (info['detail'] + ' 可把 tcl 字段存成 script.tcl 自行运行:'
                          'vmd -dispdev text -e script.tcl')}
    out_dir = os.path.dirname(out_png) or '.'
    os.makedirs(out_dir, exist_ok=True)
    script_path = os.path.join(out_dir, f'_vmd_{scene_key}.tcl')
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(tcl)
    if os.path.isfile(out_png):
        try:
            os.remove(out_png)               # 清旧图,防读到上一轮产物误判成功
        except OSError:
            pass
    try:
        proc = subprocess.run([info['path'], '-dispdev', 'text', '-e', script_path],
                              cwd=out_dir, timeout=timeout, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        code = getattr(proc, 'returncode', 1)
        raw = getattr(proc, 'stdout', b'') or b''
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'png': None, 'tcl': tcl, 'stdout_tail': '',
                'error': f'VMD 运行失败:{e}'}
    tail = _tail(raw.decode('utf-8', errors='replace'))
    if code == 0 and os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
        return {'ok': True, 'png': out_png, 'tcl': tcl, 'stdout_tail': tail, 'error': ''}
    return {'ok': False, 'png': None, 'tcl': tcl, 'stdout_tail': tail,
            'error': f'VMD 渲染未产出 PNG(退出码 {code});检查 tcl 与 cube 文件'}
