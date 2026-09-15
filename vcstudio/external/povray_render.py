"""POV-Ray 分子结构渲染适配器(零 ASE/numpy:自解析 POSCAR,自产 .pov/.ini 文本)。

对齐原版 povray_render.py/structure_render.py 的口径,但剥离 ASE 依赖:
- 半径 = 共价半径 × 0.40(原版实测:0.5 露键,0.65 重叠)
- 成键 = 距离 < 1.1×(rᵢ+rⱼ)(ASE get_bondpairs 同款启发式);不建 PBC 镜像
  (等效原版 filter_intracell_bonds 滤跨胞断键)
- 视角:top=俯视(x-y 面),side=侧视(x-z 面,原版 '-90x' 语义)
- 调用:pvengine64 /RENDER job.ini /EXIT /NORESTORE(探测:配置→默认安装位→PATH)

纯函数(解析/成键/场景文本)离线可测;subprocess 经 run= 注入可替身。
渲染失败返回 error 不抛(全自动链路里单张失败跳过)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import shutil
import subprocess

from vcstudio.generate.structure_view import parse_positions

# ── 元素表:共价半径 Å(Cordero 2008)+ jmol 配色 ────────────────────────────────
ELEMENTS = {
    'H': (0.31, '#FFFFFF'), 'He': (0.28, '#D9FFFF'),
    'Li': (1.28, '#CC80FF'), 'Be': (0.96, '#C2FF00'), 'B': (0.84, '#FFB5B5'),
    'C': (0.76, '#909090'), 'N': (0.71, '#3050F8'), 'O': (0.66, '#FF0D0D'),
    'F': (0.57, '#90E050'), 'Ne': (0.58, '#B3E3F5'),
    'Na': (1.66, '#AB5CF2'), 'Mg': (1.41, '#8AFF00'), 'Al': (1.21, '#BFA6A6'),
    'Si': (1.11, '#F0C8A0'), 'P': (1.07, '#FF8000'), 'S': (1.05, '#FFFF30'),
    'Cl': (1.02, '#1FF01F'), 'Ar': (1.06, '#80D1E3'),
    'K': (2.03, '#8F40D4'), 'Ca': (1.76, '#3DFF00'), 'Sc': (1.70, '#E6E6E6'),
    'Ti': (1.60, '#BFC2C7'), 'V': (1.53, '#A6A6AB'), 'Cr': (1.39, '#8A99C7'),
    'Mn': (1.39, '#9C7AC7'), 'Fe': (1.32, '#E06633'), 'Co': (1.26, '#F090A0'),
    'Ni': (1.24, '#50D050'), 'Cu': (1.32, '#C88033'), 'Zn': (1.22, '#7D80B0'),
    'Ga': (1.22, '#C28F8F'), 'Ge': (1.20, '#668F8F'), 'As': (1.19, '#BD80E3'),
    'Se': (1.20, '#FFA100'), 'Br': (1.20, '#A62929'), 'Kr': (1.16, '#5CB8D1'),
    'Rb': (2.20, '#702EB0'), 'Sr': (1.95, '#00FF00'), 'Y': (1.90, '#94FFFF'),
    'Zr': (1.75, '#94E0E0'), 'Nb': (1.64, '#73C2C9'), 'Mo': (1.54, '#54B5B5'),
    'Ru': (1.46, '#248F8F'), 'Rh': (1.42, '#0A7D8C'), 'Pd': (1.39, '#006985'),
    'Ag': (1.45, '#C0C0C0'), 'Cd': (1.44, '#FFD98F'), 'In': (1.42, '#A67573'),
    'Sn': (1.39, '#668080'), 'Sb': (1.39, '#9E63B5'), 'Te': (1.38, '#D47A00'),
    'I': (1.39, '#940094'), 'Xe': (1.40, '#429EB0'),
    'Cs': (2.44, '#57178F'), 'Ba': (2.15, '#00C900'), 'La': (2.07, '#70D4FF'),
    'Ce': (2.04, '#FFFFC7'), 'Hf': (1.75, '#4DC2FF'), 'Ta': (1.70, '#4DA6FF'),
    'W': (1.62, '#2194D6'), 'Re': (1.51, '#267DAB'), 'Os': (1.44, '#266696'),
    'Ir': (1.41, '#175487'), 'Pt': (1.36, '#D0D0E0'), 'Au': (1.36, '#FFD123'),
    'Pb': (1.46, '#575961'), 'Bi': (1.48, '#9E4FB5'),
}
_FALLBACK = (1.40, '#FF69B4')      # 未登记元素:粉色醒目提示
RADIUS_SCALE = 0.40                 # 原版 structure_render 实测值
BOND_FACTOR = 1.1                   # ASE get_bondpairs 同款
BOND_RADIUS = 0.11

_DEFAULT_EXES = (
    r'C:\Program Files\POV-Ray\v3.7\bin\pvengine64.exe',
    r'C:\Program Files (x86)\POV-Ray\v3.7\bin\pvengine64.exe',
)


def element_info(sym: str):
    return ELEMENTS.get(sym, _FALLBACK)


# ── POSCAR 解析(零依赖) ────────────────────────────────────────────────────────
def parse_poscar_atoms(text: str):
    """POSCAR/CONTCAR 文本 → (per-atom 元素列表, 笛卡尔坐标 Å 列表)。

    与软件结构预览共用同一解析器，支持 VASP5(元素行)、Selective dynamics、
    Direct/Cartesian、负值目标体积与三个分量缩放因子。畸形/VASP4 →
    ValueError(显式,不猜元素)。
    """
    parsed = parse_positions(text)
    return parsed['elements'], parsed['coords']


def build_bonds(symbols: list, coords: list) -> list:
    """距离 < 1.1×(rᵢ+rⱼ) 成键 → [(i,j)]。O(N²),数百原子毫秒级。"""
    bonds = []
    for i in range(len(coords)):
        ri = element_info(symbols[i])[0]
        for j in range(i + 1, len(coords)):
            cut = BOND_FACTOR * (ri + element_info(symbols[j])[0])
            d2 = sum((coords[i][k] - coords[j][k]) ** 2 for k in range(3))
            if 0.01 < d2 < cut * cut:
                bonds.append((i, j))
    return bonds


# ── POV 场景文本(纯函数) ──────────────────────────────────────────────────────
def _rgb(hexcolor: str) -> str:
    h = hexcolor.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return f'rgb <{r:.3f},{g:.3f},{b:.3f}>'


def _fin() -> str:
    return 'finish { ambient 0.22 diffuse 0.62 specular 0.22 roughness 0.04 }'


_MARGIN = 2.6          # 视野边缘余量 Å(两侧各一份;_view_spans 与 PNG 高度共用)


def _view_spans(coords: list, view: str):
    """视平面跨度与高宽比(pov_scene 与 render_poscar_views 的**唯一**口径。

    相机 up/right 与 PNG Width/Height 必须同比例,否则正交投影把球压成椭圆
    (真机侧视验收时踩过)。aspect 截断到 0.4~1.4 时,up 同步按截断值扩,保持像素方正。
    """
    if view not in ('top', 'side'):
        raise ValueError(f"view 只能是 top/side,收到 {view!r}")
    us = [c[0] for c in coords]
    vs = [c[1] if view == 'top' else c[2] for c in coords]
    span_u = max(max(us) - min(us) + 2 * _MARGIN, 4.0)
    span_v = max(max(vs) - min(vs) + 2 * _MARGIN, 4.0)
    aspect = max(0.4, min(1.4, span_v / span_u))
    return span_u, span_u * aspect, aspect


def pov_scene(symbols: list, coords: list, bonds: list, view: str = 'top') -> str:
    """结构 → POV SDL 场景文本。view: 'top'(x-y 俯视)| 'side'(x-z 侧视)。

    正交相机(工程制图感,无透视变形);right 取负号保持右手系(防镜像结构)。
    """
    span_u, span_up, _ = _view_spans(coords, view)
    n = len(coords)
    cx = sum(c[0] for c in coords) / n
    cy = sum(c[1] for c in coords) / n
    cz = sum(c[2] for c in coords) / n
    # 光源挂相机侧(主光=相机方向+右上偏移):侧视时若用世界坐标固定光,
    # 光会跑到物体背后,正面欠光发暗(真机验收踩过)
    if view == 'top':
        cam = f'<{cx:.3f},{cy:.3f},{cz + 60:.3f}>'
        up_axis, right_axis = 'y', 'x'
        key = (cx + 15, cy + 25, cz + 45)
        fill = (cx - 20, cy - 10, cz + 35)
    else:
        cam = f'<{cx:.3f},{cy - 60:.3f},{cz:.3f}>'
        up_axis, right_axis = 'z', 'x'
        key = (cx + 15, cy - 45, cz + 25)
        fill = (cx - 20, cy - 35, cz - 10)
    L = [
        '#version 3.7;',
        'global_settings { assumed_gamma 1.0 }',
        'background { color rgb <1,1,1> }',
        f'camera {{ orthographic location {cam} look_at <{cx:.3f},{cy:.3f},{cz:.3f}> '
        f'right -{right_axis}*{span_u:.3f} '
        f'up {up_axis}*{span_up:.3f} }}',
        f'light_source {{ <{key[0]:.1f},{key[1]:.1f},{key[2]:.1f}> color rgb <1,1,1> '
        f'area_light <8,0,0>,<0,8,0>, 3,3 adaptive 1 jitter }}',
        f'light_source {{ <{fill[0]:.1f},{fill[1]:.1f},{fill[2]:.1f}> color rgb <0.35,0.35,0.35> shadowless }}',
    ]
    for (i, j) in bonds:                                # 键先画(球体覆盖端头)
        a, b = coords[i], coords[j]
        mid = [(a[k] + b[k]) / 2 for k in range(3)]
        for (p, q, sym) in ((a, mid, symbols[i]), (mid, b, symbols[j])):
            L.append(
                f'cylinder {{ <{p[0]:.3f},{p[1]:.3f},{p[2]:.3f}>,'
                f'<{q[0]:.3f},{q[1]:.3f},{q[2]:.3f}>,{BOND_RADIUS} '
                f'pigment {{ color {_rgb(element_info(sym)[1])} }} {_fin()} }}')
    for sym, c in zip(symbols, coords):
        r, color = element_info(sym)
        L.append(f'sphere {{ <{c[0]:.3f},{c[1]:.3f},{c[2]:.3f}>,{r * RADIUS_SCALE:.3f} '
                 f'pigment {{ color {_rgb(color)} }} {_fin()} }}')
    return '\n'.join(L) + '\n'


def pov_ini(pov_name: str, png_name: str, width: int = 1200, height: int = 900) -> str:
    return (f'Input_File_Name={pov_name}\nOutput_File_Name={png_name}\n'
            f'Width={width}\nHeight={height}\n'
            'Output_File_Type=N\nAntialias=On\nAntialias_Threshold=0.05\n'
            'Display=Off\nPause_When_Done=Off\nVerbose=Off\n')


# ── 探测与调用 ─────────────────────────────────────────────────────────────────
def find_povray(configured: str = '') -> str | None:
    """POV-Ray 可执行探测:配置路径 → 默认安装位 → PATH。找不到 → None(降级)。"""
    if configured and os.path.isfile(configured):
        return configured
    for p in _DEFAULT_EXES:
        if os.path.isfile(p):
            return p
    return shutil.which('pvengine64') or shutil.which('pvengine')


def render_poscar_views(poscar_path, out_dir, basename: str = '', *,
                        views=('top', 'side'), width: int = 1200,
                        exe: str | None = None, configured_exe: str = '',
                        run=None, timeout: int = 180) -> dict:
    """渲染一个 POSCAR/CONTCAR 的多视角 PNG。返回 {'ok','images':{view:path},'error'}。

    - 缓存:输出 PNG 已存在且比结构文件新 → 跳过重渲(全自动链路不重复付费)。
    - 失败降级:POV-Ray 缺失/超时/非零退出 → ok=False + error 文本,不抛。
    - run 可注入(测试);默认 subprocess.run。
    """
    run = run or subprocess.run
    exe = exe or find_povray(configured_exe)
    if not exe:
        return {'ok': False, 'images': {}, 'error': 'POV-Ray 未找到(装 v3.7 或在配置里给 pvengine64 路径)'}
    try:
        with open(poscar_path, 'r', encoding='utf-8', errors='replace') as f:
            symbols, coords = parse_poscar_atoms(f.read())
    except (OSError, ValueError) as e:
        return {'ok': False, 'images': {}, 'error': f'结构解析失败:{e}'}
    bonds = build_bonds(symbols, coords)
    os.makedirs(out_dir, exist_ok=True)
    base = basename or os.path.splitext(os.path.basename(str(poscar_path)))[0]
    src_mtime = os.path.getmtime(poscar_path)
    images, errors = {}, []
    timed_out = False
    for view in views:
        png = os.path.join(out_dir, f'{base}_{view}.png')
        if os.path.isfile(png) and os.path.getmtime(png) >= src_mtime:
            images[view] = png                          # 缓存命中
            continue
        pov = os.path.join(out_dir, f'{base}_{view}.pov')
        ini = os.path.join(out_dir, f'{base}_{view}.ini')
        h = max(300, min(1600, int(width * _aspect(symbols, coords, view))))
        with open(pov, 'w', encoding='utf-8') as f:
            f.write(pov_scene(symbols, coords, bonds, view))
        with open(ini, 'w', encoding='utf-8') as f:
            f.write(pov_ini(os.path.basename(pov), os.path.basename(png), width, h))
        try:
            proc = run([exe, '/RENDER', os.path.basename(ini), '/EXIT', '/NORESTORE'],
                       cwd=out_dir, timeout=timeout,
                       stdin=subprocess.DEVNULL, capture_output=True)
            code = getattr(proc, 'returncode', 1)
        except subprocess.TimeoutExpired as e:
            timed_out = True
            errors.append(f'{view}: {e}')
            continue
        except OSError as e:
            errors.append(f'{view}: {e}')
            continue
        if code == 0 and os.path.isfile(png) and os.path.getsize(png) > 0:
            images[view] = png
        else:
            errors.append(f'{view}: pvengine 退出码 {code} 或无输出')
    return {'ok': bool(images), 'images': images,
            'error': '; '.join(errors), 'timed_out': timed_out}


def _aspect(symbols, coords, view) -> float:
    """PNG 高宽比 = 相机 up/right 比(同一口径,见 _view_spans)。"""
    return _view_spans(coords, view)[2]
