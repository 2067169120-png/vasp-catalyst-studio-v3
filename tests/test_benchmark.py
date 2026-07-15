"""吸附能验证基准:CSV 载入 / MAE 对比 / Markdown 渲染。"""
import pytest

from vcstudio.project import benchmark


def test_load_csv_skips_comments(tmp_path):
    p = tmp_path / 'ref.csv'
    p.write_text('# name,energy_eV\nCo_S8,-1.23\nV_Li2S6,-2.5\n', encoding='utf-8')
    d = benchmark.load_csv(str(p))
    assert d == {'Co_S8': -1.23, 'V_Li2S6': -2.5}


def test_compare_reports_mae_and_missing():
    computed = {'a': -1.0, 'b': -2.0}
    reference = {'a': -1.2, 'b': -1.8, 'c': -3.0}
    r = benchmark.compare_to_reference(computed, reference)
    assert r['n'] == 2
    assert r['mae'] == pytest.approx(0.2)
    assert r['missing'] == ['c']
    errs = {row['name']: row['error'] for row in r['rows']}
    assert errs['a'] == pytest.approx(0.2)


def test_compare_empty_overlap_raises():
    with pytest.raises(ValueError):
        benchmark.compare_to_reference({'x': 1.0}, {'y': 2.0})


def test_render_markdown_table():
    r = benchmark.compare_to_reference({'a': -1.0}, {'a': -1.2})
    md = benchmark.render_markdown(r)
    assert '| a |' in md and 'MAE' in md
