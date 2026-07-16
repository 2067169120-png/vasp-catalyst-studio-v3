"""examples/offline_analysis 冒烟:离线跑通 诊断→ΔE→出图→报告,产物齐全、数值合理。"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, 'examples', 'offline_analysis', 'run_demo.py')


def _load_demo():
    spec = importlib.util.spec_from_file_location('offline_run_demo', DEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_runs_offline_and_emits_artifacts(tmp_path):
    demo = _load_demo()
    res = demo.main(str(tmp_path / 'out'))

    # 1) 诊断:4 CONVERGED/DONE + 1 ZBRENT/UNCONVERGED(可续算)
    diags = res['diagnoses']
    zb = diags['ads_Li2S8_zbrent']
    assert zb.failure_class == 'ZBRENT' and zb.state == 'UNCONVERGED' and zb.restartable
    n_done = sum(1 for d in diags.values() if d.state == 'DONE')
    assert n_done == 4

    # 2) ΔE 门控:两个 DONE 组态给数;未完成组态 ΔE 留空
    rows = {r['name']: r for r in res['summary']['rows']}
    assert rows['ads_Li2S4']['delta_e'] == pytest.approx(-2.5, abs=1e-6)
    assert rows['ads_Li2S6']['delta_e'] == pytest.approx(-1.7, abs=1e-6)
    assert rows['ads_Li2S8_zbrent']['delta_e'] is None  # 门控:未完成不给数

    # 3) 出图:至少一个图文件落盘(matplotlib PNG 或 SVG 兜底)
    assert res['charts'] and all(os.path.isfile(f) for f in res['charts'])

    # 4) HTML 报告:自包含且含关键章节
    assert os.path.isfile(res['report'])
    h = open(res['report'], encoding='utf-8').read()
    assert 'ΔE 汇总统计' in h
    assert '吸附能 ΔE' in h
    assert 'ZBRENT' in h                      # 问题作业带诊断证据
    assert '方法学约定' in h
