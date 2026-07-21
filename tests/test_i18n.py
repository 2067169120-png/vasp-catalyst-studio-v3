"""国际化基础设施测试:zh/en 键同步、取词/格式化/缺键回退、缺键回落、export_for_js、scan_html。"""
from __future__ import annotations

import json

import pytest

from vcstudio.shared import i18n


@pytest.fixture(autouse=True)
def _reset_i18n():
    """每例前后清缓存 / 缺键计数 / 活动语言,防跨例串味(set_lang 会改进程内活动语言)。"""
    i18n.reset_runtime_state()
    yield
    i18n.reset_runtime_state()


# ── 键同步(两文件必须一一对应) ─────────────────────────────────────────────

def test_available_langs_has_zh_and_en_base_first():
    langs = i18n.available_langs()
    assert langs[0] == 'zh'                 # 基准语言排首
    assert 'en' in langs


def test_zh_en_keys_fully_synced():
    # en 相对 zh 无缺键
    assert i18n.missing_keys('en') == []
    # 反向:zh 相对 en 也无缺键(等价于键集完全一致)
    zh = json.load(open(i18n._locale_file('zh'), encoding='utf-8'))
    en = json.load(open(i18n._locale_file('en'), encoding='utf-8'))
    assert set(zh) == set(en)
    assert len(zh) >= 150                   # 基准字典规模底线


def test_base_lang_has_no_missing_keys():
    assert i18n.missing_keys('zh') == []


def test_no_value_is_empty():
    zh = json.load(open(i18n._locale_file('zh'), encoding='utf-8'))
    en = json.load(open(i18n._locale_file('en'), encoding='utf-8'))
    assert all(str(v).strip() for v in zh.values())
    assert all(str(v).strip() for v in en.values())


# ── t() 取词 / 格式化 / 缺键 ──────────────────────────────────────────────────

def test_t_returns_translation_per_lang():
    assert i18n.t('nav.dashboard', 'zh') == '仪表盘'
    assert i18n.t('nav.dashboard', 'en') == 'Dashboard'
    assert i18n.t('jobs.continue', 'en') == 'Restart continuation'   # 续算术语
    assert i18n.t('project.figures.ladder', 'en').endswith('needs molecule library)')


def test_t_missing_key_returns_key_and_counts():
    assert i18n.t('no.such.key', 'en') == 'no.such.key'
    i18n.t('no.such.key', 'en')
    assert i18n.missing_lookups().get('no.such.key') == 2


def test_t_format_interpolation():
    assert i18n.t('msg.generated_count', 'zh', n=12) == '已生成 12 个体系'
    assert i18n.t('msg.generated_count', 'en', n=12) == 'Generated 12 systems'


def test_t_format_missing_field_falls_back_to_template():
    # 给了占位符模板却没传对应字段 → 稳健返回未插值模板,不抛
    assert i18n.t('msg.generated_count', 'en') == 'Generated {n} systems'


def test_t_default_lang_is_zh_then_follows_active():
    assert i18n.t('nav.jobs') == '作业'          # 默认活动语言 zh


# ── 缺键回落(用临时 locales 目录构造真实缺键) ───────────────────────────────

def test_load_locale_fallback_to_base(tmp_path, monkeypatch):
    (tmp_path / 'zh.json').write_text(
        json.dumps({'a.x': '甲', 'a.y': '乙'}, ensure_ascii=False), encoding='utf-8')
    (tmp_path / 'fr.json').write_text(
        json.dumps({'a.x': 'A'}, ensure_ascii=False), encoding='utf-8')   # 缺 a.y
    monkeypatch.setattr(i18n, '_LOCALES_DIR', tmp_path)
    i18n.reset_runtime_state()
    merged = i18n.load_locale('fr')
    assert merged['a.x'] == 'A'                  # 实译
    assert merged['a.y'] == '乙'                 # 缺键回落到基准
    assert i18n.missing_keys('fr') == ['a.y']
    assert set(i18n.available_langs()) == {'zh', 'fr'}


# ── export_for_js ────────────────────────────────────────────────────────────

def test_export_for_js_returns_full_dict():
    d = i18n.export_for_js('en')
    assert isinstance(d, dict)
    assert d['nav.dashboard'] == 'Dashboard'
    assert len(d) >= 150
    # 回落补齐后不应缺任何基准键
    zh = json.load(open(i18n._locale_file('zh'), encoding='utf-8'))
    assert set(d) >= set(zh)


def test_export_for_js_is_a_copy():
    d = i18n.export_for_js('en')
    d['nav.dashboard'] = 'MUTATED'
    assert i18n.t('nav.dashboard', 'en') == 'Dashboard'


# ── current_lang / set_lang ──────────────────────────────────────────────────

def test_current_lang_from_config_defaults_zh():
    assert i18n.current_lang({'ui': {'lang': 'en'}}) == 'en'
    assert i18n.current_lang({}) == 'zh'
    assert i18n.current_lang(None) == 'zh'


def test_set_lang_persists_and_switches_active(tmp_path):
    import yaml
    cfg = tmp_path / 'config.yaml'
    i18n.set_lang('en', config_path=cfg)
    loaded = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert loaded['ui']['lang'] == 'en'
    assert i18n.current_lang(loaded) == 'en'
    # set_lang 之后 t() 无参默认走 en(进程内活动语言已切换)
    assert i18n.t('nav.dashboard') == 'Dashboard'


# ── scan_html_strings 冒烟 ───────────────────────────────────────────────────

def test_scan_html_strings_smoke():
    html = (
        '<a href="#" data-page="dashboard">仪表盘</a>'
        '<input placeholder="结构文件路径">'
        '<button>Generate</button>'          # 纯英文不收
        '<span>运行中</span>'
        '<div class="x"></div>'              # 空文本不收
    )
    got = i18n.scan_html_strings(html)
    assert '仪表盘' in got
    assert '结构文件路径' in got
    assert '运行中' in got
    assert 'Generate' not in got


def test_scan_html_strings_dedup_preserves_order():
    html = '<a>仪表盘</a><b>作业</b><c>仪表盘</c>'
    got = i18n.scan_html_strings(html)
    assert got == ['仪表盘', '作业']


def test_scan_html_on_real_index_html_finds_known_labels():
    from pathlib import Path
    idx = Path(i18n.__file__).resolve().parents[1] / 'gui_web' / 'assets' / 'index.html'
    strings = i18n.scan_html_strings(idx.read_text(encoding='utf-8'))
    for label in ('仪表盘', '生成输入', '结果分析', '作业', '集群', '设置'):
        assert label in strings, label
