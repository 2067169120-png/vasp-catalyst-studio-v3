"""批量运行报告:扫所有 job.yaml → 状态分布 + 失败目录 + 能量表 → 自包含 HTML/Markdown。

设计(缺口分析 P1「报告那一半」+ 交接 §6 M4):
- **零依赖**:纯 Python + stdlib(html/内联 CSS),不引 matplotlib/openpyxl/docx,守 EXE 零增重。
- **单一真相源**:只从各作业 job.yaml 聚合(不引 SQLite/并行状态存);拿不到就如实标注。
- **对齐论文最终摘要**:失败按 diagnose 的分类学分桶,失败/需人工作业带诊断证据;
  可选嵌入吸附能 ΔE 表(与 adsorption.export_csv 互补,一份可读的总览)。
纯函数(collect/summarize/render)可离线测;write_report 才落盘。中文注释允许,英文标识符。
"""
from __future__ import annotations

import html
import os
import time
from pathlib import Path

from vcstudio.shared import manifest as manifest_mod

# 状态 → (中文标签, 颜色)——与 GUI 任务页 _STATE_TAG 同色系
_STATE_META = {
    'CREATED': ('未提交', '#4b5563'), 'UPLOADED': ('已上传', '#1d4ed8'),
    'SUBMITTED': ('已提交', '#7e22ce'), 'QUEUED': ('排队', '#7e22ce'),
    'RUNNING': ('运行中', '#1d4ed8'), 'DONE': ('完成', '#15803d'),
    'FAILED': ('失败', '#b91c1c'), 'UNCONVERGED': ('未收敛', '#a16207'),
    'NEEDS_HUMAN': ('需人工', '#a16207'),
}
_PROBLEM_STATES = ('FAILED', 'NEEDS_HUMAN', 'UNCONVERGED')


def collect_jobs(job_dirs) -> list:
    """读一批作业目录的 job.yaml → 每作业一条摘要 dict。缺 manifest 也留一条(标注)。"""
    rows = []
    for d in job_dirs:
        name = os.path.basename(os.path.normpath(str(d)))
        m = manifest_mod.load_manifest(d)
        if m is None:
            rows.append({'name': name, 'dir': str(d), 'state': '缺 job.yaml',
                         'system': '', 'calc': '', 'cluster': '', 'job_id': '',
                         'energy': None, 'failure_class': '', 'evidence': '',
                         'restartable': False, 'updated': ''})
            continue
        res = m.get('results') or {}
        diag = res.get('diagnosis') or {}
        hist = m.get('state_history') or []
        rows.append({
            'name': name, 'dir': str(d),
            'state': m.get('state', '?'),
            'system': m.get('system', ''),
            'calc': f"{m.get('task_type','')}/{m.get('calc_type','')}".strip('/'),
            'cluster': m.get('cluster') or '',
            'job_id': m.get('scheduler_job_id') or '',
            'energy': res.get('energy_e0_eV'),
            'failure_class': diag.get('failure_class', ''),
            'evidence': diag.get('evidence', ''),
            'restartable': bool(diag.get('restartable')),
            'updated': (hist[-1].get('at', '') if hist else m.get('created_at', '')),
        })
    return rows


def summarize(rows: list) -> dict:
    """聚合:总数 + 按状态计数 + 按失败分类计数 + 问题作业清单。"""
    by_state, by_class = {}, {}
    problems = []
    for r in rows:
        by_state[r['state']] = by_state.get(r['state'], 0) + 1
        if r['state'] in _PROBLEM_STATES:
            problems.append(r)
            fc = r.get('failure_class') or '(未分类)'
            by_class[fc] = by_class.get(fc, 0) + 1
    return {
        'total': len(rows),
        'by_state': by_state,
        'by_class': by_class,
        'n_done': by_state.get('DONE', 0),
        'n_problem': len(problems),
        'problems': problems,
    }


def _fmt_energy(e) -> str:
    return f'{e:.6f}' if isinstance(e, (int, float)) else ''


# ── Markdown(轻量,便于粘进笔记/论文草稿) ─────────────────────────────────────
def render_markdown(rows: list, summary: dict, *, title: str = 'VASP 批量运行报告') -> str:
    L = [f'# {title}', '', f'生成时间:{time.strftime("%Y-%m-%d %H:%M:%S")}　'
         f'作业数:{summary["total"]}　完成:{summary["n_done"]}　问题:{summary["n_problem"]}', '']
    L.append('## 状态分布')
    for st, n in sorted(summary['by_state'].items()):
        label = _STATE_META.get(st, (st, ''))[0]
        L.append(f'- {st}（{label}）: {n}')
    if summary['by_class']:
        L += ['', '## 失败分类']
        for fc, n in sorted(summary['by_class'].items(), key=lambda kv: -kv[1]):
            L.append(f'- {fc}: {n}')
    if summary['problems']:
        L += ['', '## 问题作业（需处理）', '', '| 作业 | 状态 | 失败分类 | 可续算 | 证据 |',
              '|---|---|---|---|---|']
        for r in summary['problems']:
            L.append(f"| {r['name']} | {r['state']} | {r['failure_class']} | "
                     f"{'是' if r['restartable'] else '否'} | {r['evidence']} |")
    L += ['', '## 全部作业', '', '| 作业 | 体系 | 类型 | 状态 | 集群 | 作业号 | E0 (eV) | 更新 |',
          '|---|---|---|---|---|---|---|---|']
    for r in rows:
        L.append(f"| {r['name']} | {r['system']} | {r['calc']} | {r['state']} | "
                 f"{r['cluster']} | {r['job_id']} | {_fmt_energy(r['energy'])} | {r['updated']} |")
    return '\n'.join(L) + '\n'


# ── 自包含 HTML(单文件,内联 CSS,浏览器直接开;零外链零依赖) ──────────────────
def render_html(rows: list, summary: dict, *, title: str = 'VASP 批量运行报告',
                delta_e=None, extra_html: str = '') -> str:
    """delta_e:可选,ΔE 表;extra_html:可选,插在 ΔE 表后的附加章节
    (图表/结构图/AI 分析,由 report_full 组装,内容需自行转义)。"""
    def esc(x):
        return html.escape(str(x if x is not None else ''))

    def chip(st):
        label, color = _STATE_META.get(st, (st, '#4b5563'))
        return (f'<span class="chip" style="background:{color}1a;color:{color};'
                f'border:1px solid {color}55">{esc(st)}·{esc(label)}</span>')

    cards = ''.join(
        f'<div class="card"><div class="n">{n}</div>'
        f'<div class="l">{chip(st)}</div></div>'
        for st, n in sorted(summary['by_state'].items(), key=lambda kv: -kv[1]))

    prob_rows = ''.join(
        f'<tr><td>{esc(r["name"])}</td><td>{chip(r["state"])}</td>'
        f'<td><code>{esc(r["failure_class"])}</code></td>'
        f'<td>{"✅ 可续算" if r["restartable"] else "—"}</td>'
        f'<td class="ev">{esc(r["evidence"])}</td></tr>'
        for r in summary['problems'])
    prob_section = (
        f'<h2>问题作业（{summary["n_problem"]}）</h2>'
        f'<table><thead><tr><th>作业</th><th>状态</th><th>失败分类</th>'
        f'<th>可续算</th><th>证据</th></tr></thead><tbody>{prob_rows}</tbody></table>'
        if summary['problems'] else '<h2>问题作业</h2><p class="ok">无 — 全部作业均无失败/需人工。</p>')

    all_rows = ''.join(
        f'<tr><td>{esc(r["name"])}</td><td>{esc(r["system"])}</td>'
        f'<td>{esc(r["calc"])}</td><td>{chip(r["state"])}</td>'
        f'<td>{esc(r["cluster"])}</td><td>{esc(r["job_id"])}</td>'
        f'<td class="num">{esc(_fmt_energy(r["energy"]))}</td>'
        f'<td class="dim">{esc(r["updated"])}</td></tr>'
        for r in rows)

    def de_color(v):
        """ΔE 颜色语义(原版口径):< -3 强吸附(绿)、> 0 不利(红)、其余默认。"""
        if not isinstance(v, (int, float)):
            return ''
        if v > 0:
            return 'color:#b91c1c;font-weight:600'
        if v < -3:
            return 'color:#15803d;font-weight:600'
        return ''

    de_section = ''
    if delta_e and delta_e.get('rows'):
        de_rows = ''.join(
            f'<tr><td>{esc(r.get("name"))}</td><td>{chip(r.get("state",""))}</td>'
            f'<td class="num">{esc(_fmt_energy(r.get("e_config")))}</td>'
            f'<td class="num" style="{de_color(r.get("delta_e"))}">'
            f'{esc(_fmt_energy(r.get("delta_e")))}</td>'
            f'<td class="dim">{esc(r.get("note",""))}</td></tr>'
            for r in delta_e['rows'])
        de_section = (
            f'<h2>吸附能 ΔE</h2><table><thead><tr><th>构型</th><th>状态</th>'
            f'<th>E(slab+ads)/eV</th><th>ΔE/eV</th><th>备注</th></tr></thead>'
            f'<tbody>{de_rows}</tbody></table>')

    return _HTML_SHELL.format(
        title=esc(title),
        meta=f'生成时间 {time.strftime("%Y-%m-%d %H:%M:%S")}　·　作业 {summary["total"]}'
             f'　·　完成 {summary["n_done"]}　·　问题 {summary["n_problem"]}',
        cards=cards, problems=prob_section,
        delta_e=de_section + (extra_html or ''), all_rows=all_rows)


_HTML_SHELL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
color:#1f2937;background:#f8fafc;padding:24px}}
h1{{font-size:20px;margin:0 0 2px}} h2{{font-size:15px;margin:26px 0 8px;color:#374151}}
.meta{{color:#6b7280;font-size:12px;margin-bottom:18px}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;min-width:96px}}
.card .n{{font-size:24px;font-weight:700}}
.chip{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;
border-radius:10px;overflow:hidden;font-size:13px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #f1f5f9}}
th{{background:#f8fafc;font-weight:600;color:#475569}}
tr:last-child td{{border-bottom:none}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.dim{{color:#94a3b8;font-size:12px}} .ev{{color:#475569;font-size:12px}}
.ok{{color:#15803d}} code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}}
.wrap{{overflow-x:auto}}
</style></head><body>
<h1>{title}</h1><div class="meta">{meta}</div>
<div class="cards">{cards}</div>
{problems}
{delta_e}
<h2>全部作业</h2><div class="wrap"><table><thead><tr><th>作业</th><th>体系</th><th>类型</th>
<th>状态</th><th>集群</th><th>作业号</th><th>E0 (eV)</th><th>更新</th></tr></thead>
<tbody>{all_rows}</tbody></table></div>
</body></html>"""


def write_report(job_dirs, out_path, *, title: str = 'VASP 批量运行报告',
                 fmt: str = 'html', delta_e=None) -> Path:
    """扫 job_dirs 生成报告并落盘。fmt ∈ {'html','md'}。返回写入路径。"""
    rows = collect_jobs(job_dirs)
    summary = summarize(rows)
    if fmt == 'md':
        text = render_markdown(rows, summary, title=title)
    else:
        text = render_html(rows, summary, title=title, delta_e=delta_e)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    return out
