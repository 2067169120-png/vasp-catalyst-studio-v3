"""研究场景系统测试:schema 完备性、显隐判定、advisor 过滤、导入导出往返、config 读写。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from vcstudio.project import advisor as advisor_mod
from vcstudio.project import reactions as reactions_mod
from vcstudio.shared import scenarios as S

# 四个主模式 + 三个专业预设
_EXPECTED_KEYS = {'lis', 'vasp', 'electrocat', 'thermocat',
                  'battery_bulk', 'molecular', 'full'}


def _real_advisor_rules() -> set:
    """从 advisor.py 源码解析真实规则名(advise() 里 out.append(('Pn','NAME',...)))。"""
    src = Path(advisor_mod.__file__).read_text(encoding='utf-8')
    return set(re.findall(r"out\.append\(\(\s*'P\d',\s*'([A-Z0-9_]+)'", src))


# ── 注册表与 schema 完备性 ────────────────────────────────────────────────────

def test_builtin_has_primary_modes_and_professional_presets():
    keys = {s['key'] for s in S.list_scenarios()}
    assert keys == _EXPECTED_KEYS
    assert set(S.BUILTIN_KEYS) == _EXPECTED_KEYS


def test_all_builtin_pass_schema_validation():
    for sc in S.list_scenarios():
        assert S.validate_scenario(sc) == [], sc['key']


def test_every_builtin_has_all_required_keys():
    for sc in S.list_scenarios():
        for k in S._REQUIRED_KEYS:
            assert k in sc, (sc['key'], k)
        assert sc['ai_context'].strip()          # 领域上下文非空


def test_pages_are_subset_of_valid_pages():
    for sc in S.list_scenarios():
        assert sc['pages'], sc['key']
        assert set(sc['pages']) <= set(S.PAGES), sc['key']


def test_page_registry_matches_real_html_pages():
    html = (Path(__file__).parents[1] / 'vcstudio/gui_web/assets/index.html').read_text(
        encoding='utf-8')
    assert set(re.findall(r'<section[^>]+data-page="([^"]+)"', html)) == set(S.PAGES)
    nav = set(re.findall(r'<a[^>]+data-page="([^"]+)"[^>]+data-scene="pages\.([^"]+)"', html))
    # AI 已从一级页导航改为右侧上下文助手；其余物理页仍需有
    # data-page -> pages.<page> 能力适配，不得绕过工作模式白名单。
    assert {a for a, b in nav if a == b and a != 'ai'} == set(S.PAGES) - {'ai'}
    assert re.search(
        r'id="assistant-toggle"[^>]*data-scene="pages\.ai"[^>]*aria-controls="page-ai"',
        html)
    assert re.search(
        r'<section[^>]+data-page="ai"[^>]+id="page-ai"[^>]+data-shell-assistant',
        html)


def test_four_primary_modes_have_safe_defaults():
    primary = {s['key']: s for s in S.list_scenarios() if s['primary']}
    assert set(primary) == {'lis', 'vasp', 'molecular', 'full'}
    assert primary['lis']['defaults']['landing_page'] == 'project'
    assert primary['vasp']['defaults']['landing_page'] == 'generate'
    assert primary['molecular']['defaults']['engine'] == 'gaussian'
    assert set(primary['full']['pages']) == set(S.PAGES)
    assert set(primary['full']['engines']) == set(S.ENGINES)


def test_molecular_drops_project_page():
    # 分子化学不走表面吸附项目页(pages 子集)
    mol = S.get_scenario('molecular')
    assert 'project' not in mol['pages']
    assert 'report-workbench' not in mol['pages']
    assert 'project' in S.get_scenario('full')['pages']


def test_project_scenarios_expose_the_report_workbench_page():
    for scenario in S.list_scenarios():
        if 'project' in scenario['pages']:
            assert 'report-workbench' in scenario['pages'], scenario['key']


def test_engines_are_valid():
    for sc in S.list_scenarios():
        assert set(sc['engines']) <= set(S.ENGINES), sc['key']
    assert 'gaussian' in S.get_scenario('molecular')['engines']   # 分子场景 Gaussian 可见


def test_figure_order_subset_of_figure_keys():
    for sc in S.list_scenarios():
        assert set(sc['figure_preset_order']) <= set(S.FIGURE_KEYS), sc['key']


def test_reaction_presets_subset_and_registry_in_sync():
    # 常量必须与 reactions 模块真实预设同步
    assert set(S.REACTION_PRESETS) == set(reactions_mod.list_presets())
    for sc in S.list_scenarios():
        assert set(sc['reaction_presets']) <= set(S.REACTION_PRESETS), sc['key']


def test_advisor_rules_real_and_in_sync():
    real = _real_advisor_rules()
    assert real, '未能从 advisor.py 解析出规则名'
    # 常量与 advisor 源码严格同步
    assert set(S.ADVISOR_RULES) == real
    # 每个场景 advisor_profile 引用的规则名都真实存在
    for sc in S.list_scenarios():
        prof = sc['advisor_profile']
        if prof == 'all':
            continue
        for rule in prof:
            assert rule in real, (sc['key'], rule)


def test_default_calc_types_are_legal():
    for sc in S.list_scenarios():
        ct = sc['defaults'].get('calc_type')
        assert ct in S.CALC_TYPES, (sc['key'], ct)
    assert S.get_scenario('battery_bulk')['defaults']['calc_type'] == 'bulk'
    assert S.get_scenario('molecular')['defaults']['calc_type'] == 'molecule'


# ── get_scenario / 拷贝语义 ───────────────────────────────────────────────────

def test_unknown_scenario_falls_back_to_full_with_warning():
    with pytest.warns(UserWarning):
        sc = S.get_scenario('does-not-exist')
    assert sc['key'] == 'full'


def test_get_scenario_returns_independent_copy():
    a = S.get_scenario('lis')
    a['pages'].append('HACKED')
    a['cards']['generate'] = {'sac_matrix': False}
    b = S.get_scenario('lis')
    assert 'HACKED' not in b['pages']
    assert 'generate' not in b['cards']


# ── is_visible 各层级 ─────────────────────────────────────────────────────────

def test_is_visible_pages_whitelist():
    mol = S.get_scenario('molecular')
    assert S.is_visible(mol, 'pages.generate') is True
    assert S.is_visible(mol, 'pages.project') is False


def test_is_visible_cards_default_true_when_unspecified():
    # full 未声明任何隐藏 → 卡片默认可见
    full = S.get_scenario('full')
    assert S.is_visible(full, 'cards.generate.sac_matrix') is True
    assert S.is_visible(full, 'cards.generate.spin_family') is True


def test_is_visible_cards_override_false():
    mol = S.get_scenario('molecular')
    assert S.is_visible(mol, 'cards.structure.sac_matrix') is False
    # 未被覆盖的卡片仍默认可见
    assert S.is_visible(mol, 'cards.generate.spin_family') is True


def test_is_visible_nested_figures_card():
    bat = S.get_scenario('battery_bulk')
    assert S.is_visible(bat, 'cards.project.figures.volcano') is False
    assert S.is_visible(bat, 'cards.project.figures.scaling') is False


def test_is_visible_engines_reactions_figures():
    ele = S.get_scenario('electrocat')
    assert S.is_visible(ele, 'reactions.ORR_4E') is True
    assert S.is_visible(ele, 'reactions.LIS_16E') is False       # 电催化不带 Li–S
    assert S.is_visible(ele, 'figures.volcano') is True
    thermo = S.get_scenario('thermocat')
    assert S.is_visible(thermo, 'reactions.ORR_4E') is False     # 热催化无电化学预设
    assert S.is_visible(S.get_scenario('molecular'), 'engines.gaussian') is True
    assert S.is_visible(S.get_scenario('full'), 'engines.gaussian') is True


def test_task_keys_are_in_sync_with_catalog_and_defaults_are_allowed():
    from vcstudio.generate import task_catalog
    assert set(S.TASK_KEYS) == {t['key'] for t in task_catalog.CATALOG}
    for sc in S.list_scenarios():
        assert set(sc['task_keys']) <= set(S.TASK_KEYS)
        assert sc['defaults']['active_calculation'] in sc['task_keys']
        assert set(sc['home_actions']) <= set(S.HOME_ACTIONS)


def test_lis_neb_needs_an_explicit_page_allowance_without_broadening_the_mode():
    """Li-S 的 NEB 只应临时放行生成页，不能把其他裁剪页面一并开放。"""
    lis = S.get_scenario('lis')
    assert 'neb' in lis['task_keys']
    assert 'generate' not in lis['pages']
    assert 'wavefunction' not in lis['pages']


def test_is_visible_unknown_path_fails_open():
    full = S.get_scenario('full')
    assert S.is_visible(full, 'totally.unknown.path') is True
    assert S.is_visible(full, '') is True


# ── apply_to_advisor ─────────────────────────────────────────────────────────

def test_apply_to_advisor_all_is_sentinel():
    prof = S.apply_to_advisor(S.get_scenario('full'))
    assert prof is S.ALL_RULES
    # 哨兵 __contains__ 恒真:任何名字都算启用(含将来新增规则)
    assert 'ENCUT_UNIFY' in prof
    assert 'FUTURE_RULE_NOT_YET_DEFINED' in prof
    # 且能覆盖当前全部真实规则
    for rule in _real_advisor_rules():
        assert rule in prof


def test_apply_to_advisor_subset_is_exact_frozenset():
    prof = S.apply_to_advisor(S.get_scenario('battery_bulk'))
    assert isinstance(prof, frozenset)
    assert prof is not S.ALL_RULES
    assert prof == frozenset(S.get_scenario('battery_bulk')['advisor_profile'])
    # slab 专属规则不在体相子集里
    assert 'VACUUM_TOO_THIN' not in prof
    assert 'ENCUT_UNIFY' in prof


# ── 导出 / 导入(社区分享单元) ───────────────────────────────────────────────

def test_export_import_roundtrip(tmp_path):
    original = S.get_scenario('electrocat')
    p = S.export_scenario(original, tmp_path / 'electrocat.yaml')
    assert p.is_file()
    res = S.import_scenario(p)
    assert res['ok'] is True
    assert res['issues'] == []
    assert res['scenario'] == original


def test_import_bad_file_reports_issues(tmp_path):
    bad = {
        'key': 'x', 'name': 'x', 'description': 'x',
        'pages': ['dashboard', 'nonsense_page'],            # 未知页面
        'cards': {'weird_page': {'a': True}},               # 未知页面卡片组
        'figure_preset_order': ['volcano', 'no_such_fig'],  # 未知图型
        'reaction_presets': ['ORR_4E', 'NOPE_RX'],          # 未知反应
        'advisor_profile': ['ENCUT_UNIFY', 'FAKE_RULE'],    # 未知规则
        'defaults': {'calc_type': 'plasma'},                # 非法计算类型
        'engines': ['vasp', 'qe'],                          # 未知引擎
        'ai_context': 'x',
    }
    p = tmp_path / 'bad.yaml'
    p.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding='utf-8')
    res = S.import_scenario(p)
    assert res['ok'] is False
    joined = ' | '.join(res['issues'])
    for token in ('nonsense_page', 'no_such_fig', 'NOPE_RX', 'FAKE_RULE', 'plasma', 'qe'):
        assert token in joined, token
    # 结构可读 → scenario 原样返回供检视
    assert res['scenario'] == bad


def test_import_missing_file_is_not_ok(tmp_path):
    res = S.import_scenario(tmp_path / 'nope.yaml')
    assert res['ok'] is False
    assert res['scenario'] is None
    assert res['issues']


def test_import_non_mapping_is_not_ok(tmp_path):
    p = tmp_path / 'list.yaml'
    p.write_text(yaml.safe_dump(['a', 'b', 'c']), encoding='utf-8')
    res = S.import_scenario(p)
    assert res['ok'] is False
    assert res['scenario'] is None


def test_missing_required_keys_flagged():
    issues = S.validate_scenario({'key': 'x'})
    assert any('name' in i for i in issues)
    assert any('pages' in i for i in issues)


# ── config 读写(假 config 路径) ─────────────────────────────────────────────

def test_active_scenario_reads_ui_scenario():
    assert S.active_scenario({'ui': {'scenario': 'thermocat'}})['key'] == 'thermocat'
    assert S.active_scenario({})['key'] == 'full'          # 缺省 full
    assert S.active_scenario(None)['key'] == 'full'


def test_set_scenario_persists_to_config(tmp_path):
    cfg = tmp_path / 'config.yaml'
    S.set_scenario('electrocat', config_path=cfg)
    loaded = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert loaded['ui']['scenario'] == 'electrocat'
    # 往返:active_scenario 能读回
    assert S.active_scenario(loaded)['key'] == 'electrocat'


def test_set_scenario_unknown_key_warns_but_writes(tmp_path):
    cfg = tmp_path / 'config.yaml'
    with pytest.warns(UserWarning):
        S.set_scenario('community_custom', config_path=cfg)
    loaded = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert loaded['ui']['scenario'] == 'community_custom'
