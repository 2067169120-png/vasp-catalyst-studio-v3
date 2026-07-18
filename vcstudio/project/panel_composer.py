"""多面板拼版器:把多张单图 PNG 拼成一张投稿级组合图(a/b/c/d 粗体标号)。

定位(2026-07):一键出图管线的终点常常是"一整版组合图"——Nature 系正文图多为
2×2 / 蜂窝布局的多面板。本模块读各子图 PNG,按网格拼成一张图,统一到目标栏宽、
DPI≥300,逐面板加 a/b/c/d 粗体标号(与 native_charts.add_panel_label 同口径),
导出 PNG+PDF。纯离屏(Figure+FigureCanvasAgg,不碰 pyplot 全局态),CI 无显示可跑。

蜂窝布局:当末行面板数不足整列(如 3 图 → 2+1),末行自动居中,避免右下留大空洞。

依赖策略:matplotlib 属可选分析层依赖,全部延迟到函数内 import;顶层零重依赖。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os

# 期刊目标栏宽(英寸),与 native_charts 同源:单栏 89 mm、双栏 183 mm
_WIDTH_IN = {'single': 3.50, 'onehalf': 4.72, 'double': 7.20}
_JOURNAL_WIDTH = {           # 各期刊 (单栏, 双栏) 英寸
    'nature': (3.50, 7.20),
    'acs': (3.25, 7.00),
    'prb': (3.375, 6.75),
    'science': (3.42, 7.00),
}


def suggest_layout(n: int) -> tuple:
    """面板数 → (rows, cols) 近方形布局(蜂窝:末行不足自动居中,由 compose 处理)。

    1→(1,1)、2→(1,2)、3→(2,2)、4→(2,2)、5/6→(2,3)、7/8/9→(3,3)……
    列数取 ⌈√n⌉,行数取 ⌈n/列⌉(整体尽量方,横向略宽,契合正文双栏排版)。
    """
    n = int(n)
    if n <= 0:
        raise ValueError('面板数须为正整数')
    cols = int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))
    return rows, cols


def _target_width(journal: str, width) -> float:
    """(journal, width) → 目标图宽英寸。width 可为 'single'/'double'/'onehalf' 或数字。"""
    if isinstance(width, (int, float)) and not isinstance(width, bool):
        return float(width)
    key = str(width or 'double').lower()
    single, double = _JOURNAL_WIDTH.get(str(journal).lower(), _JOURNAL_WIDTH['nature'])
    if key in ('single', 'one', '1'):
        return single
    if key in ('onehalf', '1.5', 'oneandhalf'):
        return _WIDTH_IN['onehalf']
    return double


def _load_image(panel: dict):
    """面板项 → (RGBA ndarray, 宽像素, 高像素)。支持 {'file': png} 或 {'image': ndarray}。"""
    import numpy as np
    from matplotlib import image as mimage
    if panel.get('image') is not None:
        arr = np.asarray(panel['image'])
    else:
        src = panel.get('file')
        if not src or not os.path.isfile(str(src)):
            raise ValueError(f'面板源图不存在:{src!r}(compose 需要已渲染的 PNG 路径)')
        arr = mimage.imread(str(src))               # HxWx(3/4),float[0,1] 或 uint8
    h, w = arr.shape[0], arr.shape[1]
    return arr, int(w), int(h)


def _labels_for(panels: list) -> list:
    """逐面板标号:优先用面板自带 'label',否则按 a/b/c/… 顺次补齐。"""
    out = []
    for i, p in enumerate(panels):
        lab = p.get('label')
        out.append(str(lab) if lab else chr(ord('a') + i))
    return out


def compose(panels, out_path, *, cols: int = 2, journal: str = 'nature',
            width='double', label: bool = True, wspace: float = 0.06,
            hspace: float = 0.08, formats=('png', 'pdf'), dpi: int = 300) -> list:
    """把多张子图 PNG 拼成一张多面板组合图(网格 + a/b/c/d 粗体标号),导出 PNG+PDF。

    参数:
        panels:  [{'file': png 路径, 'label': 'a'(可选)}, ...];也支持 {'image': ndarray}。
                 label 缺省按 a/b/c… 自动补;顺序即阅读序(逐行铺)。
        out_path: 输出路径(扩展名可省;按 formats 导出)。
        cols:    每行面板数(默认 2);末行不足自动居中(蜂窝布局)。
        journal: 期刊预设(nature/acs/prb/science),决定目标栏宽。
        width:   'single'/'double'/'onehalf' 或数字英寸;决定组合图总宽(DPI≥300)。
        label:   是否加 a/b/c/d 粗体标号(左上角外侧,Nature 惯例小写无括号)。

    返回:导出文件绝对路径列表(与 formats 同序)。panels 为空 / 源图缺失 → ValueError。
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    from vcstudio.external.native_charts import add_panel_label

    panels = list(panels or [])
    if not panels:
        raise ValueError('panels 不能为空(至少需一张子图)')
    cols = max(1, int(cols))
    n = len(panels)
    rows = int(math.ceil(n / cols))

    imgs = [_load_image(p) for p in panels]
    labels = _labels_for(panels)

    # 单元格宽高比:取各图纵横比中位数,保证每格不拉伸变形
    aspects = sorted(h / w for _, w, h in imgs)
    cell_aspect = aspects[len(aspects) // 2]

    fig_w = _target_width(journal, width)
    dpi = max(300, int(dpi))                          # 投稿硬底线 300 dpi

    # 网格几何(figure 分数坐标):留出四周留白,格间距 wspace/hspace
    left, right, bottom, top = 0.02, 0.995, 0.01, 0.985
    grid_w = right - left
    grid_h = top - bottom
    cell_w = (grid_w - (cols - 1) * wspace) / cols
    cell_h = (grid_h - (rows - 1) * hspace) / rows
    # 图高:由单元格纵横比 + 行列数反推,使每格图不变形
    fig_h = fig_w * (cell_w * cell_aspect * rows + hspace * (rows - 1)) / \
        (cell_w * cols + wspace * (cols - 1)) if cols else fig_w
    fig_h = max(fig_h, fig_w * 0.4)

    fig = Figure(figsize=(fig_w, fig_h))
    FigureCanvasAgg(fig)

    # 逐行铺;末行不足 cols 时整行水平居中(蜂窝)
    idx = 0
    for r in range(rows):
        in_row = min(cols, n - r * cols)
        row_total_w = in_row * cell_w + (in_row - 1) * wspace
        row_left = left + (grid_w - row_total_w) / 2.0        # 居中偏移
        y0 = top - (r + 1) * cell_h - r * hspace
        for c in range(in_row):
            arr, w, h = imgs[idx]
            x0 = row_left + c * (cell_w + wspace)
            ax = fig.add_axes([x0, y0, cell_w, cell_h])
            ax.imshow(arr, aspect='auto', interpolation='none')
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)
            if label:
                add_panel_label(ax, labels[idx], dx=-0.02, dy=1.0)
            idx += 1

    stem, ext = os.path.splitext(os.path.abspath(str(out_path)))
    if ext.lower().lstrip('.') not in ('png', 'pdf', 'svg', 'eps', ''):
        stem = stem + ext
    os.makedirs(os.path.dirname(stem) or '.', exist_ok=True)
    paths = []
    for fmt in formats:
        p = f'{stem}.{fmt}'
        fig.savefig(p, format=fmt, dpi=dpi, bbox_inches='tight', pad_inches=0.02)
        paths.append(p)
    return paths
