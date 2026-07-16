"""原生 matplotlib 论文级出图引擎:不依赖 Origin/POV-Ray,开箱即得论文质量的图。

定位(2026-07):Origin 适配器(origin_charts.py)要求用户本机装 Origin+originpro,
POV-Ray 同理——大多数用户没装。本模块用 matplotlib 直接渲染论文级图,作为分析层
的第三条出图路径:Origin(有则最优)→ 本模块(默认论文级)→ SVG(零依赖兜底)。

论文级规范(apply_paper_style / paper_rc,对齐 Nature 系期刊惯例):
- serif 字体族(Times New Roman → STIX Two Text → DejaVu Serif 逐级回退)+ STIX 数学字体;
- 刻度朝内、默认去顶/右边框(box=True 可四边框)、轴线/刻度线宽统一 0.9 pt;
- 300 dpi 位图 + Type 42(TrueType)内嵌字体的矢量 PDF/SVG,期刊直接可投;
- Paul Tol bright / NPG / Okabe-Ito 三套色盲安全配色;
- 单栏 89 mm / 双栏 183 mm 标准图宽;多面板 a/b/c/d 粗体标号(add_panel_label)。

依赖策略(硬约束):matplotlib/numpy 属可选分析层依赖(pip install vcstudio[charts]),
本模块顶层零重依赖——全部延迟到函数内 import,core/EXE 体积不受影响;渲染走
Figure+FigureCanvasAgg 纯离屏路径,不碰 pyplot 全局状态,CI 无显示环境天然可跑。

数据契约(简单 dict/list,与 project/charts.py 的 SVG 契约互补,详见各函数 docstring):
- adsorption_bar:     {'adsorbates': [...], 'substrates': {name: [eV, ...]}}
- energy_matrix_table: 同上 + 可选 'dg': {name: [eV, ...]}
- free_energy_ladder:  [{'name': str, 'G': [eV, ...]}, ...]

架构可扩展到全套 VASP 图(DOS/能带/收敛曲线/火山图):新图种 = apply_paper_style()
上下文 + _new_figure() + _save_dual(),规范层完全复用。中文注释允许,英文标识符。
"""
from __future__ import annotations

import contextlib
import csv
import os
import re

# ── 论文级配色(全部色盲安全) ────────────────────────────────────────────────
PALETTES = {
    # Paul Tol "bright":多系列折线/柱的默认,与 project/charts.py PUB_COLORS 同源
    'tol_bright': ['#4477AA', '#EE6677', '#228833', '#CCBB44',
                   '#66CCEE', '#AA3377', '#BBBBBB', '#222255'],
    # Nature Publishing Group 风(对齐 origin_charts.py 单体系柱状图口径)
    'npg': ['#3C5488', '#E64B35', '#00A087', '#4DBBD5',
            '#F39B7F', '#8491B4', '#91D1C2', '#DC0000'],
    # Okabe-Ito:色盲安全金标准
    'okabe_ito': ['#0072B2', '#D55E00', '#009E73', '#E69F00',
                  '#56B4E9', '#CC79A7', '#F0E442', '#000000'],
}
PDS_COLOR = '#C92A2A'          # 决速步红(与 Origin 口径一致)
GRID_COLOR = '#D9D9D9'
ZERO_LINE_COLOR = '#8C8C8C'

# 期刊标准图宽(英寸):单栏 89 mm、1.5 栏 120 mm、双栏 183 mm
SINGLE_COL = 3.50
ONE_HALF_COL = 4.72
DOUBLE_COL = 7.20

_SUBSCRIPT_RE = re.compile(r'(?<=[A-Za-z\)])(\d+)')


def chem_label(name) -> str:
    """化学式排版:'Li2S8' → 'Li$_{2}$S$_{8}$'(mathtext 真下标)。已含 $ 的原样返回。"""
    s = str(name if name is not None else '')
    if '$' in s:
        return s
    return _SUBSCRIPT_RE.sub(r'$_{\1}$', s)


# ── 论文级风格层 ────────────────────────────────────────────────────────────

def paper_rc(*, font: str = 'serif', base_size: float = 9.0, box: bool = False,
             palette: str = 'tol_bright') -> dict:
    """构建论文级 rcParams 字典(纯函数,可单独测)。

    参数:
        font: 'serif'(默认,Times 系)或 'sans'(Arial 系,部分期刊要求)。
        base_size: 正文字号 pt(单栏图 8-9 pt 为宜)。
        box: True 保留四边框+顶右刻度(PRB 风);False 去顶右边框(Nature 风)。
        palette: PALETTES 键名,决定默认系列色循环。
    """
    from cycler import cycler
    colors = PALETTES.get(palette, PALETTES['tol_bright'])
    family = 'serif' if font == 'serif' else 'sans-serif'
    return {
        # 字体:serif 逐级回退(Windows→Linux CI 都有着落);数学字体与正文谐调
        'font.family': family,
        'font.serif': ['Times New Roman', 'STIX Two Text', 'Nimbus Roman',
                       'Liberation Serif', 'DejaVu Serif'],
        'font.sans-serif': ['Arial', 'Helvetica', 'Nimbus Sans',
                            'Liberation Sans', 'DejaVu Sans'],
        'mathtext.fontset': 'stix' if family == 'serif' else 'stixsans',
        'font.size': base_size,
        'axes.titlesize': base_size + 1,
        'axes.labelsize': base_size,
        'xtick.labelsize': base_size - 0.5,
        'ytick.labelsize': base_size - 0.5,
        'legend.fontsize': base_size - 0.5,
        # 全角/缺字形防线:U+2212 在部分 serif 字体缺字,用 ASCII 连字符渲负号
        'axes.unicode_minus': False,
        # 线宽/刻度:轴线与刻度同宽,刻度朝内(期刊惯例)
        'axes.linewidth': 0.9,
        'lines.linewidth': 1.4,
        'lines.markersize': 4.5,
        'xtick.direction': 'in',
        'ytick.direction': 'in',
        'xtick.major.size': 3.4, 'ytick.major.size': 3.4,
        'xtick.minor.size': 2.0, 'ytick.minor.size': 2.0,
        'xtick.major.width': 0.9, 'ytick.major.width': 0.9,
        'xtick.minor.width': 0.7, 'ytick.minor.width': 0.7,
        'xtick.top': box, 'ytick.right': box,
        'axes.spines.top': box, 'axes.spines.right': box,
        'axes.axisbelow': True,          # 网格/参考线垫在数据下
        # 系列配色
        'axes.prop_cycle': cycler(color=colors),
        # 图例:无框(Nature 惯例)
        'legend.frameon': False,
        'legend.handlelength': 1.4,
        'legend.borderaxespad': 0.4,
        # 导出:300 dpi 位图;PDF/PS 用 Type 42 内嵌 TrueType(期刊/Illustrator 可编辑)
        'figure.dpi': 120,
        'savefig.dpi': 300,
        'savefig.bbox': 'tight',
        'savefig.pad_inches': 0.03,
        'pdf.fonttype': 42,
        'ps.fonttype': 42,
        'svg.fonttype': 'none',
    }


@contextlib.contextmanager
def apply_paper_style(*, font: str = 'serif', base_size: float = 9.0,
                      box: bool = False, palette: str = 'tol_bright'):
    """论文级风格上下文:with apply_paper_style(): ... 内部全部作图套用统一规范。

    进出上下文自动恢复原 rcParams(rc_context),不污染宿主进程里其他 matplotlib 使用者。
    参数含义见 paper_rc()。
    """
    import matplotlib as mpl
    with mpl.rc_context(rc=paper_rc(font=font, base_size=base_size,
                                    box=box, palette=palette)):
        yield


def _new_figure(width: float = SINGLE_COL, aspect: float = 0.75,
                nrows: int = 1, ncols: int = 1, **subplot_kw):
    """离屏 Figure(不走 pyplot,无显示环境天然可用)。返回 (fig, axes)。"""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=(width, width * aspect))
    FigureCanvasAgg(fig)             # 绑 Agg 画布;savefig 按扩展名自动切 PDF/SVG 后端
    axes = fig.subplots(nrows, ncols, **subplot_kw)
    return fig, axes


def _save_dual(fig, out_path, formats=('png', 'pdf')) -> list:
    """按 out_path 词干导出多格式(默认 PNG 300dpi + 矢量 PDF)。返回绝对路径列表。"""
    stem, ext = os.path.splitext(os.path.abspath(str(out_path)))
    if ext.lower().lstrip('.') not in ('png', 'pdf', 'svg', 'eps', ''):
        stem = stem + ext              # 词干本身带点(如 v1.2)则不当扩展名剥
    os.makedirs(os.path.dirname(stem) or '.', exist_ok=True)
    paths = []
    for fmt in formats:
        p = f'{stem}.{fmt}'
        fig.savefig(p, format=fmt)
        paths.append(p)
    return paths


def add_panel_label(ax, letter: str, *, dx: float = -0.16, dy: float = 1.02,
                    fontsize: float | None = None) -> None:
    """多面板 a/b/c/d 标号:粗体、置于坐标区左上角外侧(Nature 惯例小写无括号)。

    dx/dy 为 axes 分数坐标偏移;组合大图时逐面板调用即可。
    """
    import matplotlib as mpl
    size = fontsize if fontsize is not None else mpl.rcParams['font.size'] + 2
    ax.text(dx, dy, str(letter), transform=ax.transAxes, fontsize=size,
            fontweight='bold', ha='left', va='bottom')


# ── 内部小工具 ──────────────────────────────────────────────────────────────

def _as_float(v) -> float:
    return float('nan') if v is None else float(v)


def _validate_matrix(data: dict) -> tuple:
    """校验 {'adsorbates', 'substrates'} 契约,返回 (adsorbates, substrates)。"""
    ads = list(data.get('adsorbates') or [])
    subs = data.get('substrates') or {}
    if not ads or not subs:
        raise ValueError("data 需含非空 'adsorbates' 列表与 'substrates' 字典")
    for name, vals in subs.items():
        if len(vals) != len(ads):
            raise ValueError(f"基底 '{name}' 的数值个数 {len(vals)} != 吸附质个数 {len(ads)}")
    return ads, dict(subs)


# ── 图 1:吸附能分组柱状图(对标论文图 3.23a) ────────────────────────────────

def adsorption_bar(data: dict, out_path, *, ylabel: str | None = None,
                   title: str = '', negative_up: bool = False,
                   value_labels: bool = True, value_fmt: str = '{:.2f}',
                   width: float | None = None, palette: str = 'npg',
                   panel: str = '', formats=('png', 'pdf'),
                   style_kw: dict | None = None) -> list:
    """吸附能分组柱状图:一组吸附质 × 一组基底,柱顶标数值。

    数据契约:
        data = {
            'adsorbates': ['S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S2', 'Li2S'],
            'substrates': {'CoO': [-1.2, ...], 'Co9S8': [...], ...},  # 缺值用 None
        }
        substrates 保持插入序;每个基底的列表长度必须等于 adsorbates 长度。
        画 ΔG 吸附柱状图时数据结构相同,传 ylabel=r'$\\Delta G_\\mathrm{ads}$ (eV)' 即可。

    参数:
        out_path:     输出路径(扩展名可省;按 formats 导出多份,默认 PNG+PDF)。
        ylabel:       纵轴标签;None → 'E_ads (eV)'(mathtext 真下标)。
        negative_up:  True 反转纵轴(锂硫惯例:吸附越强/越负,柱越向上)。
        value_labels: 柱端标注数值(自动落在柱尖外侧,跟随 negative_up 方向)。
        width:        图宽英寸;None 按柱数自适应(≥单栏 89 mm)。
        palette:      PALETTES 键名(默认 NPG,对齐 Origin 单体系柱状图口径)。
        panel:        非空则加多面板标号(如 'a')。
        style_kw:     透传 apply_paper_style 的额外风格参数(font/base_size/box)。

    返回:导出文件绝对路径列表(与 formats 同序)。
    """
    ads, subs = _validate_matrix(data)
    n, m = len(ads), len(subs)
    fig_w = width if width is not None else max(SINGLE_COL, 0.17 * n * m + 1.3)

    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.72)
        group_w = 0.78                          # 组内柱簇总宽(留 0.22 组间距)
        bw = group_w / m
        all_vals = []
        for j, (sub, vals) in enumerate(subs.items()):
            xs = [i - group_w / 2 + (j + 0.5) * bw for i in range(n)]
            ys = [_as_float(v) for v in vals]
            all_vals += [y for y in ys if y == y]
            ax.bar(xs, ys, width=bw * 0.92, label=chem_label(sub),
                   edgecolor='black', linewidth=0.5, zorder=3)
            if value_labels:
                for x, y in zip(xs, ys):
                    if y != y:                  # NaN(缺值)不标
                        continue
                    tip_up = (y < 0) if negative_up else (y >= 0)  # 柱尖在屏幕上/下方
                    ax.annotate(value_fmt.format(y), (x, y),
                                xytext=(0, 2.5 if tip_up else -2.5),
                                textcoords='offset points', ha='center',
                                va='bottom' if tip_up else 'top',
                                fontsize=max(5.5, 7.5 - 0.14 * n * m), zorder=4)
        if not all_vals:
            all_vals = [0.0]
        lo, hi = min(all_vals + [0.0]), max(all_vals + [0.0])
        pad = max(hi - lo, 0.5) * (0.14 if value_labels else 0.06)
        ax.set_ylim(lo - (pad if lo < 0 else 0), hi + (pad if hi > 0 else 0))
        if lo < 0 < hi:
            ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.8, zorder=2)
        if negative_up:
            ax.invert_yaxis()
        ax.set_xticks(range(n))
        ax.set_xticklabels([chem_label(a) for a in ads])
        ax.tick_params(axis='x', length=0)      # 类目轴不需要刻度线
        ax.set_xlim(-0.65, n - 0.35)
        ax.set_ylabel(ylabel if ylabel is not None else r'$E_\mathrm{ads}$ (eV)')
        if title:
            ax.set_title(title)
        if m > 1:
            ax.legend(ncols=min(m, 4), loc='best', columnspacing=1.0)
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)


# ── 图 2:吸附能数据表(三线表,对标论文表 3-2) ──────────────────────────────

def energy_matrix_table(data: dict, out_path, *, fmt: str = '{:.2f}',
                        de_header: str = 'ΔE (eV)', dg_header: str = 'ΔG (eV)',
                        title: str = '', formats=('png', 'pdf'),
                        style_kw: dict | None = None) -> list:
    """吸附质 × 基底能量数据表:三线表风格 PNG/PDF + 同名 CSV。

    数据契约:
        data = {
            'adsorbates': ['S8', 'Li2S8', ...],
            'substrates': {'CoO': [ΔE, ...], 'Co9S8': [...]},   # 缺值用 None → 表中 '--'
            'dg':         {'CoO': [ΔG, ...]},                    # 可选:有则每基底加 ΔG 列
        }
        'dg' 只需给出有 ΔG 数据的基底,长度同 adsorbates。

    导出:PNG(300 dpi)+ PDF(矢量,三线表:顶/底粗线 + 表头下细线)+ CSV
    (utf-8-sig,Excel 直接双开不乱码)。返回 [png, pdf, csv] 绝对路径列表。
    """
    ads, subs = _validate_matrix(data)
    dg = dict(data.get('dg') or {})
    for name, vals in dg.items():
        if name not in subs:
            raise ValueError(f"'dg' 中的基底 '{name}' 不在 'substrates' 里")
        if len(vals) != len(ads):
            raise ValueError(f"'dg' 基底 '{name}' 的数值个数 != 吸附质个数")

    # 列结构:吸附质 | 每基底 ΔE [| ΔG]
    headers, columns = ['Adsorbate'], []
    for sub, vals in subs.items():
        headers.append(f'{chem_label(sub)}\n{de_header}')
        columns.append([_as_float(v) for v in vals])
        if sub in dg:
            headers.append(f'{chem_label(sub)}\n{dg_header}')
            columns.append([_as_float(v) for v in dg[sub]])

    def _cell(v: float) -> str:
        return '--' if v != v else fmt.format(v)

    ncols, nrows = len(headers), len(ads)
    with apply_paper_style(**(style_kw or {})):
        col_w = [1.15] + [0.95] * (ncols - 1)                # 英寸
        fig_w = sum(col_w) + 0.3
        row_h, head_h = 0.26, 0.44
        fig_h = head_h + row_h * nrows + (0.35 if title else 0.15) + 0.15
        fig, ax = _new_figure(width=fig_w, aspect=fig_h / fig_w)
        ax.set_axis_off()
        ax.set_xlim(0, fig_w)
        ax.set_ylim(0, fig_h)

        x_edges = [0.15]
        for w in col_w:
            x_edges.append(x_edges[-1] + w)
        y_top = fig_h - (0.35 if title else 0.15)
        y_head = y_top - head_h
        y_bot = y_head - row_h * nrows

        if title:
            ax.text((x_edges[0] + x_edges[-1]) / 2, y_top + 0.12, title,
                    ha='center', va='bottom', fontweight='bold')
        # 三线:顶/底 1.2 pt,表头下 0.7 pt(booktabs 口径)
        for y, lw in ((y_top, 1.2), (y_head, 0.7), (y_bot, 1.2)):
            ax.plot([x_edges[0], x_edges[-1]], [y, y], color='black',
                    lw=lw, solid_capstyle='butt')
        for k, h in enumerate(headers):
            xc = (x_edges[k] + x_edges[k + 1]) / 2
            ax.text(xc, (y_top + y_head) / 2, h, ha='center', va='center',
                    fontweight='bold', linespacing=1.25)
        for i, a in enumerate(ads):
            yc = y_head - row_h * (i + 0.5)
            ax.text((x_edges[0] + x_edges[1]) / 2, yc, chem_label(a),
                    ha='center', va='center')
            for k, col in enumerate(columns):
                xc = (x_edges[k + 1] + x_edges[k + 2]) / 2
                ax.text(xc, yc, _cell(col[i]), ha='center', va='center')
        paths = _save_dual(fig, out_path, formats)

    # CSV(纯文本表头,不带 mathtext;utf-8-sig 供 Excel)
    stem = os.path.splitext(paths[0])[0]
    csv_path = stem + '.csv'
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        plain = ['Adsorbate']
        for sub in subs:
            plain.append(f'{sub} {de_header}')
            if sub in dg:
                plain.append(f'{sub} {dg_header}')
        w.writerow(plain)
        for i, a in enumerate(ads):
            w.writerow([a] + [_cell(col[i]) for col in columns])
    return paths + [csv_path]


# ── 图 3:ΔG 自由能台阶图(对标论文图 3.10 / 3.23c) ─────────────────────────

def free_energy_ladder(paths, out_path, *, step_labels=None,
                       ylabel: str = r'$\Delta G$ (eV)',
                       xlabel: str = 'Reaction coordinate', title: str = '',
                       mark_pds: bool = True, show_ul: bool = False,
                       half: float = 0.35, width: float | None = None,
                       palette: str = 'tol_bright', panel: str = '',
                       formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """自由能台阶图:多体系叠加,实线平台 + 虚线连接,可标决速步/极限电位。

    数据契约:
        paths = [
            {'name': 'CoO',   'G': [0.00, -0.42, -0.81, ...]},   # 各步 ΔG,同一反应坐标
            {'name': 'Co9S8', 'G': [0.00, -0.28, ...]},
        ]
        单体系可直接传一个 dict。各体系步数可不同(取最大者定横轴)。
        step_labels = ['S8', 'Li2S8', ...] 可选,标在横轴(自动化学式下标)。

    参数:
        mark_pds: True 时每个体系的决速步(最大上坡 ΔG)连接线改红色实线并标注数值;
                  全下坡体系(无上坡)不标。
        show_ul:  True 时图例追加极限电位 U_L = -ΔG_max/e(电催化惯例)。
        half:     平台半宽(反应坐标单位)。
        其余参数同 adsorption_bar。

    返回:导出文件绝对路径列表。
    """
    if isinstance(paths, dict):
        paths = [paths]
    systems = []
    for p in paths:
        name, gs = str(p.get('name', '')), [float(g) for g in (p.get('G') or [])]
        if len(gs) < 2:
            raise ValueError(f"体系 '{name}' 的 G 至少需 2 个台阶")
        systems.append((name, gs))
    if not systems:
        raise ValueError('paths 不能为空')
    n = max(len(gs) for _, gs in systems)
    fig_w = width if width is not None else max(SINGLE_COL, 0.52 * n + 1.2)

    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.70)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        all_g = [g for _, gs in systems for g in gs]
        rng = (max(all_g) - min(all_g)) or 1.0
        if min(all_g) < 0 < max(all_g):
            ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.7, ls=(0, (5, 3)), zorder=1)

        for si, (name, gs) in enumerate(systems):
            c = colors[si % len(colors)]
            # 决速步 = 最大上坡步(电化学 PDS 惯例);全下坡则无
            climbs = [gs[i + 1] - gs[i] for i in range(len(gs) - 1)]
            pds = max(range(len(climbs)), key=lambda i: climbs[i]) if climbs else None
            if pds is not None and climbs[pds] <= 0:
                pds = None
            for i, g in enumerate(gs):          # 平台:实线
                ax.plot([i - half, i + half], [g, g], color=c, lw=1.9,
                        solid_capstyle='butt', zorder=3)
            for i in range(len(gs) - 1):        # 连接:虚线;决速步红实线
                x0, x1 = i + half, i + 1 - half
                if mark_pds and i == pds:
                    ax.plot([x0, x1], [gs[i], gs[i + 1]], color=PDS_COLOR,
                            lw=1.6, zorder=4)
                    ax.annotate(f'+{climbs[i]:.2f}',
                                ((x0 + x1) / 2, (gs[i] + gs[i + 1]) / 2),
                                xytext=(3, -1), textcoords='offset points',
                                ha='left', va='top', color=PDS_COLOR,
                                fontsize=7)
                else:
                    ax.plot([x0, x1], [gs[i], gs[i + 1]], color=c, lw=0.9,
                            ls=(0, (4, 2.5)), zorder=2)
            label = chem_label(name) if name else f'path {si + 1}'
            if show_ul and pds is not None:
                label += rf' ($U_\mathrm{{L}}$ = {-climbs[pds]:.2f} V)'
            ax.plot([], [], color=c, lw=1.9, label=label)   # 图例句柄(实线样式)

        ax.set_xlim(-0.55, n - 0.45)
        ax.set_ylim(min(all_g) - rng * 0.10, max(all_g) + rng * 0.16)
        ax.set_ylabel(ylabel)
        ax.set_xlabel(xlabel)
        if step_labels:
            ax.set_xticks(range(min(n, len(step_labels))))
            ax.set_xticklabels([chem_label(s) for s in step_labels[:n]])
            rot = 0 if max(len(str(s)) for s in step_labels[:n]) <= 5 else 30
            if rot:
                for t in ax.get_xticklabels():
                    t.set_rotation(rot)
                    t.set_ha('right')
        else:
            ax.set_xticks([])
        ax.tick_params(axis='x', length=0)
        if title:
            ax.set_title(title)
        if len(systems) > 1 or show_ul:
            ax.legend(loc='best')
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)
