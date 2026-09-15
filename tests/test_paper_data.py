"""论文数据表抽取 → 参考集 → 确定性对照 测试。

全假件:零真实网络(transport 注入)。硬护栏落到断言:LLM 只誊抄(越界值拒绝 / 缺页码留 null /
同表去重 / 体系非空 / 值非数弃行);归一(体系 M@N4 / 物种对齐库 / 量纲派生);对照纯确定性
(MAE/RMSE 手算对拍 / worst 前三 / unmatched 双向)。
"""
import json
import math

from vcstudio.project import paper_data as pd


def _reply(obj):
    """把一个 dict 包成 OpenAI 兼容响应 (200, bytes)。"""
    return 200, json.dumps({'choices': [{'message': {'content': json.dumps(obj)}}]}).encode()


def _extract(reply_obj, **kw):
    return pd.extract_data_tables('paper text with a Table 3 of adsorption energies ...',
                                  transport=lambda *a: _reply(reply_obj),
                                  api_key='k', **kw)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. LLM 抽取 + 确定性校验层(假 transport)
# ═══════════════════════════════════════════════════════════════════════════════
def test_extract_data_tables_legal_json_parsed():
    obj = {'tables': [{'label': '表3-2', 'kind': 'adsorption energy', 'columns': ['体系', 'E'],
                       'rows': [{'system': 'Fe-N4', 'species': 'Li2S4',
                                 'value_ev': '−1.23', 'page_hint': 'p5'}],
                       'origin_snippet': 'Table 3-2 ...'}]}
    out = _extract(obj)
    assert out['ok'] is True and len(out['tables']) == 1
    t = out['tables'][0]
    assert t['label'] == '表3-2' and t['kind'] == 'adsorption_energy'      # kind 归一
    r = t['rows'][0]
    assert r['system'] == 'Fe-N4' and r['species'] == 'Li2S4'
    assert isinstance(r['value_ev'], float) and abs(r['value_ev'] + 1.23) < 1e-9  # '−1.23'→-1.23
    assert r['page_hint'] == 'p5'


def test_extract_data_tables_rejects_out_of_range():
    obj = {'tables': [{'label': 'T', 'kind': 'adsorption_energy', 'columns': [],
                       'rows': [{'system': 'Fe-N4', 'species': 'X', 'value_ev': 99.0, 'page_hint': 'p'},
                                {'system': 'Co-N4', 'species': 'X', 'value_ev': -1.5, 'page_hint': 'p'}]}]}
    out = _extract(obj)
    rows = out['tables'][0]['rows']
    assert [r['system'] for r in rows] == ['Co-N4']                        # 99 eV 越界被拒
    assert any('超出合理值域' in d['reason'] for d in out['tables'][0]['dropped'])


def test_extract_data_tables_missing_page_kept_null():
    obj = {'tables': [{'label': 'T', 'kind': 'barrier', 'columns': [],
                       'rows': [{'system': 'Ni-N4', 'species': 'H', 'value_ev': 0.8}]}]}
    out = _extract(obj)
    r = out['tables'][0]['rows'][0]
    assert r['page_hint'] is None                                          # 缺页码 → null,不臆造
    assert r['value_ev'] == 0.8


def test_extract_data_tables_dedups_identical_rows():
    dup = {'system': 'Fe-N4', 'species': 'Li2S4', 'value_ev': -1.2, 'page_hint': 'p1'}
    obj = {'tables': [{'label': 'T', 'kind': 'adsorption_energy', 'columns': [],
                       'rows': [dict(dup), dict(dup), dict(dup)]}]}
    out = _extract(obj)
    assert len(out['tables'][0]['rows']) == 1                              # 同表重复只留一
    assert sum(1 for d in out['tables'][0]['dropped'] if d['reason'] == '重复行') == 2


def test_extract_data_tables_empty_system_and_nonnumber_dropped():
    obj = {'tables': [{'label': 'T', 'kind': 'other', 'columns': [],
                       'rows': [{'system': '', 'species': 'X', 'value_ev': 1.0, 'page_hint': 'p'},
                                {'system': 'Fe-N4', 'species': 'X', 'value_ev': 'n/a', 'page_hint': 'p'},
                                {'system': 'Co-N4', 'species': 'X', 'value_ev': -0.5, 'page_hint': 'p'}]}]}
    out = _extract(obj)
    assert [r['system'] for r in out['tables'][0]['rows']] == ['Co-N4']
    reasons = {d['reason'] for d in out['tables'][0]['dropped']}
    assert '体系名为空' in reasons and '值非数值' in reasons


def test_extract_data_tables_unknown_kind_becomes_other():
    obj = {'tables': [{'label': 'T', 'kind': 'magic_metric', 'columns': [],
                       'rows': [{'system': 'Fe-N4', 'species': 'X', 'value_ev': 1.0, 'page_hint': 'p'}]}]}
    assert _extract(obj)['tables'][0]['kind'] == 'other'


def test_extract_data_tables_no_key_degrades(monkeypatch):
    monkeypatch.setattr(pd.ai_paper.ai_analysis, 'load_api_key', lambda: None)   # 隔离 keyring
    out = pd.extract_data_tables('text', transport=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert out['ok'] is False and 'API key' in out['error'] and out['tables'] == []


def test_extract_tables_prompt_discipline():
    prompt = pd.build_extract_tables_prompt('body with Table 1')
    for guard in ('COPY ONLY', 'never', 'null'):
        assert guard.lower() in prompt.lower()
    # 结构契约放末尾(recency):shape 出现在正文之后
    assert prompt.index('EXACTLY this shape') > prompt.index('PAPER TEXT')
    for guard in ('FORBIDDEN', 'verbatim', 'single JSON object'):
        assert guard in pd.EXTRACT_TABLES_SYSTEM_PROMPT


def test_extract_tables_prompt_truncates_long_text():
    prompt = pd.build_extract_tables_prompt('x' * 50000)
    assert '…[truncated]' in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# 2. build_reference_dataset(归一)
# ═══════════════════════════════════════════════════════════════════════════════
def test_canon_system_variants_to_at_style():
    for raw in ('Fe-N4', 'FeN4', 'Fe@N4', 'Fe/N4', 'Fe@MN4', 'Fe N4', 'Fe_N4'):
        assert pd._canon_system(raw) == 'Fe@N4'
    assert pd._canon_system('Co@MP1N3') == 'Co@P1N3'
    assert pd._canon_system('graphene') == 'graphene'                      # 无金属前缀 → 原样


def test_canon_species_aligns_to_molecule_lib():
    assert pd._canon_species('li2s4*') == 'Li2S4'
    assert pd._canon_species('*S8') == 'S8'
    assert pd._canon_species('WeirdMol') == 'WeirdMol'                     # 库外原样
    assert pd._canon_species(None) is None


def test_build_reference_quantity_from_kind():
    tables = [{'label': 'a', 'kind': 'adsorption_energy',
               'rows': [{'system': 'Fe-N4', 'species': 'Li2S4', 'value_ev': -1.2}]},
              {'label': 'b', 'kind': 'u_l',
               'rows': [{'system': 'Co-N4', 'species': None, 'value_ev': 0.7}]},
              {'label': 'c', 'kind': 'free_energy',
               'rows': [{'system': 'Ni-N4', 'species': 'OOH', 'value_ev': 1.1}]},
              {'label': 'd', 'kind': 'barrier',
               'rows': [{'system': 'Mn-N4', 'species': 'TS', 'value_ev': 0.9}]}]
    ref = pd.build_reference_dataset(tables)
    got = {e['system']: e['quantity'] for e in ref['entries']}
    assert got == {'Fe@N4': 'E_ads', 'Co@N4': 'U_L', 'Ni@N4': 'dG', 'Mn@N4': 'Ea'}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. compare_with_computed(纯确定性,手算对拍)
# ═══════════════════════════════════════════════════════════════════════════════
def _ref(entries):
    return {'entries': entries}


def test_compare_mae_rmse_handcalc():
    ref = _ref([{'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': -1.00},
                {'system': 'Co@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': -2.00}])
    computed = [{'system': 'Fe-N4', 'species': 'li2s4', 'quantity': 'E_ads', 'value': -1.30},
                {'system': 'CoN4', 'species': 'Li2S4', 'quantity': 'E_ads', 'value': -1.60}]
    cmp = pd.compare_with_computed(ref, computed)
    assert cmp['n'] == 2
    # |−0.30| 与 |+0.40| → MAE=0.35;RMSE=sqrt((0.09+0.16)/2)=sqrt(0.125)
    assert abs(cmp['mae'] - 0.35) < 1e-9
    assert abs(cmp['rmse'] - math.sqrt(0.125)) < 1e-9
    # 命名不同写法也能对齐(Fe-N4/CoN4/li2s4)
    assert {p['system'] for p in cmp['pairs']} == {'Fe@N4', 'Co@N4'}


def test_compare_worst_top3_sorted():
    entries, computed = [], []
    for i, d in enumerate((0.1, 0.5, 0.9, 0.3, 0.7)):
        entries.append({'system': f'Fe@N{i}', 'species': 'X', 'quantity': 'E_ads', 'ref_value': 0.0})
        computed.append({'system': f'Fe@N{i}', 'species': 'X', 'quantity': 'E_ads', 'value': d})
    cmp = pd.compare_with_computed(_ref(entries), computed)
    assert [round(w['abs_delta'], 1) for w in cmp['worst']] == [0.9, 0.7, 0.5]  # 前三


def test_compare_unmatched_both_sides():
    ref = _ref([{'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': -1.0},
                {'system': 'Zn@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': -0.2}])
    computed = [{'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'value': -1.1},
                {'system': 'Cu@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'value': -0.9}]
    cmp = pd.compare_with_computed(ref, computed)
    assert cmp['n'] == 1
    reasons = {(u['system'], u['reason']) for u in cmp['unmatched']}
    assert ('Zn@N4', '计算侧无对齐项') in reasons
    assert ('Cu@N4', '文献侧无对齐项') in reasons


def test_compare_empty_no_pairs():
    cmp = pd.compare_with_computed(_ref([]), [])
    assert cmp['n'] == 0 and cmp['mae'] is None and cmp['rmse'] is None
    assert '无可比对项' in cmp['summary_zh']


def test_mae_report_md_format():
    ref = _ref([{'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': -1.00}])
    cmp = pd.compare_with_computed(ref, [{'system': 'Fe@N4', 'species': 'Li2S4',
                                          'quantity': 'E_ads', 'value': -1.10}])
    m = pd.mae_report_md(cmp)
    assert '## 文献对照' in m
    for col in ('体系', '物种', '文献值/eV', '计算值/eV', '差/eV'):
        assert col in m
    assert '| Fe@N4 | Li2S4 | -1.000 | -1.100 | -0.100 |' in m
