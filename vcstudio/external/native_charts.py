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
- heatmap_matrix:      {'rows': [基底...], 'cols': [吸附质...], 'values': [[eV, ...], ...]}
- volcano_plot:        [{'name': str, 'x': 描述符, 'y': 活性}, ...] + 可选 legs 两条腿
- scaling_relation:    xs, ys 平行数组 + 可选 labels(点名)

架构可扩展到全套 VASP 图(DOS/能带/收敛曲线):新图种 = apply_paper_style()
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


def _luminance(rgba) -> float:
    """相对亮度(WCAG 系数简化版):决定热图格内文字用黑还是白。rgba 分量 ∈ [0,1]。"""
    r, g, b = rgba[0], rgba[1], rgba[2]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


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
                       pds_index=None,
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
        每个体系 dict 可带 'pds_index'(该体系的权威决速步序号,优先级最高)。

    参数:
        mark_pds: True 时每个体系的决速步连接线改红色实线并标注数值;
                  全下坡体系(无上坡)不标。
        pds_index: **权威决速步序号**(int,作用于所有未自带 pds_index 的体系)。
                  各步转移电子数不等时(如 Li-S 末步 8 e⁻),决速步须按**逐电子**
                  ΔG 判定(freeenergy.discharge_path 的 pds_index)——此时必须传入,
                  否则本函数按原始 ΔG 自判会高亮错步(与 U_L 图数不一致)。
                  None 时回落自判(仅逐 1 e⁻ 路径下正确)。
        show_ul:  True 时图例追加极限电位 U_L = -ΔG_max/e(电催化惯例;
                  仅逐 1 e⁻ 路径下与逐电子口径一致)。
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
        systems.append((name, gs, p.get('pds_index', None)))
    if not systems:
        raise ValueError('paths 不能为空')
    n = max(len(gs) for _, gs, _ in systems)
    fig_w = width if width is not None else max(SINGLE_COL, 0.52 * n + 1.2)

    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.70)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        all_g = [g for _, gs, _ in systems for g in gs]
        rng = (max(all_g) - min(all_g)) or 1.0
        if min(all_g) < 0 < max(all_g):
            ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.7, ls=(0, (5, 3)), zorder=1)

        for si, (name, gs, path_pds) in enumerate(systems):
            c = colors[si % len(colors)]
            climbs = [gs[i + 1] - gs[i] for i in range(len(gs) - 1)]
            # 决速步:体系自带 > 全局 pds_index > 按原始 ΔG 自判(仅逐 1e⁻ 路径正确)
            authoritative = path_pds if path_pds is not None else pds_index
            if authoritative is not None:
                pds = int(authoritative)
                if not (0 <= pds < len(climbs)):
                    raise ValueError(
                        f"pds_index={pds} 越界(体系 '{name}' 只有 {len(climbs)} 个连接步)")
            else:
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
                    ax.annotate(f'{climbs[i]:+.2f}',
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


# ── 图 4:吸附能/ΔG 矩阵热图(多催化剂 × 多吸附质横向对比) ────────────────────

def _validate_heatmap(data: dict) -> tuple:
    """校验 {'rows', 'cols', 'values'} 契约,返回 (rows, cols, values)。"""
    rows = list(data.get('rows') or [])
    cols = list(data.get('cols') or [])
    values = list(data.get('values') or [])
    if not rows or not cols:
        raise ValueError("data 需含非空 'rows'(基底)与 'cols'(吸附质)列表")
    if len(values) != len(rows):
        raise ValueError(f"'values' 行数 {len(values)} != rows 个数 {len(rows)}")
    for name, rv in zip(rows, values):
        if len(rv) != len(cols):
            raise ValueError(f"行 '{name}' 的数值个数 {len(rv)} != cols 个数 {len(cols)}")
    if not any(v is not None for rv in values for v in rv):
        raise ValueError("'values' 至少需一个非 None 数值")
    return rows, cols, values


def heatmap_matrix(data: dict, out_path, *, cmap: str = 'RdYlGn_r',
                   annotate: bool = True, fmt: str = '{:.2f}',
                   cbar_label: str = r'$E_\mathrm{ads}$ (eV)',
                   vmin: float | None = None, vmax: float | None = None,
                   title: str = '', width: float | None = None,
                   panel: str = '', formats=('png', 'pdf'),
                   style_kw: dict | None = None) -> list:
    """吸附能/ΔG 矩阵热图:纵轴=催化剂/基底,横轴=吸附质,格内标数值 + 色条。

    数据契约:
        data = {
            'rows':   ['CoO', 'Co9S8', 'Graphene', ...],        # 基底(热图自上而下)
            'cols':   ['S8', 'Li2S8', 'Li2S6', ...],            # 吸附质(自左向右)
            'values': [[-0.82, -1.61, ...],                     # 每行一个基底,按 cols 序
                       [-0.85, -1.20, ...], ...],               # 缺值用 None → 空白格
        }
        行列名走 chem_label 自动化学式下标;values 行数=len(rows)、列数=len(cols)。

    参数:
        out_path:   输出路径(扩展名可省;按 formats 导出多份,默认 PNG+PDF)。
        cmap:       matplotlib 色图名。默认 'RdYlGn_r'(越负=吸附越强=绿);红绿色弱
                    读者建议 'RdYlBu_r' 或 'viridis'——格内数值标注(annotate)保证
                    任何色图下信息不丢失。
        annotate:   True 时每格标数值(文字黑/白按底色亮度自动反转)。
        fmt:        格内/示例数值格式。
        cbar_label: 色条标签(mathtext 可用);'' 则不标。
        vmin/vmax:  色标范围;None 按数据自适应(忽略 None 缺值)。
        width:      图宽英寸;None 按行列数自适应(≥单栏 89 mm)。
        panel:      非空则加多面板标号(如 'a')。
        style_kw:   透传 apply_paper_style 的额外风格参数(font/base_size/box)。

    返回:导出文件绝对路径列表(与 formats 同序)。
    """
    import numpy as np
    from matplotlib import colormaps
    rows, cols, values = _validate_heatmap(data)
    nr, nc = len(rows), len(cols)
    arr = np.array([[_as_float(v) for v in rv] for rv in values], dtype=float)
    masked = np.ma.masked_invalid(arr)
    cm = colormaps[cmap].with_extremes(bad='#FFFFFF')      # None → 空白格

    max_row_chars = max(len(str(r)) for r in rows)
    fig_w = width if width is not None else max(
        SINGLE_COL, 0.56 * nc + 0.115 * max_row_chars + 1.15)
    fig_h = 0.34 * nr + 0.62 + (0.28 if title else 0.0)

    with apply_paper_style(**(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=fig_h / fig_w)
        im = ax.imshow(masked, cmap=cm, vmin=vmin, vmax=vmax,
                       aspect='auto', interpolation='nearest', zorder=2)
        # 白色内部格线分隔单元格(只画内线,外缘由图像边界收齐;缺值格融为空白)
        for i in range(1, nr):
            ax.axhline(i - 0.5, color='white', lw=1.1, zorder=3)
        for j in range(1, nc):
            ax.axvline(j - 0.5, color='white', lw=1.1, zorder=3)
        for spine in ax.spines.values():
            spine.set_visible(False)

        if annotate:
            for i in range(nr):
                for j in range(nc):
                    v = arr[i, j]
                    if v != v:                              # NaN(缺值)不标
                        continue
                    lum = _luminance(im.cmap(im.norm(v)))
                    ax.text(j, i, fmt.format(v), ha='center', va='center',
                            fontsize=max(5.5, 7.8 - 0.10 * nc),
                            color='white' if lum < 0.45 else 'black', zorder=4)

        ax.set_xticks(range(nc))
        ax.set_xticklabels([chem_label(c) for c in cols])
        if max(len(str(c)) for c in cols) > 5:
            for t in ax.get_xticklabels():
                t.set_rotation(30)
                t.set_ha('right')
        ax.set_yticks(range(nr))
        ax.set_yticklabels([chem_label(r) for r in rows])
        ax.tick_params(length=0)                            # 类目轴不需要刻度线
        if title:
            ax.set_title(title)

        cb = fig.colorbar(im, ax=ax, fraction=0.6 / fig_w, pad=0.03)
        cb.outline.set_linewidth(0.9)
        cb.ax.tick_params(width=0.9, length=2.6)
        if cbar_label:
            cb.set_label(cbar_label)
        if panel:
            add_panel_label(ax, panel, dx=-0.10 - 0.017 * max_row_chars)
        return _save_dual(fig, out_path, formats)


# ── 图 5:火山图(Sabatier 活性 vs 吸附描述符) ────────────────────────────────

def volcano_plot(points, out_path, *, descriptor_label: str, activity_label: str,
                 legs=None, mark_top: bool = True,
                 top_label: str = 'Sabatier optimum',
                 side_labels: tuple | None = None, annotate_points: bool = True,
                 value_fmt: str = '{:.2f}', title: str = '',
                 width: float | None = None, palette: str = 'tol_bright',
                 panel: str = '', formats=('png', 'pdf'),
                 style_kw: dict | None = None) -> list:
    """火山图:活性(极限电位/负过电位)vs 吸附描述符,可画双腿并自动标峰顶。

    数据契约:
        points = [{'name': 'Fe@N4', 'x': -2.31, 'y': -0.55}, ...]
            x = 描述符(如吸附能/ΔG),y = 活性(如 U_L;取"越大越好"的号规)。
            'name' 可省(该点不标名);催化剂名走 chem_label 自动下标。
        legs = [{'slope': k1, 'intercept': b1, 'label': '...'},   # 可选,恰好两条时
                {'slope': k2, 'intercept': b2, 'label': '...'}]   # 取下包络画 Λ 形火山
            两腿斜率不同则自动求交点 = Sabatier 峰顶(mark_top=True 时标星+顶标签)。
            腿数 ≠2(或两腿平行)时各腿按全区间直线画、不求峰顶。

    参数:
        descriptor_label: 横轴标签(必填,如 r'$-\\Delta G_\\mathrm{ads}$(*LiS$_2$) (eV)')。
        activity_label:   纵轴标签(必填,如 r'$U_\\mathrm{L}$ (V)')。
        mark_top:   True 时标峰顶:有双腿→交点(金星+垂线+两行顶标签);无 legs 且
                    ≥3 点→纯 numpy 二次拟合引导线(开口向下才画,虚线),顶点在视野
                    内画星+垂线但不标字(避免与点名注释冲突)。
        top_label:  峰顶注释文本('' 只画星不注字;仅双腿交点场景显示)。
        side_labels: (左侧文本, 右侧文本) 标注弱/强吸附侧,如 ('Weak adsorption',
                    'Strong adsorption');None 不标。方向取决于描述符号规,由调用者定。
        annotate_points: True 时散点旁标催化剂名。
        value_fmt:  峰顶注释里 x* 坐标的数值格式(top_label 后括注);'' 只标文本。
        其余参数同 adsorption_bar。

    返回:导出文件绝对路径列表(与 formats 同序)。
    """
    pts = [points] if isinstance(points, dict) else list(points)
    if not pts:
        raise ValueError('points 不能为空')
    names, xs, ys = [], [], []
    for p in pts:
        if 'x' not in p or 'y' not in p:
            raise ValueError(f"散点 {p.get('name', p)!r} 需含 'x' 与 'y'")
        names.append(str(p.get('name', '') or ''))
        xs.append(float(p['x']))
        ys.append(float(p['y']))
    legs = list(legs or [])
    for leg in legs:
        if 'slope' not in leg or 'intercept' not in leg:
            raise ValueError("每条腿需含 'slope' 与 'intercept'")

    # ── 几何计算(先算范围/交点,再进风格上下文作图) ──
    xspan = (max(xs) - min(xs)) or 1.0
    xlo, xhi = min(xs) - 0.12 * xspan, max(xs) + 0.12 * xspan
    apex = None
    if len(legs) == 2 and float(legs[0]['slope']) != float(legs[1]['slope']):
        k1, b1 = float(legs[0]['slope']), float(legs[0]['intercept'])
        k2, b2 = float(legs[1]['slope']), float(legs[1]['intercept'])
        xa = (b2 - b1) / (k1 - k2)
        apex = (xa, k1 * xa + b1)
        xlo, xhi = min(xlo, xa - 0.10 * xspan), max(xhi, xa + 0.10 * xspan)

    y_all = list(ys)
    if apex is not None:
        y_all.append(apex[1])
    for leg in legs:
        k, b = float(leg['slope']), float(leg['intercept'])
        y_all += ([min(k * xlo + b, apex[1]), min(k * xhi + b, apex[1])]
                  if apex is not None else [k * xlo + b, k * xhi + b])

    fit_curve = None                     # 无腿时的可选二次拟合:(xx, yy, 顶点或 None)
    if mark_top and not legs and len(xs) >= 3:
        import numpy as np
        a2, a1, a0 = np.polyfit(xs, ys, 2)
        if a2 < 0:                       # 开口向下才有"峰"可言
            xx = np.linspace(xlo, xhi, 200)
            yy = a2 * xx * xx + a1 * xx + a0
            xv = -a1 / (2 * a2)
            vertex = (xv, a2 * xv * xv + a1 * xv + a0) if xlo <= xv <= xhi else None
            fit_curve = (xx, yy, vertex)
            y_all += [float(yy.min()), float(yy.max())]

    yspan = (max(y_all) - min(y_all)) or 1.0
    head = 0.26 if (mark_top and apex is not None and top_label) else 0.12
    ylo, yhi = min(y_all) - 0.10 * yspan, max(y_all) + head * yspan
    fig_w = width if width is not None else SINGLE_COL

    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.80)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        ax.set_xlim(xlo, xhi)
        ax.set_ylim(ylo, yhi)

        if apex is not None:             # 恰两条腿:下包络 Λ 形(左腿=xlo 处更低的那条)
            xa = apex[0]
            left = 0 if (k1 * xlo + b1) <= (k2 * xlo + b2) else 1
            for idx, (x0, x1) in ((left, (xlo, xa)), (1 - left, (xa, xhi))):
                k = float(legs[idx]['slope'])
                b = float(legs[idx]['intercept'])
                lbl = legs[idx].get('label')
                ax.plot([x0, x1], [k * x0 + b, k * x1 + b],
                        color=colors[(idx + 1) % len(colors)], lw=1.6, zorder=2,
                        solid_capstyle='round',
                        label=chem_label(lbl) if lbl else None)
        else:                            # 0/1/≥3 条或平行:各腿全区间直线
            for idx, leg in enumerate(legs):
                k, b = float(leg['slope']), float(leg['intercept'])
                lbl = leg.get('label')
                ax.plot([xlo, xhi], [k * xlo + b, k * xhi + b],
                        color=colors[(idx + 1) % len(colors)], lw=1.6, zorder=2,
                        label=chem_label(lbl) if lbl else None)

        if fit_curve is not None:        # 无腿:二次拟合引导线(虚线,不进图例)
            xx, yy, vertex = fit_curve
            ax.plot(xx, yy, color='#888888', lw=1.1, ls=(0, (5, 3)), zorder=2)
        else:
            vertex = None

        top = apex if (mark_top and apex is not None) else vertex
        if top is not None:              # Sabatier 峰顶:金星 + 垂线 + 顶标签
            ax.plot([top[0], top[0]], [ylo, top[1]], color=ZERO_LINE_COLOR,
                    lw=0.7, ls=(0, (4, 3)), zorder=1)
            ax.plot([top[0]], [top[1]], marker='*', ms=11, color='#DDAA33',
                    mec='black', mew=0.6, ls='none', zorder=5)
            # 只有双腿交点标文字(两行居中);拟合顶点只画星,避免与点名撞字
            if top_label and apex is not None:
                txt = (f'{top_label}\n({value_fmt.format(top[0])})'
                       if value_fmt else str(top_label))
                ax.annotate(txt, top, xytext=(0, 7), textcoords='offset points',
                            ha='center', va='bottom', fontsize=7.5,
                            linespacing=1.3, zorder=5)

        ax.scatter(xs, ys, s=30, color=colors[0], edgecolors='black',
                   linewidths=0.5, zorder=4)
        if annotate_points:
            for x, y, nm in zip(xs, ys, names):
                if nm:
                    ax.annotate(chem_label(nm), (x, y), xytext=(4, 3),
                                textcoords='offset points', ha='left',
                                va='bottom', fontsize=7.5, zorder=5)
        if side_labels:
            left_txt, right_txt = side_labels
            ax.text(0.02, 0.03, str(left_txt), transform=ax.transAxes,
                    ha='left', va='bottom', fontsize=7.5,
                    fontstyle='italic', color='#555555')
            ax.text(0.98, 0.03, str(right_txt), transform=ax.transAxes,
                    ha='right', va='bottom', fontsize=7.5,
                    fontstyle='italic', color='#555555')

        ax.set_xlabel(descriptor_label)
        ax.set_ylabel(activity_label)
        if title:
            ax.set_title(title)
        if any(leg.get('label') for leg in legs):
            # 峰顶注释占顶部中央 → legend 避让到离峰顶较远的一侧上角(腿低处上方必空)
            if mark_top and apex is not None and top_label:
                loc = ('upper right' if (apex[0] - xlo) <= (xhi - apex[0])
                       else 'upper left')
            else:
                loc = 'best'
            ax.legend(loc=loc, fontsize=7, handlelength=1.1,
                      labelspacing=0.35, borderaxespad=0.25)
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)


# ── 图 6:标度关系(scaling relation)散点 + 线性拟合 ─────────────────────────

def scaling_relation(xs, ys, out_path, *, xlabel: str, ylabel: str,
                     labels=None, fit: bool = True, fit_fmt: str = '{:.2f}',
                     title: str = '', width: float | None = None,
                     palette: str = 'tol_bright', panel: str = '',
                     formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """标度关系图:两个吸附量的散点 + 最小二乘直线,标拟合式与 R²(纯 numpy)。

    数据契约:
        xs, ys: 等长数值序列(≥2 个点),如各基底的 E_ads(Li2S6) 与 E_ads(Li2S4)。
        labels: 可选,与 xs 等长的点名列表(如基底名,走 chem_label 自动下标);
                None 不标点名。

    参数:
        xlabel/ylabel: 轴标签(必填,mathtext 可用)。
        fit:      True 画最小二乘直线并标 'y = kx + b' 与 R²(k>0 标注在左上,
                  k<0 在右上,避开数据云);要求 xs 至少有 2 个不同值。
        fit_fmt:  拟合式中 k/b 的数值格式(R² 固定 3 位小数)。
        其余参数同 adsorption_bar。

    返回:导出文件绝对路径列表(与 formats 同序)。
    """
    xs = [float(x) for x in xs]
    ys = [float(y) for y in ys]
    if len(xs) != len(ys):
        raise ValueError(f'xs 与 ys 长度不一致: {len(xs)} != {len(ys)}')
    if len(xs) < 2:
        raise ValueError('标度关系至少需 2 个点')
    if labels is not None and len(labels) != len(xs):
        raise ValueError(f'labels 个数 {len(labels)} != 点数 {len(xs)}')
    if fit and len(set(xs)) < 2:
        raise ValueError('线性拟合要求 xs 至少含 2 个不同值')

    fig_w = width if width is not None else SINGLE_COL
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.80)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        ax.scatter(xs, ys, s=30, color=colors[0], edgecolors='black',
                   linewidths=0.5, zorder=3)
        if labels:
            for x, y, nm in zip(xs, ys, labels):
                if nm:
                    ax.annotate(chem_label(nm), (x, y), xytext=(4, 3),
                                textcoords='offset points', ha='left',
                                va='bottom', fontsize=7.5, zorder=4)
        if fit:
            import numpy as np
            k, b = (float(c) for c in np.polyfit(xs, ys, 1))
            yhat = [k * x + b for x in xs]
            ybar = sum(ys) / len(ys)
            ss_res = sum((y - h) ** 2 for y, h in zip(ys, yhat))
            ss_tot = sum((y - ybar) ** 2 for y in ys)
            r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else (
                1.0 if ss_res < 1e-12 else 0.0)
            pad = 0.06 * ((max(xs) - min(xs)) or 1.0)
            xx = (min(xs) - pad, max(xs) + pad)
            ax.plot(xx, [k * x + b for x in xx], color=colors[1],
                    lw=1.4, zorder=2)
            sign = '+' if b >= 0 else '-'
            eq = (rf'$y = {fit_fmt.format(k)}x \,{sign}\, {fit_fmt.format(abs(b))}$'
                  '\n' rf'$R^2 = {r2:.3f}$')
            if k >= 0:                   # 正相关点云在对角线上,拟合式放左上角
                ax.text(0.05, 0.95, eq, transform=ax.transAxes,
                        ha='left', va='top', linespacing=1.5)
            else:
                ax.text(0.95, 0.95, eq, transform=ax.transAxes,
                        ha='right', va='top', linespacing=1.5)
        ax.margins(x=0.10, y=0.12)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        if title:
            ax.set_title(title)
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)


# ── 图 7:投影 DOS(PDOS,F19;自旋镜像 + 费米零点 + d 带中心竖线)────────────────

def pdos_plot(series, out_path, *, efermi: float = 0.0, band_centers=None,
              mirror_spin: bool = True, xlim=(-8, 4), title: str = '',
              ylabel: str | None = None, width: float | None = None,
              palette: str = 'tol_bright', panel: str = '',
              formats=('png', 'pdf'), style_kw: dict | None = None) -> list:
    """投影态密度图:多条投影曲线叠加,自旋向下镜像至 y<0,标费米零点与 d 带中心。

    数据契约:
        series = [
            {'label': 'Co 3d', 'energies': [...], 'dos_up': [...],
             'dos_down': [...],   # 可选;无则该曲线单自旋
             'color': '#EE6677'}, # 可选;缺省走 palette 色循环
            {'label': 'O 2p', 'energies': [...], 'dos_up': [...]},
        ]
        energies 为**绝对**能量(eV);本函数按 efermi 平移到 E − E_F(费米落在 x=0)。

    参数:
        efermi:    费米能级(eV),用于把横轴平移到相对费米能级。
        band_centers: {'label': ε_d} → 在 x=ε_d(**已相对 E_F**,取自 band_center 的
                   center_eV)画红色竖线并标注;None 不标。
        mirror_spin: True 时 dos_down 取负画在 y=0 下方(PDOS 自旋镜像惯例)。
        xlim:      横轴范围(相对 E_F,eV)。其余参数同 adsorption_bar。

    返回:导出文件绝对路径列表(与 formats 同序)。series 空/字段缺失/长度不一致 → ValueError。
    """
    if not series:
        raise ValueError('series 不能为空')
    norm = []
    for s in series:
        if 'energies' not in s or 'dos_up' not in s:
            raise ValueError("每条 series 需含 'energies' 与 'dos_up'")
        e = [float(x) for x in s['energies']]
        up = [float(x) for x in s['dos_up']]
        if len(e) != len(up):
            raise ValueError(f"series {s.get('label', '?')!r} 的 energies 与 dos_up 长度不一致")
        dn = s.get('dos_down')
        if dn is not None:
            dn = [float(x) for x in dn]
            if len(dn) != len(e):
                raise ValueError(f"series {s.get('label', '?')!r} 的 dos_down 长度不一致")
        norm.append((s.get('label', ''), e, up, dn, s.get('color')))

    fig_w = width if width is not None else SINGLE_COL
    has_down = any(t[3] is not None for t in norm)
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.80)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        for i, (label, e, up, dn, color) in enumerate(norm):
            c = color or colors[i % len(colors)]
            x = [ee - efermi for ee in e]
            ax.plot(x, up, color=c, lw=1.3, zorder=3,
                    label=chem_label(label) if label else None)
            if dn is not None:
                ydn = [-v for v in dn] if mirror_spin else dn
                ax.plot(x, ydn, color=c, lw=1.3, zorder=3)
        if mirror_spin and has_down:
            ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.7, zorder=1)
        # 费米零点(横轴已平移,E_F 落在 x=0)
        ax.axvline(0.0, color=ZERO_LINE_COLOR, lw=0.9, ls=(0, (5, 3)), zorder=2)
        ax.annotate(r'$E_\mathrm{F}$', (0.0, 1.0), xycoords=('data', 'axes fraction'),
                    xytext=(2, -2), textcoords='offset points', ha='left', va='top',
                    fontsize=7.5, color=ZERO_LINE_COLOR)
        if band_centers:
            for lbl, eps in band_centers.items():
                if eps is None:
                    continue
                ax.axvline(float(eps), color=PDS_COLOR, lw=1.0, ls=(0, (2, 2)), zorder=4)
                ax.annotate(rf'{chem_label(lbl)} $\varepsilon_d$={float(eps):.2f}',
                            (float(eps), 1.0), xycoords=('data', 'axes fraction'),
                            xytext=(2, -11), textcoords='offset points',
                            ha='left', va='top', fontsize=7, color=PDS_COLOR)
        ax.set_xlim(*xlim)
        ax.set_xlabel(r'$E - E_\mathrm{F}$ (eV)')
        ax.set_ylabel(ylabel if ylabel is not None else 'PDOS (states/eV)')
        if title:
            ax.set_title(title)
        if any(t[0] for t in norm):
            ax.legend(loc='best')
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)


# ── 图 8:面平均差分电荷 Δρ(z) 曲线(F21 的定量补充)────────────────────────────

def charge_profile_plot(z, rho, out_path, *, regions=None, title: str = '',
                        xlabel: str | None = None, ylabel: str | None = None,
                        width: float | None = None, palette: str = 'tol_bright',
                        panel: str = '', formats=('png', 'pdf'),
                        style_kw: dict | None = None) -> list:
    """面平均差分电荷 Δρ̄(z) 曲线:聚集(>0)/耗散(<0)分色填充,可标区间底色。

    数据契约:
        z, rho: 等长数值序列(≥2 点),取自 chgdiff.plane_averaged 的 'z'/'rho'。
        regions = [{'z0': 3.0, 'z1': 8.0, 'label': 'slab', 'color': '#EEE'}, ...]
            可选:标出表面/吸附质区间底色(axvspan);label 进图例,color 缺省浅灰。

    返回:导出文件绝对路径列表。长度不一致/点数<2 → ValueError。
    """
    z = [float(v) for v in z]
    rho = [float(v) for v in rho]
    if len(z) != len(rho):
        raise ValueError(f'z 与 rho 长度不一致:{len(z)} != {len(rho)}')
    if len(z) < 2:
        raise ValueError('面平均曲线至少需 2 个点')

    fig_w = width if width is not None else SINGLE_COL
    with apply_paper_style(palette=palette, **(style_kw or {})):
        fig, ax = _new_figure(width=fig_w, aspect=0.62)
        colors = PALETTES.get(palette, PALETTES['tol_bright'])
        if regions:
            for reg in regions:
                ax.axvspan(float(reg['z0']), float(reg['z1']),
                           color=reg.get('color', '#E9E9E9'), alpha=0.6, zorder=0,
                           label=chem_label(reg['label']) if reg.get('label') else None)
        ax.axhline(0.0, color=ZERO_LINE_COLOR, lw=0.8, zorder=1)
        pos = [r if r > 0 else 0.0 for r in rho]
        neg = [r if r < 0 else 0.0 for r in rho]
        ax.fill_between(z, pos, 0.0, color='#EE6677', alpha=0.35, linewidth=0, zorder=2)
        ax.fill_between(z, neg, 0.0, color='#4477AA', alpha=0.35, linewidth=0, zorder=2)
        ax.plot(z, rho, color=colors[0], lw=1.4, zorder=3)
        ax.set_xlim(min(z), max(z))
        ax.set_xlabel(xlabel if xlabel is not None else r'$z$ (Å)')
        ax.set_ylabel(ylabel if ylabel is not None
                      else r'$\Delta\bar\rho$ (e/Å$^{3}$)')
        if title:
            ax.set_title(title)
        if regions and any(r.get('label') for r in regions):
            ax.legend(loc='best', fontsize=7)
        if panel:
            add_panel_label(ax, panel)
        return _save_dual(fig, out_path, formats)
