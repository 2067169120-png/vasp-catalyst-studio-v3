"""完整项目报告组装:ΔE 表 + Origin/SVG 图表 + POV-Ray 结构图廊 + 自由能路径 + AI 分析。

编排逻辑(spec 2026-07-06 用户 7 决策落地):
- 图表:先试 Origin(出版级 PNG 落 report_figs/),失败/缺席逐图降级为内嵌 SVG
- 热图:显式 SVG(Origin2024b 真机验证不支持,数据仍在 opju)
- 自由能:config['lis_molecules_dir'] 指向旧分子库(如 E:/V2.0.0/results/done/lis_results)
  时自动算 Li-S 放电路径;算不出(缺物种)记备注不阻塞
- AI:有 keyring key → 调用并持久化进报告;无 → 报告里放提示词包指引
- 结构图:嵌各作业目录 figs/ 里已渲染的 PNG(拉回结果时全自动渲,此处不现渲)
所有外部依赖注入可测;单段失败降级为文字说明,报告永远出得来。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import html
import os
from pathlib import Path

from vcstudio.generate.incar_builder import parse_incar
from vcstudio.project import adsorption, ai_analysis, charts, freeenergy, report

_INCAR_KEYS = ('ENCUT', 'EDIFF', 'EDIFFG', 'ISMEAR', 'SIGMA', 'ISPIN', 'IVDW', 'GGA', 'NSW', 'IBRION')


def _esc(s):
    return html.escape(str(s if s is not None else ''))


def incar_summary_from_dir(job_dir) -> dict:
    """读作业目录真实 INCAR 的关键键(喂 AI 的真实参数;缺文件 → 空 dict)。"""
    try:
        with open(os.path.join(str(job_dir), 'INCAR'), 'r', encoding='utf-8',
                  errors='replace') as f:
            d = parse_incar(f.read())
    except OSError:
        return {}
    return {k: d[k] for k in _INCAR_KEYS if k in d}


def _member_dirs(proj) -> list:
    mem = proj.get('members') or {}
    return [d for d in ([mem.get('clean_slab'), mem.get('gas_ref')]
                        + list(mem.get('configs') or [])) if d]


def _fig_html(png_path, report_dir, caption='') -> str:
    rel = os.path.relpath(str(png_path), str(report_dir)).replace(os.sep, '/')
    cap = f"<div class='dim' style='text-align:center'>{_esc(caption)}</div>" if caption else ''
    return (f"<div style='margin:12px 0;text-align:center'>"
            f"<img src='{_esc(rel)}' style='max-width:100%;border:1px solid #e5e7eb;"
            f"border-radius:8px'/>{cap}</div>")


def generate_project_report(proj: dict, out_path, *, config: dict | None = None,
                            origin_render=None, ai_analyze=None, log=None) -> Path:
    """组装并落盘完整项目报告(HTML)。origin_render/ai_analyze 可注入替身。"""
    config = config or {}
    log = log or (lambda s: None)
    origin_render = origin_render or _default_origin
    ai_analyze = ai_analyze or ai_analysis.analyze
    out_path = Path(out_path)
    report_dir = out_path.parent
    figs_dir = report_dir / 'report_figs'
    name = proj.get('name', 'project')

    delta = adsorption.delta_e_rows(proj)
    dirs = _member_dirs(proj)
    rows = report.collect_jobs(dirs)
    summary = report.summarize(rows)
    sections: list = []

    # ── 0a. ΔE 汇总统计(原版 summary-stats 移植,零依赖) ──
    des = [r['delta_e'] for r in delta['rows'] if isinstance(r.get('delta_e'), (int, float))]
    if des:
        strongest = min(delta['rows'], key=lambda r: r['delta_e']
                        if isinstance(r.get('delta_e'), (int, float)) else 1e9)
        sections.append(
            '<h2>ΔE 汇总统计</h2><p>'
            f'有效组态 {len(des)} 个;最强吸附 <b>{_esc(strongest["name"])}</b>'
            f'(ΔE = {min(des):.4f} eV);最弱 ΔE = {max(des):.4f} eV;'
            f'平均 ΔE = {sum(des) / len(des):.4f} eV。</p>')

    # ── 0b. 计算参数表(原版 INCAR parameters 表移植;读共享 INCAR 真实键) ──
    inc0 = {}
    for d in dirs:
        inc0 = incar_summary_from_dir(d)
        if inc0:
            break
    if inc0:
        prow = ''.join(f'<tr><td><code>{_esc(k)}</code></td><td class="num">{_esc(v)}</td></tr>'
                       for k, v in inc0.items())
        sections.append(
            '<h2>计算参数(成员共享 INCAR 关键键)</h2>'
            f'<table><thead><tr><th>键</th><th>值</th></tr></thead><tbody>{prow}</tbody></table>'
            "<p class='dim'>完整输入与赝势身份(variant/TITEL/ENMAX)见各作业 job.yaml 的 inputs 小节。</p>")

    # ── 1. ΔE 图表(Origin 优先,SVG 兜底) ──
    band = config.get('ideal_window')            # 可配理想窗口 (lo, hi);默认不画
    band_label = config.get('ideal_window_label', '理想窗口')
    fed = _try_fed(delta, config, log)
    origin_specs, svg_parts = [], {}
    try:
        bar = charts.bar_data_from_delta(name, delta['rows'],
                                         band=band, band_label=band_label)
        origin_specs.append({'kind': 'bar', 'name': 'ads_bar', 'data': bar})
        svg_parts['ads_bar'] = charts.render_bar_svg(bar, title=f'{name} 吸附能')
    except ValueError as e:
        sections.append(f"<p class='dim'>ΔE 柱状图未生成:{_esc(e)}</p>")
    if fed:
        ladder = charts.ladder_data(fed['steps'], u_l=fed['u_l'])
        origin_specs.append({'kind': 'ladder', 'name': 'fed', 'data': ladder})
        svg_parts['fed'] = charts.render_ladder_svg(
            ladder, title=f'Li-S 放电路径(μ_Li={fed["mu_li"]:.3f} eV,电子能未含 ZPE/熵)')
    origin_images = {}
    if origin_specs:
        r = origin_render(origin_specs, str(figs_dir),
                          opju_path=str(figs_dir / f'{name}.opju'))
        origin_images = r.get('images') or {}
        if r.get('error'):
            log(f"Origin 出图部分降级:{r['error'][:200]}")
    if origin_specs:
        sections.append('<h2>图表</h2>')
        for spec in origin_specs:
            key = spec['name']
            if key in origin_images:
                sections.append(_fig_html(origin_images[key], report_dir,
                                          caption=f'{key}(Origin,600dpi 级,工程存 report_figs/{name}.opju)'))
            elif key in svg_parts:
                sections.append(svg_parts[key])          # SVG 内嵌兜底

    # ── 2. AI 分析(持久化进报告;无 key 给指引) ──
    inc = {}
    for d in dirs:                                       # 逐成员找第一份可读 INCAR(真实参数)
        inc = incar_summary_from_dir(d)
        if inc:
            break
    payload = ai_analysis.build_payload(
        project_name=name, delta_rows=delta['rows'],
        incar_summary=inc, path_result=fed)
    ai_out = ai_analyze(payload)
    sections.append('<h2>AI 分析</h2>')
    if ai_out.get('ok'):
        cave = ''.join(f'<li>{_esc(c)}</li>' for c in ai_out.get('caveats', []))
        sections.append(
            f"<p>{_esc(ai_out['analysis_zh'])}</p>"
            f"<p style='font-style:italic'>{_esc(ai_out['paragraph_en'])}</p>"
            + (f"<ul class='dim'>{cave}</ul>" if cave else '')
            + f"<p class='dim'>置信度:{_esc(ai_out.get('confidence'))}(AI 生成,须人工核验)</p>")
    else:
        sections.append(f"<p class='dim'>AI 分析未运行:{_esc(ai_out.get('error'))}</p>")

    # ── 3. 结构图廊(嵌已渲染的 POV-Ray PNG) ──
    gallery = []
    for d in dirs:
        fdir = os.path.join(str(d), 'figs')
        if not os.path.isdir(fdir):
            continue
        for png in sorted(os.listdir(fdir)):
            if png.endswith(('_top.png', '_side.png')):
                gallery.append(_fig_html(os.path.join(fdir, png), report_dir,
                                         caption=f'{os.path.basename(d)} / {png}'))
    if gallery:
        sections.append('<h2>结构图(POV-Ray)</h2>' + ''.join(gallery))

    # ── 4. 方法学约定(原版 methods 节的通用版;论文同款口径,项目专用内容不写死) ──
    conv = ['E<sub>ads</sub> = E(slab+ads) − E(slab) − E(ref),负值 = 有利吸附;'
            'ΔE 着色:&lt; −3 eV 强吸附(绿)、&gt; 0(红)。',
            '全部能量为 DFT 电子能(OSZICAR E0),未含 ZPE/熵修正。',
            '成员全部 DONE 才给 ΔE;能量经物理合理性闸(E≥0/|E|&gt;10⁴ 拒收)。']
    if fed:
        conv.insert(1, f'μ<sub>Li</sub> = (E(Li₂S) − E(S₈)/8) / 2 = {fed["mu_li"]:.4f} eV'
                       '(由分子库估算);ΔG 参照 S8* = 0,U<sub>L</sub> = −max(ΔG/Δn·e)(CHE)。')
    sections.append('<h2>方法学约定</h2><ul>'
                    + ''.join(f'<li>{c}</li>' for c in conv) + '</ul>')

    html_text = report.render_html(rows, summary, title=f'{name} 完整报告',
                                   delta_e=delta, extra_html=''.join(sections))
    report_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_text)
    return out_path


def _try_fed(delta, config, log):
    """分子库目录已配置时算放电路径;缺物种/失败 → None + 日志,不阻塞报告。"""
    mol_dir = (config or {}).get('lis_molecules_dir') or ''
    if not mol_dir or not os.path.isdir(mol_dir):
        return None
    try:
        slab_state, e_slab = delta['slab']
        return freeenergy.path_from_project_and_molecules(
            delta['rows'], e_slab=e_slab, molecules_dir=mol_dir)
    except ValueError as e:
        log(f'自由能路径未生成:{e}')
        return None


def _default_origin(specs, out_dir, opju_path=None):
    from vcstudio.external import origin_charts
    return origin_charts.render_charts(specs, out_dir, opju_path=opju_path)
