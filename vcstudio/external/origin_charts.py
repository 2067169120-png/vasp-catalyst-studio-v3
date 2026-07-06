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

PUB_COLORS = ['#4C72B0', '#DD8452', '#55A868', '#C44E52',
              '#8172B2', '#937860', '#DA8BC3', '#8C8C8C']

# ── runner:在系统 Python 里跑的独立脚本(只依赖 originpro/numpy,不依赖 vcstudio) ──
RUNNER_SOURCE = r'''# -*- coding: utf-8 -*-
"""vcstudio Origin 渲染 runner(自动生成,勿手改):spec.json → PNG + result.json"""
import json, math, os, sys

PUB = ['#4C72B0', '#DD8452', '#55A868', '#C44E52', '#8172B2', '#937860', '#DA8BC3', '#8C8C8C']


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
        cmds.append('label -r legend')       # Origin 图例是名为 legend 的 label 对象
    if hide_x_labels:
        cmds.append('layer.x.label.show=0')
    for cmd in cmds:
        try:
            op.lt_exec(cmd + ';')
        except Exception:
            pass


def _hline(op, y, color='#8C8C8C'):
    try:
        op.lt_exec('draw -l -h %.6f;' % y)
    except Exception:
        pass


def render_bar(op, c, out_dir, width):
    d = c['data']
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
    _style_layer(op, gl, drop_legend=(len(d['rows']) <= 1))
    band = d.get('band')
    if band:
        _hline(op, band[0]); _hline(op, band[1])
    gl.rescale()
    png = os.path.join(out_dir, c['name'] + '.png')
    gp.save_fig(png, width=width)
    return png


def render_ladder(op, c, out_dir, width):
    d = c['data']
    steps = d['steps']
    xs, ys = [], []
    for i, s in enumerate(steps):
        xs += [i - 0.3, i + 0.3]
        ys += [s['G'], s['G']]
    wks = op.new_sheet('w', lname=str(c['name'])[:12])
    wks.from_list(0, xs, 'coord')
    wks.from_list(1, ys, 'G')
    gp = op.new_graph()
    gl = gp[0]
    p = gl.add_plot(wks, coly=1, colx=0, type='l')
    p.color = PUB[0]
    try:
        p.set_int('line.width', 3)
    except Exception:
        pass
    pds = d.get('pds_index')
    if pds is not None:                       # 决速步红色连接段
        wk2 = op.new_sheet('w', lname='pds')
        wk2.from_list(0, [pds + 0.3, pds + 1 - 0.3], 'x')
        wk2.from_list(1, [steps[pds]['G'], steps[pds + 1]['G']], 'y')
        p2 = gl.add_plot(wk2, coly=1, colx=0, type='l')
        p2.color = PUB[3]
        try:
            p2.set_int('line.width', 3)
        except Exception:
            pass
    gl.axis('y').title = c.get('ylabel', '\\g(D)G (eV)')     # Origin 转义:希腊 Δ
    gl.axis('x').title = 'Reaction coordinate'
    _style_layer(op, gl, hide_x_labels=True)                  # 反应坐标数字刻度无意义
    gl.rescale()
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
