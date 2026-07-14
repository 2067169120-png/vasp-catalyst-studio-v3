# -*- coding: utf-8 -*-
r"""Word 报告生成器：老版格式 + 新版优化图 + 论文风格分子模型图。

- 文档骨架移植 E:\V2.0.0\layer2_science\report.py 的 WordReport
  (封面→计算结果概要→吸附能矩阵→图表分析→结果分析与讨论→计算方法)。
- 数据图直接复用新版 Origin 出图 (results/imported/<组>/report_figs/{ads_bar,fed}.png,
  2400px, X 轴剥公共前缀 + FED 对齐张洪毅图4.3 惯例)。
- 分子模型图按张洪毅硕论 图4.1/4.2 风格重渲染: 球棍模型、白底、
  C=棕 / N=浅灰紫 / S=亮黄 / Li=浅绿 / 金属=宝蓝 (双金属第二色橙红),
  每物种 侧视+俯视, 图注标 d(S–金属) 键长; CONTCAR 取自 job.yaml 的 legacy_source。
- 只处理全算完的组 (7/7: 裸载体 + 6 物种全 DONE); 覆盖老版报告
  E:\V2.0.0\results\reports\<组>\report_<组>.docx, 同时在
  results/imported/<组>/ 落一份 report_<组>.docx。

依赖系统 Python: python-docx, ase, matplotlib, numpy, PyYAML; POV-Ray 可选
(缺失回退 ASE plot_atoms)。
用法: python tools/generate_word_report.py [组名...]  (缺省 Co V Zn-Ta)
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import yaml
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from matplotlib.lines import Line2D
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from PIL import Image
from ase.io import read as ase_read
from ase.io.pov import write_pov, get_bondpairs
from ase.data import covalent_radii
from ase.data.colors import jmol_colors

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

REPO = Path(__file__).resolve().parents[1]
IMPORTED = REPO / 'results' / 'imported'
MOLECULES_DIR = Path(r'E:\V2.0.0\results\done\lis_results')
OLD_REPORTS = Path(r'E:\V2.0.0\results\reports')
DEFAULT_GROUPS = ['Co', 'V', 'Zn-Ta']
SPECIES_ORDER = ['S8', 'Li2S8', 'Li2S6', 'Li2S4', 'Li2S2', 'Li2S']

BODY_FONT = 'SimSun'
HEADING_FONT = 'SimSun'

# ── 张洪毅论文配色 (图4.1/4.2): C 棕骨架 / N 浅灰紫 / S 亮黄 / Li 浅绿 /
#    单原子金属宝蓝; 双金属体系第二金属用论文图3.7 Co/Ni 行的橙红。──────────
THESIS_HEX = {
    'C':  '#8f5b2c',   # 棕/赭 — 石墨烯碳骨架
    'N':  '#c6cbe9',   # 浅灰紫 — 配位氮
    'S':  '#ffe100',   # 亮黄 — 硫
    'Li': '#7ec96a',   # 浅绿 — 锂
    'B':  '#e64fa8',   # 品红 — 硼掺杂(论文图4.1)
    'P':  '#f0956d',   # 鲑橙 — 磷掺杂(论文图3.1)
    'H':  '#f2f2f2',
}
METAL_PRIMARY = '#2b63c9'    # 宝蓝 — 论文单原子金属色
METAL_SECONDARY = '#d8552b'  # 橙红 — 双金属第二元素(论文图3.7 Co/Ni 行)
NONMETALS = set(THESIS_HEX)

_UNICODE_SUB = str.maketrans('0123456789', '₀₁₂₃₄₅₆₇₈₉')


def chem_text(species):
    """'Li2S8' -> 'Li₂S₈' (Word 文本用 Unicode 下标)。"""
    return species.translate(_UNICODE_SUB)


def chem_math(species):
    """'Li2S8' -> 'Li$_2$S$_8$' (matplotlib mathtext, 图内标签用)。"""
    return re.sub(r'(\d+)', r'$_\1$', species)


def group_metals(group):
    """'Zn-Ta' -> ['Zn','Ta']; 'Co' -> ['Co']。"""
    return [m for m in group.split('-') if m]


def palette_for(group):
    """组 → {元素: (r,g,b) 0-1}。金属按组内顺序 蓝/橙红 分配。"""
    pal = {}
    for el, h in THESIS_HEX.items():
        pal[el] = tuple(int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    metal_hex = [METAL_PRIMARY, METAL_SECONDARY]
    for i, m in enumerate(group_metals(group)):
        h = metal_hex[min(i, 1)]
        pal[m] = tuple(int(h[i2:i2 + 2], 16) / 255.0 for i2 in (1, 3, 5))
    return pal


# ═══ POV-Ray 渲染 (移植原版 povray_render/structure_render, 换论文配色) ═══════
_POV_PATHS = [
    r'C:\Program Files\POV-Ray\v3.7\bin\pvengine64.exe',
    r'C:\Program Files (x86)\POV-Ray\v3.7\bin\pvengine64.exe',
]


def find_povray():
    import shutil
    for c in _POV_PATHS:
        if os.path.isfile(c):
            return c
    for name in ('pvengine64', 'pvengine', 'povray'):
        p = shutil.which(name)
        if p:
            return p
    return None


def _intracell_bonds(bondpairs):
    """剔除跨周期边界的悬空键(与原版 filter_intracell_bonds 一致)。"""
    return [bp for bp in bondpairs if all(int(o) == 0 for o in bp[2])]


def _render_pov(atoms, pov_path, colors, rotation='-90x', timeout=240):
    exe = find_povray()
    if exe is None:
        return None
    pov_path = Path(pov_path)
    radii = [covalent_radii[n] * 0.40 for n in atoms.numbers]
    settings = dict(
        canvas_width=2000, camera_type='perspective',
        bondatoms=_intracell_bonds(get_bondpairs(atoms, radius=1.1)),
        textures=['intermediate'] * len(atoms), background='White',
        area_light=[(2., 3., 40.), 'White', .7, .7, 3, 3])
    write_pov(pov_path, atoms, rotation=rotation, radii=radii, colors=colors,
              show_unit_cell=0, povray_settings=settings)
    ini = pov_path.with_suffix('.ini')
    with open(ini, 'a') as f:
        f.write('\nAntialias=true\nSampling_Method=2\n'
                'Antialias_Threshold=0.1\nAntialias_Depth=3\n')
    subprocess.run([exe, '/RENDER', str(ini.absolute()), '/EXIT', '/NORESTORE'],
                   capture_output=True, text=True, timeout=timeout,
                   stdin=subprocess.DEVNULL, cwd=str(pov_path.parent))
    png = pov_path.with_suffix('.png').absolute()
    return str(png) if png.is_file() and png.stat().st_size > 0 else None


def _compose(png_in, png_out, symbols, pal, label=None):
    """顶部标题条 + 底部元素图例条(独立条带不压结构, 原版 _add_legend 布局)。"""
    img = mpimg.imread(png_in)
    h, w = img.shape[:2]
    top = int(h * 0.085) if label else int(h * 0.012)
    bot = int(h * 0.095)
    H = h + top + bot
    fig, ax = plt.subplots(figsize=(w / 220, H / 220), dpi=220)
    ax.imshow(img, extent=[0, w, bot, bot + h])
    ax.set_xlim(0, w); ax.set_ylim(0, H)
    ax.axis('off')
    if label:
        ax.text(w / 2, bot + h + top * 0.5, label, ha='center', va='center',
                fontsize=15, fontweight='bold', color='#1a1a1a')
    handles = [Line2D([0], [0], marker='o', color='w', markersize=12,
                      markerfacecolor=pal.get(s, (0.53, 0.53, 0.53)),
                      markeredgecolor='#333333', linewidth=0, label=s)
               for s in symbols]
    ax.legend(handles=handles, loc='lower center', bbox_to_anchor=(0.5, 0.0),
              ncol=len(symbols), frameon=False, fontsize=12,
              columnspacing=1.5, handletextpad=0.3, borderpad=0.2)
    fig.savefig(png_out, bbox_inches='tight', dpi=220)
    plt.close(fig)


def render_structure(contcar, out_png, group, rotation='-90x', label=None):
    """CONTCAR → 论文风格球棍 PNG。返回 True/False。"""
    atoms = ase_read(str(contcar), format='vasp')
    pal = palette_for(group)
    syms = atoms.get_chemical_symbols()
    colors = np.array([pal.get(s, jmol_colors[atoms.numbers[i]])
                       for i, s in enumerate(syms)])
    atoms.arrays['colors'] = colors
    uniq = []
    for s in syms:
        if s not in uniq:
            uniq.append(s)
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as td:
            raw = _render_pov(atoms, os.path.join(td, 'raw.pov'), colors,
                              rotation=rotation)
            if raw:
                _compose(raw, out_png, uniq, pal, label=label)
                return True
    except Exception as e:
        print(f'  [warn] POV-Ray 渲染失败 {contcar}: {e}', flush=True)
    # ASE 回退
    try:
        from ase.visualize.plot import plot_atoms
        fig, ax = plt.subplots(figsize=(5, 4))
        if label:
            ax.set_title(label, fontsize=13, fontweight='bold')
        plot_atoms(atoms, ax, rotation=(rotation if ',' in rotation
                                        else rotation + ',0y,0z'), radii=0.5)
        ax.set_axis_off()
        fig.savefig(out_png, bbox_inches='tight', dpi=220)
        plt.close(fig)
        return True
    except Exception as e:
        plt.close('all')
        print(f'  [warn] ASE 回退亦失败 {contcar}: {e}', flush=True)
        return False


def render_inset(contcar, out_png, group):
    """FED 台阶缩略图: 俯视 POV 原图(无标题/图例), 白边裁剪。失败返回 None。"""
    try:
        atoms = ase_read(str(contcar), format='vasp')
        pal = palette_for(group)
        syms = atoms.get_chemical_symbols()
        colors = np.array([pal.get(s, jmol_colors[atoms.numbers[i]])
                           for i, s in enumerate(syms)])
        atoms.arrays['colors'] = colors
        os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            raw = _render_pov(atoms, os.path.join(td, 'raw.pov'), colors,
                              rotation='0x,0y,0z')
            if not raw:
                return None
            src = Image.open(raw)
            if src.mode in ('RGBA', 'LA', 'P'):
                src = src.convert('RGBA')
                img = Image.new('RGB', src.size, (255, 255, 255))
                img.paste(src, mask=src.split()[-1])
            else:
                img = src.convert('RGB')
            arr = np.asarray(img)
            mask = (arr < 247).any(axis=2)
            ys, xs = np.where(mask)
            if len(xs):
                pad = 12
                img = img.crop((max(xs.min() - pad, 0), max(ys.min() - pad, 0),
                                min(xs.max() + pad, img.width),
                                min(ys.max() + pad, img.height)))
            img.thumbnail((520, 520), Image.LANCZOS)
            img.save(out_png)
        return out_png
    except Exception as e:
        print(f'  [warn] 缩略图渲染失败 {contcar}: {e}', flush=True)
        return None


def plot_ads_bar_gradient(group, cols, vals, out_png):
    """吸附能柱状图: 颜色随数值渐变(越负=吸附越强=颜色越深), 带渐变色条。"""
    plt.rcParams['font.family'] = ['Arial', 'DejaVu Sans']
    fig, ax = plt.subplots(figsize=(8.6, 6.2), dpi=260)
    vmin, vmax = min(vals), max(vals)
    pad = max((vmax - vmin) * 0.12, 0.05)
    norm = matplotlib.colors.Normalize(vmin=vmin - pad, vmax=vmax + pad)
    cmap = matplotlib.colormaps['Blues_r']   # 最负 → 深蓝
    colors = [cmap(norm(v)) for v in vals]
    x = np.arange(len(cols))
    bars = ax.bar(x, vals, width=0.62, color=colors, edgecolor='#1a1a1a',
                  linewidth=1.4, zorder=3)
    for xi, v in zip(x, vals):
        ax.text(xi, v - 0.06, f'{v:.2f}', ha='center', va='top',
                fontsize=13.5, fontweight='bold', color='#1a1a1a', zorder=4)
    ax.axhline(0, color='#1a1a1a', lw=1.6, zorder=2)
    ax.set_xticks(x)
    ax.set_xticklabels([chem_math(c) for c in cols], fontsize=15)
    ax.set_ylabel(r'$E_\mathrm{ads}$ (eV)', fontsize=17)
    ax.set_ylim(vmin - abs(vmin) * 0.18 - 0.3, max(vmax, 0) + 0.25)
    ax.tick_params(axis='y', labelsize=14, width=1.6, length=5)
    ax.tick_params(axis='x', width=1.6, length=5)
    for spine in ax.spines.values():
        spine.set_linewidth(1.6)
        spine.set_color('#1a1a1a')
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    cbar = fig.colorbar(sm, ax=ax, pad=0.02, fraction=0.045)
    cbar.set_label(r'$E_\mathrm{ads}$ (eV)', fontsize=13)
    cbar.ax.tick_params(labelsize=11, width=1.2)
    cbar.outline.set_linewidth(1.2)
    fig.tight_layout()
    fig.savefig(out_png, dpi=260)
    plt.close(fig)
    return out_png


def plot_fed_thesis(group, fe, insets, out_png):
    """张洪毅硕论 图4.3 风格自由能台阶图。

    要素: 顶部加粗 Discharging 表头 / 黑实台阶横线 + 灰虚斜连接线 /
    每跃迁标 ΔG / 台阶下方 *物种 标签 / 台阶上方浮置吸附构型俯视缩略图 /
    PDS 红色突出 + 左下角 PDS/U_L 注记 / 黑框无 X 刻度。
    insets: {物种: 缩略图路径或 None}。
    """
    labels = fe['labels']
    G = fe['G']
    pds = fe['pds_index']
    n = len(G)
    plt.rcParams['font.family'] = ['Arial', 'DejaVu Sans']

    ymin_d = min(G) - 1.7
    ymax_d = max(G) + 3.1
    fig, ax = plt.subplots(figsize=(10.5, 7.4), dpi=110)
    ax.set_xlim(-0.05, n + 0.05)
    ax.set_ylim(ymin_d, ymax_d)

    # 台阶横线(黑实粗) + 连接线(灰虚细, PDS 红粗)
    for i, g in enumerate(G):
        ax.plot([i + 0.12, i + 0.88], [g, g], color='#1a1a1a', lw=4.5,
                solid_capstyle='butt', zorder=3)
    for i in range(n - 1):
        col, lw = ('#d62728', 2.6) if i == pds else ('#8a8a8a', 1.3)
        ax.plot([i + 0.88, i + 1.12], [G[i], G[i + 1]], color=col, lw=lw,
                ls=(0, (5, 3)), zorder=2)

    # ΔG 数值: 标在连接线中点旁(白底垫片防叠线)
    for i in range(n - 1):
        dg = G[i + 1] - G[i]
        col = '#d62728' if i == pds else '#555555'
        ax.text(i + 1.0, (G[i] + G[i + 1]) / 2, f'{dg:+.2f}',
                ha='center', va='center', fontsize=13.5, color=col,
                fontweight='bold', zorder=6,
                bbox=dict(boxstyle='round,pad=0.18', fc='white', ec='none',
                          alpha=0.92))

    # 物种标签: 台阶正下方
    for i, (sp, g) in enumerate(zip(labels, G)):
        ax.text(i + 0.5, g - 0.22, f'*{chem_math(sp)}', ha='center', va='top',
                fontsize=15, fontweight='bold', color='#1a1a1a', zorder=4)

    # 吸附构型俯视缩略图: 浮置于台阶上方
    fig.canvas.draw()
    bbox = ax.get_window_extent()
    px_per_data_x = bbox.width / (n + 0.1)
    for i, sp in enumerate(labels):
        p = insets.get(sp)
        if not p or not Path(p).exists():
            continue
        img = np.asarray(Image.open(p))
        zoom = (0.66 * px_per_data_x) / img.shape[1]
        ab = AnnotationBbox(OffsetImage(img, zoom=zoom), (i + 0.5, G[i] + 0.38),
                            box_alignment=(0.5, 0.0), frameon=False, zorder=1.5)
        ax.add_artist(ab)

    # 顶部 Discharging 表头(论文图4.3)
    header = 'Discharging : ' + r'$\rightarrow$'.join(
        chem_math(sp) for sp in labels)
    ax.text(0.5, 0.975, header, transform=ax.transAxes, ha='center', va='top',
            fontsize=15.5, fontweight='bold', color='#1a1a1a', zorder=6)

    # 左下角 PDS / U_L 注记
    if pds is not None and pds >= 0:
        a, b = chem_math(labels[pds]), chem_math(labels[pds + 1])
        dg = G[pds + 1] - G[pds]
        ax.text(0.025, 0.085,
                f'PDS: *{a}$\\rightarrow$*{b},  $\\Delta$G = {dg:+.2f} eV',
                transform=ax.transAxes, ha='left', va='bottom',
                fontsize=13.5, fontweight='bold', color='#d62728', zorder=6)
        if fe.get('U_L') is not None:
            ax.text(0.025, 0.03,
                    f'$U_\\mathrm{{L}}$ = {fe["U_L"]:+.2f} V vs Li/Li$^+$',
                    transform=ax.transAxes, ha='left', va='bottom',
                    fontsize=13.5, fontweight='bold', color='#1a1a1a', zorder=6)

    ax.set_ylabel('Relative Free Energy (eV)', fontsize=17)
    ax.set_xticks([])
    ax.tick_params(axis='y', labelsize=14, width=1.6, length=5)
    for spine in ax.spines.values():
        spine.set_linewidth(1.6)
        spine.set_color('#1a1a1a')
    fig.tight_layout()
    fig.savefig(out_png, dpi=260)
    plt.close(fig)
    return out_png


def min_s_metal_distance(contcar, metals):
    """min d(S–金属)。返回 (金属符号, 距离Å) 或 None (物种含 S 才有意义)。"""
    atoms = ase_read(str(contcar), format='vasp')
    syms = atoms.get_chemical_symbols()
    s_idx = [i for i, s in enumerate(syms) if s == 'S']
    best = None
    for m in metals:
        m_idx = [i for i, s in enumerate(syms) if s == m]
        for i in m_idx:
            if not s_idx:
                break
            d = atoms.get_distances(i, s_idx, mic=True).min()
            if best is None or d < best[1]:
                best = (m, float(d))
    return best


# ═══ 数据读取 (project.yaml + job.yaml, 与新版 adsorption.delta_e_rows 同口径) ═══
def load_group(group):
    """→ {'project', 'e_slab', 'rows': [{'species','name','e_config','delta_e'}],
         'legacy_root': Path} 或 None(组不完整)。"""
    pdir = IMPORTED / group
    with open(pdir / 'project.yaml', 'r', encoding='utf-8') as f:
        proj = yaml.safe_load(f)

    def job(rel):
        jp = REPO / rel / 'job.yaml' if not Path(rel).is_absolute() else Path(rel) / 'job.yaml'
        with open(jp, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    slab = job(proj['members']['clean_slab'])
    if slab.get('state') != 'DONE':
        return None
    e_slab = float(slab['results']['energy_e0_eV'])
    refs = proj.get('species_refs') or {}
    rows = []
    for cdir in proj['members']['configs']:
        j = job(cdir)
        name = j['system']
        sp = name.rsplit('_', 1)[1]
        if j.get('state') != 'DONE' or sp not in refs:
            return None            # 任一物种未完成 → 组不完整, 不出报告
        e_cfg = float(j['results']['energy_e0_eV'])
        rows.append({'species': sp, 'name': name, 'state': 'DONE',
                     'e_config': e_cfg,
                     'delta_e': e_cfg - e_slab - float(refs[sp])})
    if {r['species'] for r in rows} != set(SPECIES_ORDER):
        return None
    rows.sort(key=lambda r: SPECIES_ORDER.index(r['species']))
    return {'project': proj, 'e_slab': e_slab, 'rows': rows,
            'legacy_root': Path(proj['legacy_source'])}


def _load_freeenergy_module():
    spec = importlib.util.spec_from_file_location(
        'vc_freeenergy', REPO / 'vcstudio' / 'project' / 'freeenergy.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def compute_fed(data):
    """新版 freeenergy 契约 → 老版 analysis_paragraphs 期望的 fe dict。"""
    fe_mod = _load_freeenergy_module()
    res = fe_mod.path_from_project_and_molecules(
        data['rows'], data['e_slab'], str(MOLECULES_DIR))
    steps = res['steps']
    G = [s['G'] for s in steps]
    return {
        'labels': [s['label'].rstrip('*') for s in steps],
        'G': G,
        'pds_index': res['pds_index'] if res['pds_index'] is not None else -1,
        'U_L': res['u_l'],
        'step_diffs': [G[i + 1] - G[i] for i in range(len(G) - 1)],
        'per_electron': res['per_electron'],
        'mu_li': res['mu_li'],
    }


# ═══ WordReport (移植原版 layer2_science/report.py, 仅保留 Li-S 简报所需节) ═══
class WordReport:
    def __init__(self):
        self.doc = Document()
        self._section = 0
        self._subsection = 0
        self._setup_styles()

    def _heading1(self, title):
        self._section += 1
        self._subsection = 0
        p = self.doc.add_heading(f'{self._section} {title}', level=1)
        self._force_heading_font(p, 23)

    def _heading2(self, title):
        self._subsection += 1
        p = self.doc.add_heading(
            f'{self._section}.{self._subsection} {title}', level=2)
        self._force_heading_font(p, 16)

    def _force_heading_font(self, paragraph, pt):
        for run in paragraph.runs:
            run.font.name = HEADING_FONT
            run.font.size = Pt(pt)
            run.bold = True
            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.find(qn('w:rFonts'))
            if rFonts is None:
                rFonts = rPr.makeelement(qn('w:rFonts'), {})
                rPr.insert(0, rFonts)
            for attr in ('w:ascii', 'w:hAnsi', 'w:eastAsia'):
                rFonts.set(qn(attr), HEADING_FONT)

    def _setup_styles(self):
        style = self.doc.styles['Normal']
        style.font.name = BODY_FONT
        style.font.size = Pt(12)
        style.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.line_spacing = 1.5
        style.paragraph_format.first_line_indent = Pt(24)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        rPr = style.element.get_or_add_rPr()
        rFonts = rPr.find(qn('w:rFonts'))
        if rFonts is None:
            rFonts = rPr.makeelement(qn('w:rFonts'), {})
            rPr.insert(0, rFonts)
        rFonts.set(qn('w:eastAsia'), BODY_FONT)
        for level, size, centered in [(1, 23, True), (2, 16, False), (3, 15, False)]:
            h = self.doc.styles[f'Heading {level}']
            h.font.name = HEADING_FONT
            h.font.size = Pt(size)
            h.font.color.rgb = RGBColor(0x1a, 0x1a, 0x1a)
            h.font.bold = True
            h.paragraph_format.space_before = Pt(12 if level > 1 else 18)
            h.paragraph_format.space_after = Pt(6)
            h.paragraph_format.first_line_indent = Pt(0)
            h.paragraph_format.alignment = (WD_ALIGN_PARAGRAPH.CENTER if centered
                                            else WD_ALIGN_PARAGRAPH.LEFT)
            hrPr = h.element.get_or_add_rPr()
            hrFonts = hrPr.find(qn('w:rFonts'))
            if hrFonts is None:
                hrFonts = hrPr.makeelement(qn('w:rFonts'), {})
                hrPr.insert(0, hrFonts)
            hrFonts.set(qn('w:eastAsia'), HEADING_FONT)

    # ── 封面 ──
    def add_cover(self, title, subtitle='', catalyst='', molecules=None):
        for _ in range(6):
            self.doc.add_paragraph('')
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        run = p.add_run(title)
        run.font.name = HEADING_FONT
        run.font.size = Pt(22)
        run.font.color.rgb = RGBColor(0x1a, 0x1a, 0x1a)
        run.bold = True
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.makeelement(qn('w:rFonts'), {})
        rPr.insert(0, rFonts)
        rFonts.set(qn('w:eastAsia'), HEADING_FONT)
        if subtitle:
            p2 = self.doc.add_paragraph()
            p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p2.paragraph_format.first_line_indent = Pt(0)
            run2 = p2.add_run(subtitle)
            run2.font.name = BODY_FONT
            run2.font.size = Pt(14)
            run2.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        self.doc.add_paragraph('')
        info_lines = []
        if catalyst:
            info_lines.append(f'催化剂: {catalyst}')
        if molecules:
            info_lines.append(f'吸附分子: {", ".join(molecules)}')
        info_lines.append(f'日期: {time.strftime("%Y-%m-%d")}')
        info_lines.append('计算软件: VASP 5.4.4 + VASP Catalyst Studio V2.0.0')
        info_lines.append('泛函: RPBE (GGA=RP)　|　色散: DFT-D3 (IVDW=11)')
        info_lines.append('赝势: PAW_PBE　|　ENCUT: 400 eV')
        for line in info_lines:
            p = self.doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            run = p.add_run(line)
            run.font.name = BODY_FONT
            run.font.size = Pt(10.5)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        self.doc.add_page_break()

    # ── 1 计算结果概要 ──
    def add_summary(self, stats):
        self._heading1('计算结果概要')
        self._add_body_paragraph(
            '本报告汇总了基于密度泛函理论（DFT）的 VASP 计算结果。'
            '所有计算使用 VASP 5.4.4，采用 RPBE 交换关联泛函和 PAW_PBE 赝势。'
            '吸附能定义为 E_ads = E_system − E_slab − E_molecule，'
            '负值表示放热（有利）吸附，正值表示吸热（不利）吸附。')
        table = self.doc.add_table(rows=1, cols=2)
        table.style = 'Light Grid Accent 1'
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        hdr = table.rows[0].cells
        hdr[0].text = '统计项'
        hdr[1].text = '数值'
        self._bold_row(table.rows[0])
        stat_rows = [
            ('总计算数', str(stats.get('total', 0))),
            ('已收敛', str(stats.get('converged', 0))),
            ('失败/放弃', str(stats.get('failed', 0))),
            ('运行中/排队中', str(stats.get('running', 0))),
            ('吸附体系数', str(stats.get('adsorption_systems', 0))),
        ]
        if stats.get('strongest_ads'):
            stat_rows.append(('最强吸附', f"{stats['strongest_ads']} eV"))
        if stats.get('weakest_ads'):
            stat_rows.append(('最弱吸附', f"{stats['weakest_ads']} eV"))
        for label, value in stat_rows:
            row = table.add_row()
            row.cells[0].text = label
            row.cells[1].text = value
        self._add_table_caption('表1  计算统计汇总')
        self.doc.add_paragraph('')

    # ── 2 吸附能矩阵 (单体系竖表) ──
    def add_energy_table(self, matrix_data):
        self._heading1('吸附能矩阵')
        self._add_body_paragraph(
            '吸附能 E_ads = E_system − E_slab − E_molecule。'
            '负值（绿色）表示放热吸附，正值（红色）表示吸热吸附。绝对值越大，吸附越强。')
        cols = matrix_data['cols']
        vals = matrix_data['matrix'][0]

        def _color(v):
            return '2a7a3b' if v < -3 else ('e0556a' if v > 0 else None)

        table = self.doc.add_table(rows=len(cols) + 1, cols=2)
        table.style = 'Light Grid Accent 1'
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        self._set_cell(table.rows[0].cells[0], '物种', bold=True)
        self._set_cell(table.rows[0].cells[1], 'E_ads (eV)', bold=True)
        for i, mol in enumerate(cols):
            r = table.rows[i + 1]
            self._set_cell(r.cells[0], chem_text(mol), bold=True)
            if vals[i] is not None:
                self._set_cell(r.cells[1], f'{vals[i]:.2f}', color=_color(vals[i]))
        self._set_col_widths(table, [Cm(3.4), Cm(3.4)])
        self._no_split_rows(table)
        self._keep_table_on_one_page(table)
        self._add_table_caption('表2  吸附能矩阵 (eV)')
        self.doc.add_paragraph('')
        pairs = [(chem_text(c), v) for c, v in zip(cols, vals) if v is not None]
        if pairs:
            s = min(pairs, key=lambda x: x[1]); w = max(pairs, key=lambda x: x[1])
            self._add_body_paragraph(
                f'其中 {s[0]} 吸附最强（{s[1]:.2f} eV），{w[0]} 最弱（{w[1]:.2f} eV）。')

    # ── 3 图表分析 ──
    def add_charts(self, figures, group=''):
        """figures: [{'path', 'label', 'small'?}]。数据图整行大图;
        small 图两张并排一行(双列表格, 图注在图下同格), 报告更紧凑。"""
        if not figures:
            return
        self._heading1('图表分析')
        fig_num = 1
        i = 0
        items = [f for f in figures if Path(f['path']).exists()]
        while i < len(items):
            item = items[i]
            if item.get('small') and i + 1 < len(items) and items[i + 1].get('small'):
                pair = [item, items[i + 1]]
                table = self.doc.add_table(rows=1, cols=2)
                table.alignment = WD_TABLE_ALIGNMENT.CENTER
                table.autofit = False
                table.allow_autofit = False
                for j, it in enumerate(pair):
                    cell = table.rows[0].cells[j]
                    cell.width = Cm(8.0)
                    cp = cell.paragraphs[0]
                    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    cp.paragraph_format.first_line_indent = Pt(0)
                    run = cp.add_run()
                    try:
                        run.add_picture(it['path'], width=Cm(7.6))
                    except Exception as e:
                        run.text = f'[{Path(it["path"]).name} 嵌入失败 {e}]'
                    cap = cell.add_paragraph(f'图{fig_num}  {it["label"]}')
                    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    cap.paragraph_format.first_line_indent = Pt(0)
                    cap.paragraph_format.space_before = Pt(2)
                    cap.paragraph_format.space_after = Pt(4)
                    for r2 in cap.runs:
                        r2.font.name = BODY_FONT
                        r2.font.size = Pt(9)
                        r2.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                    fig_num += 1
                self._no_split_rows(table)
                i += 2
                continue
            try:
                self.doc.add_picture(item['path'], width=Inches(5.5))
                last = self.doc.paragraphs[-1]
                last.alignment = WD_ALIGN_PARAGRAPH.CENTER
                last.paragraph_format.first_line_indent = Pt(0)
            except Exception as e:
                self._add_body_paragraph(f'[图表: {Path(item["path"]).name} — 嵌入失败 {e}]')
                i += 1
                continue
            self._add_figure_caption(f'图{fig_num}  {item["label"]}')
            fig_num += 1
            i += 1

    # ── 4 结果分析与讨论 ──
    def add_analysis(self, group, matrix_data, fe=None):
        self._heading1('结果分析与讨论')
        for para in analysis_paragraphs(group, matrix_data, fe=fe):
            if para['kind'] == 'h2':
                self._heading2(para['text'])
            else:
                self._add_body_paragraph(para['text'])

    # ── 5 计算方法 ──
    def add_methods_section(self):
        self._heading1('计算方法')
        self._add_body_paragraph(
            '本工作采用 VASP 5.4.4，PAW_PBE 赝势，交换关联泛函 RPBE（GGA=RP），'
            '平面波截断能 ENCUT=400 eV，色散校正 DFT-D3（IVDW=11，零阻尼 Grimme D3），'
            '不使用 DFT+U，不加偶极校正。电子收敛 EDIFF=1×10⁻⁵ eV，'
            '离子弛豫收敛 EDIFFG=−0.02 eV/Å。含磁性 3d 金属（V/Cr/Mn/Fe/Co/Ni）的体系 '
            'ISPIN=2 并按元素顺序设初始磁矩，其余 ISPIN=1。布里渊区采样：周期性 slab/吸附用 '
            'Gamma 中心 3×3×1 Monkhorst-Pack，孤立分子用 Γ 点。吸附能定义 '
            'E_ads = E(slab+吸附质) − E(slab) − E(分子)，负值表示放热（有利）吸附。')

    def save(self, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(output_path)
        return output_path

    # ── helpers ──
    def _add_body_paragraph(self, text):
        p = self.doc.add_paragraph(text)
        p.paragraph_format.first_line_indent = Pt(24)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        for run in p.runs:
            run.font.name = BODY_FONT
            run.font.size = Pt(12)
        return p

    def _add_table_caption(self, text):
        p = self.doc.add_paragraph(text)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        p.paragraph_format.space_before = Pt(4)
        for run in p.runs:
            run.font.name = BODY_FONT
            run.font.size = Pt(10.5)
            run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    def _add_figure_caption(self, text):
        p = self.doc.add_paragraph(text)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        p.paragraph_format.space_after = Pt(6)
        for run in p.runs:
            run.font.name = BODY_FONT
            run.font.size = Pt(10.5)
            run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    def _bold_row(self, row):
        cells = row.cells if hasattr(row, 'cells') else row
        for cell in cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True

    def _set_cell(self, cell, text, bold=False, color=None):
        cell.text = ''
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        run = p.runs[0] if p.runs else p.add_run()
        run.text = text
        run.font.name = BODY_FONT
        run.font.size = Pt(11)
        run.bold = bold
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.find(qn('w:rFonts'))
        if rFonts is None:
            rFonts = rPr.makeelement(qn('w:rFonts'), {})
            rPr.insert(0, rFonts)
        for attr in ('w:ascii', 'w:hAnsi', 'w:eastAsia'):
            rFonts.set(qn(attr), BODY_FONT)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)

    def _keep_table_on_one_page(self, table):
        """整表锁同一页: 除末行外全部单元格段落设 keepNext(Word 会把连续
        keepNext 的行与下一行绑在一起, 配合行级 cantSplit 即整表不跨页)。"""
        rows = table.rows
        for row in rows[:-1]:
            for cell in row.cells:
                for para in cell.paragraphs:
                    para.paragraph_format.keep_with_next = True

    def _no_split_rows(self, table):
        for ri, row in enumerate(table.rows):
            trPr = row._tr.get_or_add_trPr()
            trPr.append(OxmlElement('w:cantSplit'))
            if ri == 0:
                trPr.append(OxmlElement('w:tblHeader'))

    def _set_col_widths(self, table, widths):
        table.autofit = False
        table.allow_autofit = False
        for row in table.rows:
            for i, w in enumerate(widths):
                if i < len(row.cells):
                    row.cells[i].width = w


def analysis_paragraphs(group, matrix_data, fe=None):
    """移植原版 analysis_paragraphs (数据驱动 + 文献框架)。"""
    out = []
    cols = matrix_data['cols']
    vals = matrix_data['matrix'][0]
    pairs = [(c, v) for c, v in zip(cols, vals) if v is not None]

    out.append({'kind': 'h2', 'text': '多硫化物吸附强度与锚定能力'})
    if pairs:
        emin = min(v for _, v in pairs)
        emax = max(v for _, v in pairs)
        sp_strong = next(c for c, v in pairs if v == emin)
        sp_weak = next(c for c, v in pairs if v == emax)
        out.append({'kind': 'p', 'text': (
            f'{group} 对所考察含硫物种的吸附能介于 {emin:.2f}～{emax:.2f} eV，'
            f'其中对 {chem_text(sp_strong)} 吸附最强（{emin:.2f} eV），'
            f'对 {chem_text(sp_weak)} 相对最弱（{emax:.2f} eV）。')})
        if all(v < -2.90 for _, v in pairs):
            out.append({'kind': 'p', 'text': (
                '全部吸附能均显著低于 Cui 等提出的理想锚定窗口（−2.90～−1.65 eV），'
                f'说明 {group} 对多硫化物具有很强的化学锚定，有利于抑制可溶性多硫化物的'
                '穿梭效应；但吸附亦明显偏强，按基于吸附能描述符的活性判据，过强结合往往'
                '不利于后续多硫化物的转化与产物脱附（文献报道）。')})
    else:
        out.append({'kind': 'p', 'text': '暂无吸附能数据，无法进行吸附强度分析。'})

    if fe and fe.get('pds_index', -1) >= 0:
        labels = fe['labels']
        pds = fe['pds_index']
        dG = fe['step_diffs'][pds]
        per_e = fe['per_electron'][pds]
        U_L = fe.get('U_L')
        a, b = chem_text(labels[pds]), chem_text(labels[pds + 1])
        out.append({'kind': 'h2', 'text': '硫还原自由能路径与限制电位步'})
        t = ('在计算锂电极（CHE，U = 0 V vs Li/Li⁺）框架下，硫还原反应（SRR）自由能台阶图'
             '显示：自 S₈ 经液相多硫化物（Li₂S₈/Li₂S₆/Li₂S₄）的转化整体自发下行，'
             f'而限制电位步（PDS, potential-determining step）为 {a}→{b}，'
             f'其相对自由能升高 ΔG = {dG:+.2f} eV（折合 {per_e:+.2f} eV/电子）')
        t += (f'，对应限制电位 U_L = {U_L:+.2f} V vs Li/Li⁺。'
              if U_L is not None else '。')
        out.append({'kind': 'p', 'text': t})
        out.append({'kind': 'p', 'text': (
            '该步对应放电后半程"液相多硫化物→固相 Li₂S"的转化与沉积，与文献普遍报道的'
            '"锂硫电池后半程还原动力学迟缓、是制约倍率与容量的关键瓶颈"相符（文献报道）。'
            '当外加电位 U ≤ U_L 时整条路径转为全程下行，'
            'U_L 即定量刻画了驱动该转化所需克服的电化学过电位。')})

    out.append({'kind': 'h2', 'text': '催化性能评估与设计启示'})
    perf = [f'综合而言，{group} 对多硫化物具备强吸附锚定，在抑制穿梭方面具有优势']
    if fe and fe.get('pds_index', -1) >= 0:
        perf.append('；但最终固相 Li₂S 的生成/沉积为其限制电位步，制约整体转化效率')
    perf.append('。')
    out.append({'kind': 'p', 'text': ''.join(perf)})
    out.append({'kind': 'p', 'text': (
        '基于吸附能/过电位描述符的设计判据表明，"适中"的多硫化物吸附（而非一味增强）'
        '更利于在锚定与转化之间取得平衡（文献报道）。据此，可通过配位环境调控'
        '（如 P/S 掺杂、调整配位 N 数）适度削弱过强吸附、降低关键转化步的自由能升高，'
        '以进一步提升催化活性。')})
    return out


# ═══ 主流程 ═══════════════════════════════════════════════════════════════════
def build_figures(group, data, figdir, fe=None):
    """渲染结构图并汇总全部图: 柱状图(复用新版 Origin) + FED(论文图4.3 风格重绘,
    台阶上浮吸附构型缩略图) + 结构图(论文风格)。"""
    figdir = Path(figdir)
    legacy = data['legacy_root']
    figures = []
    cols = [r['species'] for r in data['rows']]
    vals = [r['delta_e'] for r in data['rows']]
    bar = figdir / 'ads_bar_gradient.png'
    try:
        plot_ads_bar_gradient(group, cols, vals, str(bar))
        figures.append({'path': str(bar),
                        'label': f'{group} 多硫化物吸附能柱状图（颜色随吸附强度渐变）'})
    except Exception as e:
        print(f'  [warn] 渐变柱状图绘制失败, 回退 Origin 版: {e}', flush=True)
        if (figdir / 'ads_bar.png').exists():
            figures.append({'path': str(figdir / 'ads_bar.png'),
                            'label': f'{group} 多硫化物吸附能柱状图 (Origin)'})
    if fe:
        insets = {}
        for sp in fe['labels']:
            contcar = legacy / f'{group}_{sp}' / 'CONTCAR'
            if contcar.exists():
                insets[sp] = render_inset(contcar,
                                          figdir / f'_inset_{sp}.png', group)
        fed_png = figdir / 'fed_thesis.png'
        try:
            plot_fed_thesis(group, fe, insets, str(fed_png))
            figures.append({'path': str(fed_png),
                            'label': f'{group} 硫还原相对吉布斯自由能台阶图'
                                     '（含吸附构型俯视图）'})
        except Exception as e:
            print(f'  [warn] 论文风格 FED 绘制失败, 回退 Origin 版: {e}', flush=True)
            if (figdir / 'fed.png').exists():
                figures.append({'path': str(figdir / 'fed.png'),
                                'label': f'{group} 硫还原自由能台阶图 (Origin)'})
    metals = group_metals(group)
    # 裸载体: 俯视+侧视 (论文图3.1/图4.1 惯例: 先俯视后侧视)
    slab_contcar = legacy / group / 'CONTCAR'
    if slab_contcar.exists():
        for rot, view, suffix in (('0x,0y,0z', '俯视', '_top'), ('-90x', '侧视', '')):
            out = figdir / f'struct_{group}{suffix}.png'
            if render_structure(slab_contcar, out, group, rotation=rot,
                                label=f'{group} substrate'):
                figures.append({'path': str(out), 'small': True,
                                'label': f'{group} 基底优化结构（{view}）'})
    # 各物种: 侧视+俯视 (论文图4.2 惯例, 侧视标 d(S–金属))
    for r in data['rows']:
        sp = r['species']
        contcar = legacy / f'{group}_{sp}' / 'CONTCAR'
        if not contcar.exists():
            print(f'  [warn] 缺 CONTCAR: {contcar}', flush=True)
            continue
        try:
            dsm = min_s_metal_distance(contcar, metals)
        except Exception:
            dsm = None
        dtxt = f'，d(S–{dsm[0]}) = {dsm[1]:.2f} Å' if dsm else ''
        math_label = f'{group} + {chem_math(sp)}'
        for rot, view, suffix in (('-90x', '侧视', ''), ('0x,0y,0z', '俯视', '_top')):
            out = figdir / f'struct_{group}_{sp}{suffix}.png'
            if render_structure(contcar, out, group, rotation=rot,
                                label=math_label):
                cap = (f'{group} + {chem_text(sp)} 吸附构型（{view}{dtxt}）'
                       if view == '侧视' else
                       f'{group} + {chem_text(sp)} 吸附构型（{view}）')
                figures.append({'path': str(out), 'small': True, 'label': cap})
    return figures


def generate_group_report(group):
    print(f'== {group} ==', flush=True)
    data = load_group(group)
    if data is None:
        print(f'  [skip] {group} 组不完整(未全 DONE / 缺物种), 不生成报告', flush=True)
        return None
    cols = [r['species'] for r in data['rows']]
    vals = [r['delta_e'] for r in data['rows']]
    matrix_data = {'rows': [group], 'cols': cols, 'matrix': [vals]}
    try:
        fe = compute_fed(data)
        print(f"  FED: PDS={fe['labels'][fe['pds_index']]}→"
              f"{fe['labels'][fe['pds_index'] + 1]}, U_L={fe['U_L']}", flush=True)
    except Exception as e:
        fe = None
        print(f'  [warn] 自由能路径不可用: {e}', flush=True)

    figdir = IMPORTED / group / 'report_figs'
    figures = build_figures(group, data, figdir, fe=fe)
    n_struct = sum(1 for f in figures if 'struct_' in Path(f['path']).name)
    print(f'  图表: {len(figures)} 张 (其中结构图 {n_struct})', flush=True)

    r = WordReport()
    r.add_cover(title=f'{group} 体系锂硫吸附能计算报告',
                subtitle='SAC/DAC 多硫化物吸附 — 结果报告',
                catalyst=group, molecules=cols)
    flat = [v for v in vals if v is not None]
    stats = {'total': len(flat) + 1, 'converged': len(flat) + 1, 'failed': 0,
             'running': 0, 'adsorption_systems': len(flat)}
    if flat:
        stats['strongest_ads'] = f'{min(flat):.3f}'
        stats['weakest_ads'] = f'{max(flat):.3f}'
    r.add_summary(stats)
    r.add_energy_table(matrix_data)
    r.add_charts(figures, group=group)
    r.add_analysis(group, matrix_data, fe=fe)
    r.add_methods_section()

    out_new = IMPORTED / group / f'report_{group}.docx'
    r.save(str(out_new))
    out_old = OLD_REPORTS / group / f'report_{group}.docx'
    try:
        out_old.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(out_new, out_old)
        print(f'  已覆盖老版报告: {out_old}', flush=True)
    except Exception as e:
        print(f'  [warn] 覆盖老版报告失败: {e}', flush=True)
    print(f'  已生成: {out_new}', flush=True)
    return str(out_new)


def main(argv):
    groups = argv or DEFAULT_GROUPS
    done = []
    for g in groups:
        try:
            out = generate_group_report(g)
            if out:
                done.append(out)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'  [error] {g} 失败: {e}', flush=True)
    print(f'\n完成 {len(done)}/{len(groups)}: ' + '; '.join(done), flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
