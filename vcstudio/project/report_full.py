"""完整项目报告组装:ΔE 表 + Origin/SVG 图表 + POV-Ray 结构图廊 + 自由能路径 + AI 分析。

编排逻辑(spec 2026-07-06 用户 7 决策落地):
- 图表:先试 Origin(出版级 PNG 落 report_figs/),失败/缺席逐图降级为内嵌 SVG
- 热图:显式 SVG(Origin2024b 真机验证不支持,数据仍在 opju)
- 自由能:优先用 project['molecules_dir'](导入项目自带分子库),不存在再回退
  config['lis_molecules_dir'];算不出(缺物种)记备注不阻塞
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


def _energy_source_line(dirs) -> str:
    """Describe recorded per-member energy provenance without inventing one."""
    from vcstudio.shared import manifest as _mm

    sources: list[str] = []
    missing = 0
    for d in dirs:
        m = _mm.load_manifest(d)
        source = str(((m or {}).get('results') or {}).get('energy_source') or '').strip()
        if source:
            if source not in sources:
                sources.append(source)
        else:
            missing += 1
    if sources:
        suffix = f'；另有 {missing} 个成员未记录 energy_source' if missing else ''
        return ('电子总能来源按各成员 job.yaml 记录为：'
                + '、'.join(f'<code>{_esc(source)}</code>' for source in sources)
                + suffix + '。')
    return ('各成员 job.yaml 未完整记录 energy_source；请以成员原始输出复核，'
            '本报告不假定能量全部来自 OSZICAR E0。')


def _member_dirs(proj) -> list:
    mem = proj.get('members') or {}
    return [d for d in ([mem.get('clean_slab'), mem.get('gas_ref')]
                        + list(mem.get('configs') or [])) if d]


def _species_reference_evidence(delta: dict) -> list[dict]:
    """Use the exact manifest-backed evidence already resolved for ΔE.

    Re-reading ``project['species_refs']`` here would create a second truth
    source and could make the evidence table disagree with the formula.
    """
    rows = delta.get('species_reference_evidence') or []
    return [dict(item) for item in rows if isinstance(item, dict)]


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
    _annotate_continuations(delta, proj)   # ΔE 行按续算历史注"续算×N"(发刊级可复现)
    dirs = _member_dirs(proj)
    rows = report.collect_jobs(dirs)
    summary = report.summarize(rows)
    sections: list = []

    # 逐物种参考是 Li-S 公式的实际第三项，必须与ΔE同页可审计。
    ref_evidence = _species_reference_evidence(delta)
    if ref_evidence:
        ref_rows = []
        for item in ref_evidence:
            hashes = item['hashes'] if isinstance(item['hashes'], dict) else {}
            hash_text = '；'.join(f'{key}:{str(value)[:12]}…'
                                 for key, value in sorted(hashes.items())) or '—'
            conf = item['confirmation'] if isinstance(item['confirmation'], dict) else {}
            conf_text = ('人工确认：' + str(conf.get('reason') or '未填理由')
                         if conf.get('manual') else
                         ('多证据自动门控' if item.get('valid') else '未记录'))
            validation_text = ('可用于 ΔE' if item.get('valid') else
                               (item.get('note') or '参考未通过完整性校验'))
            ref_rows.append(
                f'<tr><td><b>{_esc(item["species"])}</b></td>'
                f'<td>{_esc(item["state"])}</td><td class="num">{_esc(item["energy"])}</td>'
                f'<td><code>{_esc(item["source"] or "未记录")}</code></td>'
                f'<td><code>{_esc(item["job"])}</code></td>'
                f'<td>{_esc(item["imported_from"] or "—")}</td>'
                f'<td><code>{_esc(hash_text)}</code></td><td>{_esc(conf_text)}</td>'
                f'<td>{_esc(validation_text)}</td></tr>')
        sections.append(
            '<h2>逐物种参考能与溯源</h2><table><thead><tr>'
            '<th>物种</th><th>状态</th><th>E(ref) / eV</th><th>能量来源</th>'
            '<th>参考作业</th><th>导入源</th><th>SHA256(前12位)</th><th>确认方式</th>'
            '<th>ΔE 门控</th>'
            '</tr></thead><tbody>' + ''.join(ref_rows) + '</tbody></table>')

    formula_rows = []
    for row in delta.get('rows') or []:
        if row.get('delta_e') is None or row.get('e_ref') is None:
            continue
        formula_rows.append(
            f'<tr><td>{_esc(row.get("name"))}</td><td>{_esc(row.get("reference_species") or row.get("species"))}</td>'
            f'<td>{_esc(row.get("reference_state") or "—")}</td>'
            f'<td class="num">{row.get("e_config"):.10f}</td>'
            f'<td class="num">{delta["slab"][1]:.10f}</td>'
            f'<td class="num">{row.get("e_ref"):.10f}</td>'
            f'<td class="num"><b>{row.get("delta_e"):.10f}</b></td></tr>')
    if formula_rows:
        sections.append(
            '<h2>吸附能逐项核算</h2><p><code>ΔE = E(slab+ads) − E(slab) − E(ref)</code></p>'
            '<table><thead><tr><th>构型</th><th>参考物种</th><th>参考状态</th><th>E(slab+ads)</th>'
            '<th>E(slab)</th><th>E(ref)</th><th>ΔE / eV</th></tr></thead><tbody>'
            + ''.join(formula_rows) + '</tbody></table>')

    # ── 0a. ΔE 汇总统计(原版 summary-stats 移植,零依赖) ──
    des = [r['delta_e'] for r in delta['rows'] if isinstance(r.get('delta_e'), (int, float))]
    if des:
        strongest = min(delta['rows'], key=lambda r: r['delta_e']
                        if isinstance(r.get('delta_e'), (int, float)) else 1e9)
        sections.append(
            '<h2>ΔE 汇总统计</h2><p>'
            f'有效构型 {len(des)} 个;最强吸附 <b>{_esc(strongest["name"])}</b>'
            f'(ΔE = {min(des):.4f} eV);最弱 ΔE = {max(des):.4f} eV;'
            f'平均 ΔE = {sum(des) / len(des):.4f} eV。</p>')

    # ── 0b. 计算参数表(INCAR 关键键 + KPOINTS 网格;发刊级可复现) ──
    inc0 = {}
    for d in dirs:
        inc0 = incar_summary_from_dir(d)
        if inc0:
            break
    kpts_repro, potcar_prov = _repro_fields_from_dirs(dirs)
    if inc0 or kpts_repro:
        prow = ''.join(f'<tr><td><code>{_esc(k)}</code></td><td class="num">{_esc(v)}</td></tr>'
                       for k, v in inc0.items())
        if kpts_repro:
            grid = ' × '.join(str(x) for x in kpts_repro)
            prow += (f'<tr><td><code>KPOINTS</code></td>'
                     f'<td class="num">{_esc(grid)}(倒格矢自动网格)</td></tr>')
        tail = (
            "<p class='dim'>本表展示首个可读成员的 INCAR 关键键与首份可用 K 点记录；"
            "不据此推断所有成员参数相同，逐成员值请查看各自输入文件与 job.yaml。</p>"
            + ('' if potcar_prov else
               "<p class='dim'>完整赝势身份(variant/TITEL/ENMAX)见各作业 job.yaml 的 inputs 小节。</p>"))
        sections.append(
            '<h2>计算参数(首个可读成员的 INCAR 关键键 + 可用 K 点网格)</h2>'
            f'<table><thead><tr><th>键</th><th>值</th></tr></thead><tbody>{prow}</tbody></table>'
            + tail)
    # ── 0c. POTCAR 身份小表(赝势溯源:结果永远可答"哪套赝势算的") ──
    if potcar_prov:
        prows = ''.join(
            f'<tr><td>{_esc(p.get("element"))}</td><td>{_esc(p.get("variant"))}</td>'
            f'<td><code>{_esc(p.get("titel"))}</code></td>'
            f'<td class="num">{_esc(p.get("enmax"))}</td></tr>'
            for p in potcar_prov)
        sections.append(
            '<h2>赝势身份(POTCAR provenance)</h2>'
            '<table><thead><tr><th>元素</th><th>variant</th><th>TITEL</th>'
            f'<th>ENMAX / eV</th></tr></thead><tbody>{prows}</tbody></table>'
            "<p class='dim'>赝势本体为版权材料,不随仓库分发;此表仅记身份供溯源与复算对齐。</p>")

    # ── 1. ΔE 图表(Origin 优先,SVG 兜底) ──
    band = config.get('ideal_window')            # 可配理想窗口 (lo, hi);默认不画
    band_label = config.get('ideal_window_label', '理想窗口')
    fed = _try_fed(delta, config, log, proj=proj)
    origin_specs, svg_parts = [], {}
    if not any(r.get('delta_e') is not None for r in (delta.get('rows') or [])):
        sections.append(
            "<p class='dim'>当前报告为诊断版：尚无可用 ΔE。请在软件中处理"
            "缺失的清洁表面/吸附构型，或确认 NEEDS_HUMAN 成员后重新生成。</p>")
    try:
        bar = charts.bar_data_from_delta(name, delta['rows'],
                                         band=band, band_label=band_label)
        origin_specs.append({'kind': 'bar', 'name': 'ads_bar', 'data': bar})
        svg_parts['ads_bar'] = charts.render_bar_svg(bar, title=f'{name} 吸附能')
    except ValueError as e:
        sections.append(f"<p class='dim'>ΔE 柱状图未生成:{_esc(e)}</p>")
    if fed:
        # pds_index 必须透传 freeenergy 的逐电子口径:Li-S 末步 8 e⁻,按原始 ΔG
        # 重算会高亮错步(图上决速步 ≠ U_L 对应步)
        ladder = charts.ladder_data(fed['steps'], u_l=fed['u_l'],
                                    pds_index=fed['pds_index'])
        origin_specs.append({'kind': 'ladder', 'name': 'fed', 'data': ladder})
        corr_note = ('含 ZPE−TS 振动校正(298.15 K)' if fed.get('thermo_corrected')
                     else '电子能未含 ZPE/熵')
        svg_parts['fed'] = charts.render_ladder_svg(
            ladder, title=f'Li-S 放电路径(μ_Li={fed["mu_li"]:.3f} eV,{corr_note})')
    elif (proj.get('molecules_dir') or (config or {}).get('lis_molecules_dir')):
        sections.append(
            "<p class='dim'>Li-S 自由能台阶未生成：分子参考态或吸附态不齐，"
            "或其中存在未确认收敛的结果。NEEDS_HUMAN 分子能量不会进入 μLi/ΔG。</p>")
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
    slab_dir = (proj.get('members') or {}).get('clean_slab')
    slab_els = []
    if slab_dir:
        from vcstudio.shared import manifest as _mm
        slab_els = ((_mm.load_manifest(slab_dir) or {}).get('inputs') or {}).get('elements') or []
    payload = ai_analysis.build_payload(
        project_name=name, delta_rows=delta['rows'],
        incar_summary=inc, path_result=fed, slab_elements=slab_els)
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
    if fed and fed.get('thermo_corrected'):
        meta = fed.get('thermo_meta') or {}
        corr_line = ('ΔG 已含吸附态振动热校正(谐振子 ZPE − T·S<sub>vib</sub>,298.15 K;'
                     '物种:' + '、'.join(
                         f'{sp} {v["g_corr"]:+.3f} eV' for sp, v in meta.items()) + ')。')
    else:
        corr_line = '吸附能与 Li-S 路径按 DFT 电子能口径计算，未含 ZPE/熵修正。'
    method_scope = (
        '本项目由外部已算结果导入，软件不默认其 INCAR/ENCUT/K 点一致；'
        '请以上方参数与赝势表为准，对缺失的复现信息做人工稽核。'
        if proj.get('import_source') else
        '项目内各作业 ENCUT 统一(生成时按元素并集取一致截断能，保 ΔE 各成员基组一致)。')
    conv = ['E<sub>ads</sub> = E(slab+ads) − E(slab) − E(ref),负值 = 有利吸附;'
            'ΔE 着色:&lt; −3 eV 强吸附(绿)、&gt; 0(红)。',
            _energy_source_line(dirs + [row['job'] for row in ref_evidence if row.get('job')]),
            corr_line,
            '成员全部 DONE 才给 ΔE;能量经物理合理性闸(E≥0/|E|&gt;10⁴ 拒收)。',
            '平面波基组不存在基组重叠误差(BSSE),无需 counterpoise 校正;'
            '气相参考的真空盒尺寸见各作业输入文件(POSCAR/CONTCAR)。',
            method_scope]
    if fed:
        conv.insert(1, f'μ<sub>Li</sub> = (E(Li₂S) − E(S₈)/8) / 2 = {fed["mu_li"]:.4f} eV'
                       '(由分子库估算);ΔG 参照 S8* = 0,U<sub>L</sub> = −max(ΔG/Δn·e)(CHE);'
                       'U<sub>L</sub> 是相对 S8/Li₂S 整反应平衡电位的极限电位(整反应参照系),'
                       '非相对 Li/Li⁺ 电极。')
    sections.append('<h2>方法学约定</h2><ul>'
                    + ''.join(f'<li>{c}</li>' for c in conv) + '</ul>')

    html_text = report.render_html(rows, summary, title=f'{name} 完整报告',
                                   delta_e=delta, extra_html=''.join(sections))
    report_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_text)
    return out_path


def _try_fed(delta, config, log, proj=None):
    """有可用分子库时算放电路径;缺物种/失败 → None + 日志,不阻塞报告。

    目录优先级:``proj['molecules_dir']``(导入项目自带) →
    ``config['lis_molecules_dir']``(全局兼容配置)。项目中的路径已失效时也会
    回退全局目录，避免移动项目后整段自由能静默丢失。

    热校正(投稿级):config['freq_dirs'] = {物种: 频率作业目录} 时,自动解析各
    OUTCAR 频率(IBRION=5/6)做 ZPE−TS 校正并叠进 ΔG;有虚频的物种记日志提醒。
    未配置保持电子能口径(报告方法学节明示)。
    """
    project_dir = str((proj or {}).get('molecules_dir') or '').strip()
    config_dir = str((config or {}).get('lis_molecules_dir') or '').strip()
    if project_dir and os.path.isdir(project_dir):
        mol_dir = project_dir
    elif config_dir and os.path.isdir(config_dir):
        mol_dir = config_dir
        if project_dir:
            log(f'项目分子库目录不存在,已回退全局配置:{project_dir}')
    else:
        return None
    g_corr, corr_meta = None, {}
    freq_dirs = (config or {}).get('freq_dirs') or {}
    if freq_dirs:
        from vcstudio.project import thermo
        corr_meta = thermo.load_corrections(freq_dirs)
        if corr_meta:
            g_corr = {sp: v['g_corr'] for sp, v in corr_meta.items()}
            for sp, v in corr_meta.items():
                if v['n_imag']:
                    log(f'⚠ {sp} 频率计算含 {v["n_imag"]} 个虚频'
                        f'({v["imag_cm1"]} cm⁻¹):校正已按实频计,请人工核查构型')
    try:
        slab_state, e_slab = delta['slab']
        fed = freeenergy.path_from_project_and_molecules(
            delta['rows'], e_slab=e_slab, molecules_dir=mol_dir, g_corr=g_corr)
        if corr_meta:
            fed['thermo_meta'] = corr_meta
        return fed
    except ValueError as e:
        log(f'自由能路径未生成:{e}')
        return None


def _repro_fields_from_dirs(dirs):
    """从首个可读成员 manifest 取(KPOINTS 网格, POTCAR 身份表)——发刊级可复现字段。

    KPOINTS 优先 manifest 顶层 ``kpoints``,退 ``inputs.kpoints``;POTCAR 身份取
    ``inputs.potcar_provenance``(逐元素 {element/variant/titel/enmax})。任一成员给全即用。
    缺失一律返回 (None, []),调用方按"不展示该表"降级。
    """
    from vcstudio.shared import manifest as _mm
    kpts, prov = None, []
    for d in dirs:
        m = _mm.load_manifest(d)
        if not m:
            continue
        inputs = m.get('inputs') or {}
        if kpts is None:
            kpts = m.get('kpoints') or inputs.get('kpoints')
        # 取元素最全的一份赝势身份(构型含吸附质,元素多于清洁表面)
        cand = inputs.get('potcar_provenance') or []
        if len(cand) > len(prov):
            prov = cand
    return (list(kpts) if kpts else None), list(prov)


def _annotate_continuations(delta, proj):
    """ΔE 表行按各构型 manifest 的续算历史追加"续算×N"备注(attempts 里 result=continued)。"""
    from vcstudio.shared import manifest as _mm
    configs = ((proj.get('members') or {}).get('configs')) or []
    by_name = {os.path.basename(os.path.normpath(str(d))): d for d in configs if d}
    for r in (delta.get('rows') or []):
        d = by_name.get(r.get('name'))
        if not d:
            continue
        m = _mm.load_manifest(d)
        if not m:
            continue
        n = sum(1 for a in (m.get('attempts') or []) if a.get('result') == 'continued')
        if n:
            tag = f'续算×{n}'
            r['note'] = (str(r['note']) + '；' + tag) if r.get('note') else tag


def _default_origin(specs, out_dir, opju_path=None):
    from vcstudio.external import origin_charts
    return origin_charts.render_charts(specs, out_dir, opju_path=opju_path)
