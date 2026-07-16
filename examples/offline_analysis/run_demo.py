"""离线科学演示:无 VASP / 无集群,验证 vcstudio 的分析链路。

审稿人/新用户不必装 VASP、不必连集群,即可端到端验证本工具的**分析半程**:

    诊断分类  →  ΔE 汇总(门控)  →  出柱状图  →  HTML 报告

数据来自 ``jobs/`` 下一组**合成的"已完成"最小作业集**(job.yaml + CONTCAR +
OSZICAR/OUTCAR,均为合成样本,非真实 VASP 产物,见 README)。每步都打印
**实际结果**与**期望**,便于对照确认链路行为无误。产物落 ``./demo_out/``。

用法::

    python examples/offline_analysis/run_demo.py            # 默认输出 ./demo_out
    python examples/offline_analysis/run_demo.py /tmp/out   # 指定输出目录

Offline analysis demo — reviewers without VASP or a cluster can verify the
analysis half of the pipeline end-to-end (diagnosis → gated ΔE → bar chart →
HTML report). Inputs are synthetic "completed" jobs under ``jobs/``.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, 'jobs')
# 让脚本可直接 `python examples/offline_analysis/run_demo.py` 运行(无需先 pip install)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from vcstudio.cluster.diagnose import classify  # noqa: E402
from vcstudio.project import adsorption, freeenergy  # noqa: E402

CONFIG_MEMBERS = ('ads_Li2S4', 'ads_Li2S6', 'ads_Li2S8_zbrent')


def build_project():
    """按绝对路径组装 proj dict(不依赖 project.yaml 的存储路径,便于任意机器直接跑)。"""
    return {
        'name': 'Li-S SAC demo (offline · synthetic)',
        'root': JOBS,
        'members': {
            'clean_slab': os.path.join(JOBS, 'slab_clean'),
            'gas_ref': os.path.join(JOBS, 'ref_S8'),
            'configs': [os.path.join(JOBS, m) for m in CONFIG_MEMBERS],
        },
    }


def _gather_evidence(job_dir):
    """从本地 OUTCAR/OSZICAR 取证 → diagnose.classify(离线,纯文本)。"""
    def _read(name):
        p = os.path.join(job_dir, name)
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return ''
    outcar, oszicar = _read('OUTCAR'), _read('OSZICAR')
    return classify(
        converged='reached required accuracy' in outcar,
        energy=freeenergy.read_e0(job_dir),
        outcar_size=len(outcar.encode('utf-8')),
        oszicar_size=len(oszicar.encode('utf-8')),
        log_tail=outcar[-4000:],
        oszicar_tail=oszicar[-4000:],
        clean_exit='General timing' in outcar,
    )


def _p(line=''):
    print(line, flush=True)


def step_diagnose(proj):
    """步骤 1:逐作业失败分类(CONVERGED/ZBRENT/…)。"""
    _p('━━ 步骤 1/4 · 诊断分类(离线取证 → diagnose.classify) ━━')
    _p('  期望:4 个作业 CONVERGED→DONE;ads_Li2S8_zbrent 命中 ZBRENT→UNCONVERGED 且可续算(♻)。')
    mem = proj['members']
    dirs = [mem['clean_slab'], mem['gas_ref']] + list(mem['configs'])
    results = {}
    for d in dirs:
        dg = _gather_evidence(d)
        results[os.path.basename(d)] = dg
        flag = ' ♻可续算' if dg.restartable else ''
        _p(f'    {os.path.basename(d):20s} → {dg.failure_class:10s} '
           f'[{dg.state}]{flag}')
    return results


def step_delta_e(proj):
    """步骤 2:ΔE 汇总 + 门控(任一成员未 DONE 则该行不给数)。"""
    _p('')
    _p('━━ 步骤 2/4 · ΔE 汇总(adsorption.delta_e_rows,含门控) ━━')
    _p('  期望:Li2S4≈-2.50 eV、Li2S6≈-1.70 eV 给出;Li2S8 未完成 → ΔE 留空并注明缺谁。')
    summary = adsorption.delta_e_rows(proj)
    ss, es = summary['slab']
    rs, er = summary['ref']
    _p(f'    E(slab)={es} eV [{ss}]   E(ref)={er} eV [{rs}]')
    for r in summary['rows']:
        de = f'{r["delta_e"]:+.3f} eV' if isinstance(r['delta_e'], float) else '（未给·门控)'
        note = f'  — {r["note"]}' if r['note'] else ''
        _p(f'    {r["name"]:20s} [{r["state"]:11s}] ΔE = {de}{note}')
    return summary


def step_bar_chart(summary, out_dir):
    """步骤 3:吸附能柱状图。matplotlib 可用 → 论文级 PNG;否则 → 纯 SVG 兜底。"""
    _p('')
    _p('━━ 步骤 3/4 · 出柱状图(matplotlib 可用则论文级 PNG,否则 SVG 兜底) ━━')
    done = [(r['name'], r['delta_e']) for r in summary['rows']
            if isinstance(r['delta_e'], float)]
    _p(f'  期望:仅对已完成的 {len(done)} 个组态出图(未完成的被门控排除)。')
    if not done:
        _p('    无已完成组态,跳过出图。')
        return []
    try:
        from vcstudio.external import native_charts
        data = {'adsorbates': [_short_label(n) for n, _ in done],
                'substrates': {'demo SAC': [d for _, d in done]}}
        files = native_charts.adsorption_bar(
            data, os.path.join(out_dir, 'adsorption_bar.png'),
            ylabel=r'$\Delta E$ (eV)', negative_up=True, formats=('png',))
        _p(f'    引擎:matplotlib(论文级)。产物:{", ".join(os.path.basename(f) for f in files)}')
        return files
    except ImportError:
        from vcstudio.project import charts
        bar = charts.bar_data_from_delta('demo SAC',
                                         [{'name': n, 'delta_e': d} for n, d in done])
        svg = charts.render_bar_svg(bar, title='吸附能 ΔE(离线演示)')
        out = os.path.join(out_dir, 'adsorption_bar.svg')
        with open(out, 'w', encoding='utf-8') as f:
            f.write(svg)
        _p(f'    引擎:纯 SVG 兜底(未装 matplotlib)。产物:{os.path.basename(out)}')
        return [out]


def _short_label(name):
    """组态目录名 → 短标签(剥 ads_ 前缀,去 _zbrent 等后缀噪声)。"""
    n = name
    if n.startswith('ads_'):
        n = n[4:]
    return n.split('_')[0]


def step_report(proj, out_dir):
    """步骤 4:完整 HTML 报告(离线:Origin 停用走 SVG 兜底;AI 停用给提示)。"""
    _p('')
    _p('━━ 步骤 4/4 · HTML 报告(report_full,完全离线) ━━')
    _p('  期望:生成自包含 HTML(状态卡片 + 问题作业 + ΔE 表 + 内嵌 SVG 图 + 方法学约定);'
       'Origin/AI 均离线降级,报告仍完整产出。')
    from vcstudio.project import report_full

    def offline_origin(specs, out_dir_, opju_path=None):
        # 离线演示不外呼 Origin:返回失败 → report_full 自动改用内嵌 SVG
        return {'ok': False, 'images': {}, 'error': '离线演示:Origin 未启用(改用内嵌 SVG)'}

    def offline_ai(payload):
        # 离线演示不调用 LLM:如实说明(报告 AI 章节将显示"未运行")
        return {'ok': False, 'error': '离线演示:未启用 LLM 分析层(核心分析链路无需联网)'}

    out_path = os.path.join(out_dir, 'report.html')
    report_full.generate_project_report(
        proj, out_path, config={}, origin_render=offline_origin,
        ai_analyze=offline_ai, log=lambda s: None)
    _p(f'    产物:{os.path.basename(out_path)}')
    return out_path


def main(out_dir=None):
    out_dir = os.path.abspath(out_dir or os.path.join(os.getcwd(), 'demo_out'))
    os.makedirs(out_dir, exist_ok=True)
    _p('╭─ vcstudio 离线科学演示 ─────────────────────────────────────────╮')
    _p(f'│ 输入:{JOBS}')
    _p(f'│ 输出:{out_dir}')
    _p('│ 说明:无 VASP / 无集群,验证 诊断→ΔE→出图→报告 分析链路。')
    _p('╰──────────────────────────────────────────────────────────────╯')
    _p('')
    diagnoses = step_diagnose(build_project())
    proj = build_project()
    summary = step_delta_e(proj)
    charts = step_bar_chart(summary, out_dir)
    report = step_report(proj, out_dir)
    _p('')
    _p('✔ 全部 4 步完成。打开 HTML 报告即可查看:')
    _p(f'    {report}')
    return {'out_dir': out_dir, 'diagnoses': diagnoses, 'summary': summary,
            'charts': charts, 'report': report}


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
