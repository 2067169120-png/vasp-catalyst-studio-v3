"""Origin 出版级图表适配器:外呼系统 Python(带 originpro)执行内嵌 runner 脚本。

为什么外呼而不 import:EXE 不打包 originpro/numpy(体积+许可),Origin COM 崩溃也
不能拖垮 GUI 进程——所以把渲染脚本(RUNNER_SOURCE,零 vcstudio 依赖)写到临时目录,
调系统 Python 跑,读回 result.json。找不到 Python/originpro/Origin → ok=False,
报告层自动用 charts.py 的 SVG 兜底(用户决策:SVG 兜底)。

同一数据契约(spec 2026-07-06):bar/heatmap/ladder/convergence 与 charts.py 完全一致。
纯函数(spec 构建/结果解析)离线可测;subprocess 经 run= 注入。中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

PUB_COLORS = ['#4477AA', '#EE6677', '#228833', '#CCBB44',
              '#66CCEE', '#AA3377', '#BBBBBB', '#222255']

# ── runner:在系统 Python 里跑的独立脚本(只依赖 originpro/numpy,不依赖 vcstudio) ──
RUNNER_SOURCE = r'''# -*- coding: utf-8 -*-
"""vcstudio Origin 渲染 runner(自动生成,勿手改):spec.json → PNG + result.json"""
import json, math, os, sys, textwrap

PUB = ['#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE', '#AA3377', '#BBBBBB', '#222255']
# 出版级样式常量(移植原版 origin_ZnTa_plots.py 真机验证过的口径)
BAR_COLORS = ['#E64B35', '#F39B7F', '#4DBBD5', '#00A087', '#3C5488', '#DC0000']  # NPG
U0_COLOR, PDS_RED, GRID_GRAY = '#2C5F8A', '#C92A2A', '#CCCCCC'
BAND_FILL_RGB = '232,236,241'                 # #E8ECF1 理想窗口带

_ARIAL = None


def _arial_idx():
    global _ARIAL
    if _ARIAL is None:
        import originpro as op
        _ARIAL = op.lt_int('font(Arial)')
    return _ARIAL


def _lab(gl, text, x, y, pt=13, color=None):
    """文本标签:强制 Arial(修全角数字)+ 字号 + 可选颜色(原版 _lab 同款)。"""
    lab = gl.add_label(str(text), float(x), float(y))
    try:
        lab.set_int('font', _arial_idx())
        lab.set_int('pt', pt)
        if color is not None:
            import originpro as op
            lab.set_int('color', op.lt_int('color(%s)' % color))
    except Exception:
        pass
    return lab


def _grid_on(gl):
    try:
        gl.lt_exec('grid 1')
        gl.lt_exec('grid.color = color(%s)' % GRID_GRAY)
    except Exception:
        pass


def _nan(v):
    return float('nan') if v is None else float(v)


def _style_layer(op, gl, drop_legend=True, hide_x_labels=False):
    """统一样式:轴线加粗、Arial、字号;单系列去图例;逐条执行坏一条不连坐。"""
    cmds = ['layer.x.thickness=1.8', 'layer.y.thickness=1.8',
            'layer.x.label.pt=18', 'layer.y.label.pt=18',
            'layer.x.label.font=font(Arial)', 'layer.y.label.font=font(Arial)',
            'xb.font=font(Arial)', 'yl.font=font(Arial)',
            'xb.fsize=20', 'yl.fsize=20']
    if drop_legend:
        cmds.append('legend.show=0')         # ZnTa 真机口径(label -r 不可靠)
    if hide_x_labels:
        cmds.append('layer.x.label.show=0')
    for cmd in cmds:
        try:
            gl.lt_exec(cmd)                  # 层作用域(op.lt_exec 会飘到别的活动层)
        except Exception:
            pass


def _hline(op, y, color='#8C8C8C'):
    try:
        op.lt_exec('draw -l -h %.6f;' % y)
    except Exception:
        pass


def render_bar(op, c, out_dir, width):
    d = c['data']
    band = d.get('band')
    if len(d['rows']) == 1:
        return _render_bar_single(op, c, out_dir, width)
    # 多体系:分组柱(保留原逻辑)+ 网格 + 参考线
    wks = op.new_sheet('w', lname=str(c['name'])[:12])
    wks.from_list(0, [str(x) for x in d['cols']], 'Species')
    for j, rname in enumerate(d['rows']):
        wks.from_list(1 + j, [_nan(v) for v in d['matrix'][j]], str(rname))
    gp = op.new_graph(template='column')
    gl = gp[0]
    for j in range(len(d['rows'])):
        p = gl.add_plot(wks, coly=1 + j, colx=0, type='c')
        p.color = PUB[j % len(PUB)]
    gl.axis('y').title = c.get('ylabel', 'E\\-(ads) (eV)')   # Origin 转义:真下标
    gl.axis('x').title = ''
    _style_layer(op, gl, drop_legend=False)
    _grid_on(gl)
    if band:
        _hline(op, band[0]); _hline(op, band[1])
    gl.rescale()
    png = os.path.join(out_dir, c['name'] + '.png')
    gp.save_fig(png, width=width)
    return png


def _strip_common_prefix(names):
    """剥离全员公共前缀(截到最后一个 '_'):'Zn-Ta_S8'... → 'S8'...,防 X 轴标签重叠。"""
    if len(names) < 2:
        return names
    pref = os.path.commonprefix(names)
    k = pref.rfind('_') + 1
    return [n[k:] or n for n in names] if k > 0 else names


def _wrap_species_label(name):
    """保留完整构型名，优先在目录分隔符后换行，避免水平文字互相覆盖。"""
    value = str(name)
    if len(value) <= 14:
        return value
    breakable = value.replace('_', '_ ').replace('-', '- ')
    return '\n'.join(textwrap.wrap(
        breakable, width=14, break_long_words=False, break_on_hyphens=True))


def _render_bar_single(op, c, out_dir, width):
    """单体系出版级柱状图(原版 _fig_ads_bar 口径):NPG 逐物种配色 + 数值标注 +
    物种名下置 + 隐藏 X 刻度 + 网格 + 阴影理想窗口带(失败降级为参考线)。"""
    d = c['data']
    species = [_wrap_species_label(x)
               for x in _strip_common_prefix([str(x) for x in d['cols']])]
    vals = [_nan(v) for v in d['matrix'][0]]
    n = len(species)
    x_spacing = 2.0
    xpos = [i * x_spacing for i in range(n)]
    band = d.get('band')

    ymin = min(v for v in vals if v == v) if any(v == v for v in vals) else -1.0
    ymax = max((v for v in vals if v == v), default=0.0)
    yr = max(ymax - ymin, 1.0)
    ylo = ymin - yr * 0.40                                   # 底部留数值标注空间
    yhi = max(ymax + yr * 0.12, 0.3)
    if band:
        ylo = min(ylo, min(band) - yr * 0.15)
    x0, x1 = -1.6, xpos[-1] + 1.6

    gp = op.new_graph(template='column')
    gl = gp[0]
    wks = op.new_sheet('w', lname=str(c['name'])[:12])
    wks.from_list(0, xpos, 'X')
    for i, sp in enumerate(species):                          # 每物种一列(NaN 掩码)→ 独立配色
        col = [float('nan')] * n
        col[i] = vals[i]
        wks.from_list(i + 1, col, sp)
    for i in range(n):
        p = gl.add_plot(wks, coly=i + 1, colx=0, type='c')
        p.color = BAR_COLORS[i % len(BAR_COLORS)]
        p.lt_exec('set %C -w 1100')

    gl.axis('x').title = ''
    gl.axis('y').title = c.get('ylabel', 'E\\-(ads) (eV)')
    gl.set_ylim(ylo, yhi)
    gl.set_xlim(x0, x1)
    _style_layer(op, gl, drop_legend=True)
    try:                                                      # 隐藏无意义的 X 数字刻度
        gl.lt_exec('layer.x.label.color=color(white)')
        gl.lt_exec('layer.x.majorTicks=0')
        gl.lt_exec('layer.x.minorTicks=0')
    except Exception:
        pass
    _grid_on(gl)
    if band:                                              # 理想窗口:两条参考线
        _hline(op, band[0]); _hline(op, band[1])          # (Origin 填充带不可靠,SVG 版有阴影带)
    _hline(op, 0.0)                                           # 零参考线

    sp_y = ylo - yr * 0.12                                    # 物种名落 x 轴线下方
    for i, y in enumerate(vals):
        if y != y:
            continue
        _lab(gl, '%.2f' % y, xpos[i] - 0.62, y - yr * 0.06, pt=13)
        _lab(gl, species[i], xpos[i] - 0.55, sp_y, pt=12)

    png = os.path.join(out_dir, c['name'] + '.png')
    gp.save_fig(png, width=width)
    return png


def render_ladder(op, c, out_dir, width):
    """自由能阶梯(原版 _fig_free_energy 口径):蓝主线 w1200 + 红 PDS w1800 +
    物种名上置/析出标签下置 + PDS/U_L 标注块 + 网格 + 隐藏 X 数字刻度。"""
    d = c['data']
    steps = d['steps']
    n = len(steps)
    half = 0.35
    nan = float('nan')
    # 学位论文 FED 惯例(张洪毅 图4.3):水平台阶实线,台阶间连接线虚线,跃迁标 ΔG
    xs, ys, cxs, cys = [], [], [], []
    for i, s in enumerate(steps):
        xs += [i - half, i + half, nan]                      # NaN 断段:各台阶独立实线
        ys += [s['G'], s['G'], nan]
        if i + 1 < n:
            cxs += [i + half, i + 1 - half, nan]             # 连接线(虚)
            cys += [s['G'], steps[i + 1]['G'], nan]
    wks = op.new_sheet('w', lname=str(c['name'])[:12])
    wks.from_list(0, xs, 'coord')
    wks.from_list(1, ys, 'G')
    wks.from_list(2, cxs, 'cx')
    wks.from_list(3, cys, 'conn')
    gp = op.new_graph(template='line')
    gl = gp[0]
    pc = gl.add_plot(wks, coly=3, colx=2, type='l')          # 连接线:灰细虚(垫底)
    pc.color = '#9AA7B4'
    pc.lt_exec('set %C -w 700'); pc.lt_exec('set %C -d 1')
    p = gl.add_plot(wks, coly=1, colx=0, type='l')
    p.color = U0_COLOR
    p.lt_exec('set %C -w 1200')                              # 台阶:蓝实粗
    try:
        p.set_int('line.width', 3)                           # 保底(某些模板 -w 不生效)
    except Exception:
        pass
    pds = d.get('pds_index')
    if pds is not None:                                      # 决速步:红实粗覆盖在上
        wk2 = op.new_sheet('w', lname='pds')
        wk2.from_list(0, [pds + half, pds + 1 - half], 'x')
        wk2.from_list(1, [steps[pds]['G'], steps[pds + 1]['G']], 'y')
        p2 = gl.add_plot(wk2, coly=1, colx=0, type='l')
        p2.color = PDS_RED
        p2.lt_exec('set %C -w 1800')
        try:
            p2.set_int('line.width', 4)
        except Exception:
            pass
    gl.axis('y').title = c.get('ylabel', '\\g(D)G (eV)')     # Origin 转义:希腊 Δ
    gl.axis('x').title = 'Reaction coordinate'
    gs = [s['G'] for s in steps]
    lo, hi = min(gs), max(gs)
    rng = (hi - lo) or 1.0
    gl.set_xlim(-0.7, float(n) - 0.3)
    gl.set_ylim(lo - rng * 0.13, hi + rng * 0.24)            # 顶部留标注空间
    _style_layer(op, gl, hide_x_labels=True)                  # 反应坐标数字刻度无意义
    _grid_on(gl)

    dy = max(rng * 0.045, 0.30)
    for i, s in enumerate(steps):                             # 物种名上置 + 析出标签下置灰
        _lab(gl, s.get('label', ''), i - 0.14, s['G'] + dy, pt=13)
        if s.get('sub_label'):
            _lab(gl, s['sub_label'], i - 0.14, s['G'] - dy * 1.05, pt=10,
                 color='150,150,150')
    for i in range(n - 1):                                    # 跃迁 ΔG 标注(论文图4.3 惯例)
        d_g = steps[i + 1]['G'] - steps[i]['G']
        ymid = (steps[i]['G'] + steps[i + 1]['G']) / 2.0
        col = '200,30,30' if i == pds else '90,90,90'
        # 放连接线中点右下(物种名在台阶上方,错开避碰)
        _lab(gl, '%+.2f' % d_g, i + 0.55, ymid - dy * 0.55, pt=10, color=col)
    ax0 = -0.6                                               # 左下标注块(原版口径)
    if pds is not None:
        d_g = steps[pds + 1]['G'] - steps[pds]['G']
        _lab(gl, 'PDS %s>%s:  %+.2f eV' % (steps[pds].get('label', ''),
                                           steps[pds + 1].get('label', ''), d_g),
             ax0, lo + rng * 0.27, pt=13, color='200,30,30')
    if d.get('u_l') is not None:
        _lab(gl, 'U_L = %+.2f V' % float(d['u_l']), ax0, lo + rng * 0.19, pt=12,
             color='30,30,30')

    png = os.path.join(out_dir, c['name'] + '.png')
    gp.save_fig(png, width=width)
    return png


def render_convergence(op, c, out_dir, width):
    d = c['data']
    wks = op.new_sheet('w', lname=str(c['name'])[:12])
    wks.from_list(0, [float(x) for x in d['x']], d.get('xlabel', 'X'))
    wks.from_list(1, [float(y) for y in d['y']], 'E (eV)')
    gp = op.new_graph()
    gl = gp[0]
    p = gl.add_plot(wks, coly=1, colx=0, type='y')     # line+symbol
    p.color = PUB[0]
    ref = float(d['y'][-1]); eps = float(d.get('epsilon', 0.001))
    _hline(op, ref + eps); _hline(op, ref - eps)
    gl.axis('y').title = 'E (eV)'
    gl.axis('x').title = d.get('xlabel', '')
    _style_layer(op, gl)
    gl.rescale()
    png = os.path.join(out_dir, c['name'] + '.png')
    gp.save_fig(png, width=width)
    return png


def render_heatmap(op, c, out_dir, width):
    # 真机验证(Origin2024b):matrix add_mplot 的 242/220 都渲出空图(色标与数据无关),
    # 且行列名映射需深挖模板。热图显式不支持 → 报告层用 SVG 兜底(带行列名+格内数值,
    # 筛选表反而更实用)。数据仍存入 opju 工作表,用户可在 Origin 里手工出图。
    d = c['data']
    wks = op.new_sheet('w', lname=str(c['name'])[:12])
    wks.from_list(0, [str(r) for r in d['rows']], 'System')
    for j, cname in enumerate(d['cols']):
        wks.from_list(1 + j, [_nan(row[j]) for row in d['matrix']], str(cname))
    raise RuntimeError('Origin 热图暂不支持(数据已存 opju 工作表,报告用 SVG 版)')


_RENDERERS = {'bar': render_bar, 'ladder': render_ladder,
              'convergence': render_convergence, 'heatmap': render_heatmap}


def main():
    spec_path, out_dir = sys.argv[1], sys.argv[2]
    with open(spec_path, 'r', encoding='utf-8') as f:
        spec = json.load(f)
    import originpro as op
    if op.oext:
        op.set_show(False)
    images, errors = {}, []
    try:
        for c in spec['charts']:
            try:
                png = _RENDERERS[c['kind']](op, c, out_dir, spec.get('width', 2400))
                if png and os.path.isfile(png):
                    images[c['name']] = png
                else:
                    errors.append('%s: 无输出' % c['name'])
            except Exception as e:
                errors.append('%s: %s' % (c['name'], e))
        if spec.get('opju'):
            try:
                op.save(spec['opju'])
            except Exception as e:
                errors.append('opju: %s' % e)
    finally:
        if op.oext:
            op.exit()
    with open(os.path.join(out_dir, '_origin_result.json'), 'w', encoding='utf-8') as f:
        json.dump({'images': images, 'errors': errors}, f)


if __name__ == '__main__':
    main()
'''


def find_python(configured: str = '') -> str | None:
    """系统 Python 探测(EXE 里 sys.executable 是 EXE 自身,必须另找)。"""
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which('python') or shutil.which('python3') or shutil.which('py')


def render_charts(specs: list, out_dir, *, opju_path=None, width: int = 2400,
                  python_exe: str | None = None, configured_python: str = '',
                  run=None, timeout: int = 300) -> dict:
    """一次 Origin 会话渲多张图。specs=[{'kind','data','title','name','ylabel'?}]。

    返回 {'ok','images':{name:png},'error'}。任何环节失败 → ok=False 降级(SVG 兜底),
    绝不抛。runner 脚本 + spec.json 落 out_dir(留证可手查),结果读 _origin_result.json。
    """
    run = run or subprocess.run
    py = python_exe or find_python(configured_python)
    if not py:
        return {'ok': False, 'images': {}, 'error': '未找到系统 Python(Origin 出图需要装了 originpro 的 Python)'}
    # 必须绝对路径:Origin 进程工作目录与本进程不同,相对路径 save_fig 会存错地方/失败
    out_dir = os.path.abspath(str(out_dir))
    if opju_path:
        opju_path = os.path.abspath(str(opju_path))
    os.makedirs(out_dir, exist_ok=True)
    runner = os.path.join(out_dir, '_origin_runner.py')
    spec_file = os.path.join(out_dir, '_origin_spec.json')
    result_file = os.path.join(out_dir, '_origin_result.json')
    if os.path.isfile(result_file):
        os.remove(result_file)                          # 防读到上一轮陈旧结果
    with open(runner, 'w', encoding='utf-8') as f:
        f.write(RUNNER_SOURCE)
    with open(spec_file, 'w', encoding='utf-8') as f:
        json.dump({'charts': specs, 'width': width,
                   'opju': str(opju_path) if opju_path else None}, f, ensure_ascii=False)
    try:
        proc = run([py, runner, spec_file, str(out_dir)], cwd=str(out_dir),
                   timeout=timeout, capture_output=True)
        code = getattr(proc, 'returncode', 1)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'ok': False, 'images': {}, 'error': f'Origin runner 启动失败:{e}'}
    if not os.path.isfile(result_file):
        err = ''
        stderr = getattr(proc, 'stderr', b'') or b''
        if stderr:
            err = stderr.decode('utf-8', errors='replace').strip()[-400:]
        return {'ok': False, 'images': {},
                'error': f'Origin runner 无结果(退出码 {code}){(":" + err) if err else ""}'}
    try:
        with open(result_file, 'r', encoding='utf-8') as f:
            result = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {'ok': False, 'images': {}, 'error': f'Origin 结果解析失败:{e}'}
    images = {k: v for k, v in (result.get('images') or {}).items() if os.path.isfile(v)}
    return {'ok': bool(images), 'images': images,
            'error': '; '.join(result.get('errors') or [])}
