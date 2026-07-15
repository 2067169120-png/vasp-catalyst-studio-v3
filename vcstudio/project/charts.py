"""四种科研图的数据契约 + SVG 渲染(纯函数,零依赖,离线可测)。

双引擎架构的"兜底层":同一套数据契约同时供 external/origin_charts.py(出版级)与本模块
(SVG,内嵌 HTML 报告)使用——Origin 不可用时报告仍然有图(用户决策:SVG 兜底)。

契约(与原版 plotting.py 对齐,见 spec 2026-07-06):
- bar:     {'rows':[体系], 'cols':[物种], 'matrix':[[eV]], 'band':(lo,hi)|None, 'band_label':str}
- heatmap: {'rows':[体系], 'cols':[物种], 'matrix':[[eV|None]]}(None=缺格)
- ladder:  {'steps':[{'label','G','sub_label'?}], 'pds_index':int|None, 'u_l':float|None}
- conv:    {'x':[...], 'y':[eV], 'xlabel':str, 'epsilon':float}

配色沿用原版 PUB_COLORS(seaborn deep);SVG 文本交浏览器渲染,天然避开 matplotlib
CJK 全角数字坑(原版 plot_style.py 的 '−3. 12' bug)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import html
import math

# Paul Tol "bright" 色板:期刊出版标准、色盲安全(对齐论文级配色,替换原版 seaborn deep)
PUB_COLORS = ['#4477AA', '#EE6677', '#228833', '#CCBB44',
              '#66CCEE', '#AA3377', '#BBBBBB', '#222255']
BAND_FILL = '#EAF0F6'          # 理想窗口带(更柔和的蓝灰)
RDS_COLOR = '#EE6677'          # 决速步高亮(Tol 红)
_FONT = "font-family='Segoe UI,Microsoft YaHei,sans-serif'"


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ''))


def _nice_ticks(lo: float, hi: float, n: int = 6) -> list:
    """好看的轴刻度(1/2/2.5/5×10^k 步长)。lo==hi 时向两侧各扩 0.5。"""
    if hi <= lo:
        lo, hi = lo - 0.5, hi + 0.5
    raw = (hi - lo) / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 0.501:
        ticks.append(round(t, 10))
        t += step
    return ticks


class _Plot:
    """SVG 坐标系小助手:数据空间 → 像素空间 + 常用图元。"""

    def __init__(self, width, height, xlo, xhi, ylo, yhi,
                 ml=64, mr=18, mt=40, mb=46):
        self.w, self.h = width, height
        self.ml, self.mr, self.mt, self.mb = ml, mr, mt, mb
        self.xlo, self.xhi, self.ylo, self.yhi = xlo, xhi, ylo, yhi
        self.parts: list = []

    def x(self, v):
        return self.ml + (v - self.xlo) / (self.xhi - self.xlo) * (self.w - self.ml - self.mr)

    def y(self, v):
        return self.h - self.mb - (v - self.ylo) / (self.yhi - self.ylo) * (self.h - self.mt - self.mb)

    def add(self, s):
        self.parts.append(s)

    def yaxis(self, ticks, fmt='{:g}'):
        for t in ticks:
            if not (self.ylo - 1e-9 <= t <= self.yhi + 1e-9):
                continue
            py = self.y(t)
            self.add(f"<line x1='{self.ml}' y1='{py:.1f}' x2='{self.w - self.mr}' y2='{py:.1f}' "
                     f"stroke='#e5e7eb' stroke-width='1' stroke-dasharray='3,3'/>")
            self.add(f"<text x='{self.ml - 8}' y='{py + 4:.1f}' text-anchor='end' "
                     f"font-size='11' fill='#6b7280'>{fmt.format(t)}</text>")

    def frame(self):
        self.add(f"<line x1='{self.ml}' y1='{self.mt}' x2='{self.ml}' y2='{self.h - self.mb}' "
                 f"stroke='#374151' stroke-width='1.2'/>")
        self.add(f"<line x1='{self.ml}' y1='{self.h - self.mb}' x2='{self.w - self.mr}' "
                 f"y2='{self.h - self.mb}' stroke='#374151' stroke-width='1.2'/>")

    def title(self, text):
        if text:
            self.add(f"<text x='{(self.ml + self.w - self.mr) / 2:.0f}' y='{self.mt - 16}' "
                     f"text-anchor='middle' font-size='14' font-weight='600' "
                     f"fill='#111827'>{_esc(text)}</text>")

    def ylabel(self, text):
        if text:
            self.add(f"<text x='14' y='{(self.mt + self.h - self.mb) / 2:.0f}' font-size='12' "
                     f"fill='#374151' text-anchor='middle' "
                     f"transform='rotate(-90 14 {(self.mt + self.h - self.mb) / 2:.0f})'>{_esc(text)}</text>")

    def svg(self) -> str:
        return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {self.w} {self.h}' "
                f"width='{self.w}' height='{self.h}' {_FONT}>"
                f"<rect width='{self.w}' height='{self.h}' fill='white'/>"
                + ''.join(self.parts) + '</svg>')


# ── 1. ΔE 柱状图(旗舰:分组柱 + 可配理想窗口带 + 柱顶数值) ─────────────────────
def render_bar_svg(data: dict, *, title='', ylabel='E_ads (eV)',
                   width=760, height=420) -> str:
    rows, cols = list(data['rows']), list(data['cols'])
    mat = data['matrix']
    band = data.get('band')
    vals = [v for r in mat for v in r if v is not None]
    if not vals:
        raise ValueError('柱状图矩阵为空,无可画数据')
    ylo = min(vals + ([band[0]] if band else []) + [0])
    yhi = max(vals + ([band[1]] if band else []) + [0])
    pad = (yhi - ylo) * 0.18 or 0.5
    p = _Plot(width, height, 0, len(cols), ylo - pad, yhi + pad)
    p.title(title)
    p.ylabel(ylabel)
    p.yaxis(_nice_ticks(ylo - pad, yhi + pad))
    if band:
        by1, by2 = p.y(band[1]), p.y(band[0])
        p.add(f"<rect x='{p.ml}' y='{by1:.1f}' width='{width - p.ml - p.mr}' "
              f"height='{by2 - by1:.1f}' fill='{BAND_FILL}' opacity='0.8'/>")
        if data.get('band_label'):
            p.add(f"<text x='{width - p.mr - 6}' y='{by1 + 13:.1f}' text-anchor='end' "
                  f"font-size='10.5' fill='#64748b'>{_esc(data['band_label'])}</text>")
    zero = p.y(0)
    p.add(f"<line x1='{p.ml}' y1='{zero:.1f}' x2='{width - p.mr}' y2='{zero:.1f}' "
          f"stroke='#374151' stroke-width='1'/>")
    gw = 0.7 / max(len(rows), 1)                       # 组内单柱宽(数据坐标)
    for i, col in enumerate(cols):
        p.add(f"<text x='{p.x(i + 0.5):.1f}' y='{height - p.mb + 16}' text-anchor='middle' "
              f"font-size='11.5' fill='#374151'>{_esc(col)}</text>")
        for j, row in enumerate(rows):
            v = mat[j][i]
            if v is None:
                continue
            cx = i + 0.5 + (j - (len(rows) - 1) / 2) * gw
            x1 = p.x(cx - gw / 2 * 0.92)
            x2 = p.x(cx + gw / 2 * 0.92)
            y1, y2 = sorted((p.y(v), zero))
            color = PUB_COLORS[j % len(PUB_COLORS)]
            p.add(f"<rect x='{x1:.1f}' y='{y1:.1f}' width='{x2 - x1:.1f}' "
                  f"height='{max(y2 - y1, 0.5):.1f}' fill='{color}' opacity='0.92' "
                  f"stroke='#444444' stroke-width='0.6'/>")
            ty = p.y(v) + (-5 if v >= 0 else 13)       # 数值标注:正柱上方,负柱下方
            p.add(f"<text x='{p.x(cx):.1f}' y='{ty:.1f}' text-anchor='middle' "
                  f"font-size='10' fill='#1f2937'>{v:.2f}</text>")
    if len(rows) > 1:                                   # 图例(多体系才画)
        for j, row in enumerate(rows):
            lx, ly = p.ml + 10 + j * 130, p.mt - 2
            p.add(f"<rect x='{lx}' y='{ly - 9}' width='11' height='11' "
                  f"fill='{PUB_COLORS[j % len(PUB_COLORS)]}'/>")
            p.add(f"<text x='{lx + 15}' y='{ly + 1}' font-size='11' "
                  f"fill='#374151'>{_esc(row)}</text>")
    p.frame()
    return p.svg()


# ── 2. 多体系热图(RdYlGn_r 色标 + 格内数值) ────────────────────────────────────
_HEAT_ANCHORS = [(0.0, (26, 152, 80)), (0.5, (255, 255, 191)), (1.0, (215, 48, 39))]


def _heat_color(v: float, vmin=-5.0, vmax=1.0) -> str:
    """RdYlGn_r:强吸附(负)→绿,弱/正→红。分段线性插值。"""
    t = min(max((v - vmin) / (vmax - vmin), 0.0), 1.0)
    for (t1, c1), (t2, c2) in zip(_HEAT_ANCHORS, _HEAT_ANCHORS[1:]):
        if t <= t2:
            f = (t - t1) / (t2 - t1) if t2 > t1 else 0
            rgb = [round(a + (b - a) * f) for a, b in zip(c1, c2)]
            return f'rgb({rgb[0]},{rgb[1]},{rgb[2]})'
    return 'rgb(215,48,39)'


def render_heatmap_svg(data: dict, *, title='', width=760, height=None,
                       vmin=-5.0, vmax=1.0) -> str:
    rows, cols = list(data['rows']), list(data['cols'])
    mat = data['matrix']
    if not rows or not cols:
        raise ValueError('热图行列为空')
    ml, mt, mr, mb = 110, 46, 18, 34
    ch = 34                                            # 单元格高
    height = height or (mt + mb + ch * len(rows))
    cw = (width - ml - mr) / len(cols)
    p = _Plot(width, height, 0, 1, 0, 1, ml=ml, mr=mr, mt=mt, mb=mb)
    p.title(title)
    for i, r in enumerate(rows):
        p.add(f"<text x='{ml - 8}' y='{mt + i * ch + ch / 2 + 4:.1f}' text-anchor='end' "
              f"font-size='11.5' fill='#374151'>{_esc(r)}</text>")
        for j, c in enumerate(cols):
            v = mat[i][j] if j < len(mat[i]) else None
            x, y = ml + j * cw, mt + i * ch
            if v is None:
                p.add(f"<rect x='{x:.1f}' y='{y}' width='{cw:.1f}' height='{ch}' "
                      f"fill='#f3f4f6' stroke='white'/>")
                continue
            p.add(f"<rect x='{x:.1f}' y='{y}' width='{cw:.1f}' height='{ch}' "
                  f"fill='{_heat_color(v, vmin, vmax)}' stroke='white'/>")
            tc = 'white' if v < -1 else '#1f2937'      # 深绿格用白字(原版口径)
            p.add(f"<text x='{x + cw / 2:.1f}' y='{y + ch / 2 + 4}' text-anchor='middle' "
                  f"font-size='10.5' fill='{tc}'>{v:.2f}</text>")
    for j, c in enumerate(cols):
        p.add(f"<text x='{ml + j * cw + cw / 2:.1f}' y='{mt + len(rows) * ch + 16}' "
              f"text-anchor='middle' font-size='11.5' fill='#374151'>{_esc(c)}</text>")
    return p.svg()


# ── 3. 自由能阶梯图(RDS 高亮 + U_L 标注) ───────────────────────────────────────
def ladder_data(steps: list, u_l: float | None = None) -> dict:
    """steps=[{'label','G','sub_label'?}] → 契约(补 pds_index=最陡上坡步)。"""
    if len(steps) < 2:
        raise ValueError('阶梯图至少需要 2 个状态')
    diffs = [steps[i + 1]['G'] - steps[i]['G'] for i in range(len(steps) - 1)]
    pds = max(range(len(diffs)), key=lambda i: diffs[i]) if diffs else None
    return {'steps': list(steps), 'pds_index': pds, 'u_l': u_l}


def render_ladder_svg(data: dict, *, title='', ylabel='ΔG (eV)',
                      width=760, height=440) -> str:
    steps = data['steps']
    pds = data.get('pds_index')
    gs = [s['G'] for s in steps]
    pad = (max(gs) - min(gs)) * 0.22 or 0.5
    p = _Plot(width, height, -0.5, len(steps) - 0.5, min(gs) - pad, max(gs) + pad)
    p.title(title)
    p.ylabel(ylabel)
    p.yaxis(_nice_ticks(min(gs) - pad, max(gs) + pad))
    half = 0.3
    for i, s in enumerate(steps):
        y = p.y(s['G'])
        p.add(f"<line x1='{p.x(i - half):.1f}' y1='{y:.1f}' x2='{p.x(i + half):.1f}' "
              f"y2='{y:.1f}' stroke='{PUB_COLORS[0]}' stroke-width='3.5'/>")
        p.add(f"<text x='{p.x(i):.1f}' y='{y - 9:.1f}' text-anchor='middle' font-size='11' "
              f"font-weight='600' fill='#1f2937'>{_esc(s['label'])}</text>")
        if s.get('sub_label'):
            p.add(f"<text x='{p.x(i):.1f}' y='{y + 18:.1f}' text-anchor='middle' "
                  f"font-size='9.5' fill='#9ca3af'>{_esc(s['sub_label'])}</text>")
        if i < len(steps) - 1:                          # 连接线;决速步红色加粗实线,其余灰虚线
            ny = p.y(steps[i + 1]['G'])
            is_pds = (i == pds)
            dash = '' if is_pds else " stroke-dasharray='4,3'"
            p.add(f"<line x1='{p.x(i + half):.1f}' y1='{y:.1f}' x2='{p.x(i + 1 - half):.1f}' "
                  f"y2='{ny:.1f}' stroke='{RDS_COLOR if is_pds else '#999999'}' "
                  f"stroke-width='{2.6 if is_pds else 1.3}'{dash}/>")
    if pds is not None:
        dg = steps[pds + 1]['G'] - steps[pds]['G']
        p.add(f"<text x='{p.x(pds + 0.5):.1f}' y='{p.mt + 14}' text-anchor='middle' "
              f"font-size='11.5' font-weight='600' fill='{RDS_COLOR}'>"
              f"PDS: ΔG = {dg:+.2f} eV</text>")
    if data.get('u_l') is not None:
        p.add(f"<text x='{width - p.mr - 6}' y='{p.mt + 14}' text-anchor='end' "
              f"font-size='11.5' fill='#374151'>U_L = {data['u_l']:.2f} V</text>")
    p.frame()
    return p.svg()


# ── 4. 收敛曲线(±ε 带 + 达标点星标) ────────────────────────────────────────────
def render_convergence_svg(data: dict, *, title='', width=760, height=420) -> str:
    xs, ys = list(data['x']), list(data['y'])
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError('收敛曲线需要等长且 ≥2 点的 x/y')
    eps = float(data.get('epsilon', 0.001))
    ref = ys[-1]                                        # 以最后一点为收敛参考
    ylo, yhi = min(ys + [ref - eps]), max(ys + [ref + eps])
    pad = (yhi - ylo) * 0.2 or eps * 4
    p = _Plot(width, height, min(xs), max(xs), ylo - pad, yhi + pad)
    p.title(title)
    p.ylabel('E (eV)')
    p.yaxis(_nice_ticks(ylo - pad, yhi + pad), fmt='{:.3f}')
    by1, by2 = p.y(ref + eps), p.y(ref - eps)          # ±ε 收敛带
    p.add(f"<rect x='{p.ml}' y='{by1:.1f}' width='{width - p.ml - p.mr}' "
          f"height='{by2 - by1:.1f}' fill='#d9ead9' opacity='0.6'/>")
    pts = ' '.join(f'{p.x(x):.1f},{p.y(y):.1f}' for x, y in zip(xs, ys))
    p.add(f"<polyline points='{pts}' fill='none' stroke='{PUB_COLORS[0]}' stroke-width='2'/>")
    converged_at = None
    for x, y in zip(xs, ys):
        ok = abs(y - ref) <= eps
        if ok and converged_at is None:
            converged_at = x
        p.add(f"<circle cx='{p.x(x):.1f}' cy='{p.y(y):.1f}' r='4' "
              f"fill='{'#2a7a3b' if ok else PUB_COLORS[0]}' stroke='white' stroke-width='1'/>")
        p.add(f"<text x='{p.x(x):.1f}' y='{p.y(y) - 10:.1f}' text-anchor='middle' "
              f"font-size='9.5' fill='#6b7280'>{y:.3f}</text>")
        p.add(f"<text x='{p.x(x):.1f}' y='{height - p.mb + 16}' text-anchor='middle' "
              f"font-size='11' fill='#374151'>{_esc(x)}</text>")
    if converged_at is not None:
        p.add(f"<text x='{p.x(converged_at):.1f}' y='{p.mt + 14}' text-anchor='middle' "
              f"font-size='12' fill='#2a7a3b'>★ 收敛于 {converged_at}(±{eps:g} eV)</text>")
    if data.get('xlabel'):
        p.add(f"<text x='{(p.ml + width - p.mr) / 2:.0f}' y='{height - 8}' text-anchor='middle' "
              f"font-size='12' fill='#374151'>{_esc(data['xlabel'])}</text>")
    p.frame()
    return p.svg()


# ── 5. 总 DOS(C4:E−E_F 横轴 + 自旋镜像 + 费米线) ─────────────────────────────
def render_dos_svg(data: dict, *, title='', width=760, height=420,
                   window=(-8.0, 6.0)) -> str:
    """dosparse.parse_vasprun_dos 输出 → 出版风格总 DOS SVG。

    x = E − E_F(默认窗口 −8…6 eV,与数据范围取交集);自旋向下取负镜像;
    x=0 处 E_F 竖虚线。空数据 → ValueError。
    """
    energies = list(data.get('energies') or [])
    up = list(data.get('spin_up') or [])
    down = data.get('spin_down')
    ef = data.get('efermi')
    if not energies or not up or ef is None:
        raise ValueError('DOS 数据为空,无可画内容')
    xs = [e - ef for e in energies]
    xlo = max(window[0], min(xs))
    xhi = min(window[1], max(xs))
    if xhi <= xlo:                       # 数据全在窗口外 → 用数据全范围
        xlo, xhi = min(xs), max(xs)
    vis = [i for i, x in enumerate(xs) if xlo - 1e-9 <= x <= xhi + 1e-9]
    if not vis:
        raise ValueError('窗口内无 DOS 数据点')
    peak = max([up[i] for i in vis] +
               ([abs(down[i]) for i in vis] if down else [0.0])) or 1.0
    ylo = -peak * 1.1 if down else 0.0
    yhi = peak * 1.1
    p = _Plot(width, height, xlo, xhi, ylo, yhi)
    p.title(title)
    p.ylabel('DOS (states/eV)')
    p.yaxis(_nice_ticks(ylo, yhi))
    # x 轴刻度
    for t in _nice_ticks(xlo, xhi):
        if not (xlo - 1e-9 <= t <= xhi + 1e-9):
            continue
        p.add(f"<text x='{p.x(t):.1f}' y='{height - p.mb + 16}' text-anchor='middle' "
              f"font-size='11' fill='#374151'>{t:g}</text>")
    if down:                              # 自旋镜像基线
        zy = p.y(0)
        p.add(f"<line x1='{p.ml}' y1='{zy:.1f}' x2='{width - p.mr}' y2='{zy:.1f}' "
              f"stroke='#9ca3af' stroke-width='0.8'/>")
    # E_F 竖虚线 @ x=0
    if xlo <= 0 <= xhi:
        fx = p.x(0)
        p.add(f"<line x1='{fx:.1f}' y1='{p.mt}' x2='{fx:.1f}' y2='{height - p.mb}' "
              f"stroke='#374151' stroke-width='1' stroke-dasharray='5,4'/>")
        p.add(f"<text x='{fx + 4:.1f}' y='{p.mt + 12}' font-size='10.5' "
              f"fill='#374151'>E_F</text>")

    def _path(vals, sign, color):
        d = ' '.join(f"{'M' if k == 0 else 'L'}{p.x(xs[i]):.1f},{p.y(sign * vals[i]):.1f}"
                     for k, i in enumerate(vis))
        p.add(f"<path d='{d}' fill='none' stroke='{color}' stroke-width='1.6'/>")

    _path(up, 1, PUB_COLORS[0])
    if down:
        _path(down, -1, PUB_COLORS[1])
        # 图例(双自旋才画)
        lx, ly = p.ml + 10, p.mt - 2
        p.add(f"<rect x='{lx}' y='{ly - 9}' width='11' height='11' fill='{PUB_COLORS[0]}'/>")
        p.add(f"<text x='{lx + 15}' y='{ly + 1}' font-size='11' fill='#374151'>spin up</text>")
        p.add(f"<rect x='{lx + 90}' y='{ly - 9}' width='11' height='11' fill='{PUB_COLORS[1]}'/>")
        p.add(f"<text x='{lx + 105}' y='{ly + 1}' font-size='11' fill='#374151'>spin down</text>")
    p.add(f"<text x='{(p.ml + width - p.mr) / 2:.0f}' y='{height - 8}' text-anchor='middle' "
          f"font-size='12' fill='#374151'>E − E_F (eV)</text>")
    p.frame()
    return p.svg()


# ── 契约构建助手:从项目 ΔE 结果直接出图数据 ─────────────────────────────────────
def bar_data_from_delta(project_name: str, delta_rows: list,
                        band=None, band_label='') -> dict:
    """adsorption.delta_e_rows 的 rows → 单体系柱状图契约(只取有 ΔE 的组态)。"""
    cols = [r['name'] for r in delta_rows if r.get('delta_e') is not None]
    vals = [r['delta_e'] for r in delta_rows if r.get('delta_e') is not None]
    if not cols:
        raise ValueError('没有任何组态有 ΔE(需成员全部 DONE)')
    return {'rows': [project_name], 'cols': cols, 'matrix': [vals],
            'band': band, 'band_label': band_label}


def heatmap_data_from_projects(projects: list) -> dict:
    """多项目 → 热图契约:rows=项目名,cols=组态名并集(保序),缺格 None。"""
    cols: list = []
    for _, rows in projects:
        for r in rows:
            if r['name'] not in cols:
                cols.append(r['name'])
    matrix = []
    for _, rows in projects:
        vals = {r['name']: r.get('delta_e') for r in rows}
        matrix.append([vals.get(c) for c in cols])
    return {'rows': [name for name, _ in projects], 'cols': cols, 'matrix': matrix}
