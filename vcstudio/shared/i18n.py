"""国际化(i18n)基础设施:多语言词条加载、取词、缺键回落与同步校验。

定位(见项目调研《v3.1 总体方案》发刊/国际化路线):当前 GUI 文案硬编码在 index.html 与各 JS 里,
出海需要一层可切换的词典。本模块提供纯数据 + 纯函数的 i18n 引擎:

- locales/zh.json 为**基准字典**(值 = 现中文原文),locales/en.json 为同键英文翻译;
- 缺键回落:非基准语言缺某键 → 取基准(zh)值兜底,并记录 missing,绝不给用户空白;
- 键同步保证:missing_keys(lang) 给出该语言相对基准的缺键清单,测试用它强制两文件键完全同步;
- 前端注入:export_for_js(lang) 返回整棵(回落补齐后的)词典 dict,供 GUI 一次性注入。

词条键分层扁平化(点分 key,如 'nav.dashboard' / 'jobs.derive.freq' / 'common.ok'),
JSON 里就是扁平 {点分key: 文案} —— 便于取词、缺键比对与前端查表。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

BASE_LANG = 'zh'                                    # 基准语言(键的权威来源 + 回落兜底)
DEFAULT_LANG = 'zh'                                 # t() 未指定 lang 且未 set_lang 时的默认
_LOCALES_DIR = Path(__file__).resolve().parent / 'locales'

# 运行期缓存:{lang: 回落补齐后的扁平 dict};_ACTIVE_LANG 为 t() 的隐式当前语言
_CACHE: dict = {}
_ACTIVE_LANG = DEFAULT_LANG
# t() 取词命中缺键(连基准都没有)时按 key 计数,供体检/测试观察
_MISSING_LOOKUPS: Counter = Counter()

_CJK_RE = re.compile(r'[一-鿿]')
_LANG_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,15}$')


def normalize_lang(lang: str | None, *, require_available: bool = True) -> str:
    """Return one bounded locale identifier or fail closed.

    Locale names cross the webview bridge and are later used to select a JSON
    resource.  They must never become arbitrary path fragments.  Production
    language changes additionally require an installed locale; tests may set a
    temporary locale directory and therefore use the same availability check.
    """
    candidate = str(lang or DEFAULT_LANG).strip().lower()
    if not _LANG_RE.fullmatch(candidate):
        raise ValueError('language must be a safe locale identifier')
    if require_available:
        installed = {
            path.stem.lower() for path in _LOCALES_DIR.glob('*.json')
            if _LANG_RE.fullmatch(path.stem)
        }
        if candidate not in installed:
            raise ValueError(f'unsupported interface language: {candidate}')
    return candidate


def _locale_file(lang: str) -> Path:
    return _LOCALES_DIR / f'{normalize_lang(lang, require_available=False)}.json'


def _read_raw(lang: str) -> dict:
    """读某语言的原始 JSON(不回落)。缺文件/坏 JSON → {}。"""
    p = _locale_file(lang)
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def available_langs() -> list:
    """扫描 locales 目录下的 *.json → 语言码列表(基准语言排首,其余字母序)。"""
    langs = sorted(p.stem for p in _LOCALES_DIR.glob('*.json'))
    if BASE_LANG in langs:
        langs = [BASE_LANG] + [x for x in langs if x != BASE_LANG]
    return langs


def missing_keys(lang: str) -> list:
    """返回 lang 相对基准(zh)缺失的键(排序)。测试用它保证两文件键完全同步。"""
    base = _read_raw(BASE_LANG)
    other = _read_raw(lang)
    return sorted(set(base) - set(other))


def load_locale(lang: str) -> dict:
    """加载某语言词典(回落补齐后的扁平 dict):基准键全在,缺键取基准值兜底。

    结果进 _CACHE;非基准语言的缺键同时记入日志式结构(见 missing_keys 独立复算)。
    """
    lang = normalize_lang(lang)
    if lang in _CACHE:
        return _CACHE[lang]
    base = _read_raw(BASE_LANG)
    raw = _read_raw(lang)
    merged = dict(base)                             # 先铺满基准键(兜底值)
    merged.update({k: v for k, v in raw.items()})   # 再用本语言实译覆盖
    _CACHE[lang] = merged
    return merged


def t(key: str, lang: str | None = None, **fmt) -> str:
    """取词 + 可选 str.format 插值。

    lang 缺省用当前活动语言(_ACTIVE_LANG,由 set_lang 更新,初始 DEFAULT_LANG)。
    缺键(连基准都没有)→ 返回 key 本身并按 key 计数(_MISSING_LOOKUPS),绝不抛异常。
    fmt 里若给了占位符但模板无对应字段,str.format 失败时回退返回未插值模板(稳健优先)。
    """
    use = lang or _ACTIVE_LANG
    table = load_locale(use)
    if key not in table:
        _MISSING_LOOKUPS[key] += 1
        return key
    val = table[key]
    if fmt and isinstance(val, str):
        try:
            return val.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return val
    return val


def current_lang(config: dict | None = None) -> str:
    """读 config 的 ui.lang 决定当前语言(缺省 'zh')。config 缺省 → 默认语言。"""
    if isinstance(config, dict):
        ui = config.get('ui')
        if isinstance(ui, dict) and ui.get('lang'):
            try:
                return normalize_lang(ui['lang'])
            except ValueError:
                return DEFAULT_LANG
    return DEFAULT_LANG


def set_lang(lang: str, config_path=None) -> Path:
    """把界面语言写入 config 的 ui.lang 并持久化;同时更新进程内活动语言(t() 立即生效)。"""
    global _ACTIVE_LANG
    lang = normalize_lang(lang)
    _ACTIVE_LANG = lang
    from vcstudio.shared.config import set_ui_state
    return set_ui_state(config_path, lang=lang)


def export_for_js(lang: str) -> dict:
    """返回整棵(回落补齐后的)扁平词典 dict,供前端一次性注入(GUI 按点分 key 查表)。"""
    return dict(load_locale(lang))


def export_bundle_for_js(lang: str) -> dict:
    """Return target and source dictionaries for deterministic DOM translation.

    ``dict`` remains the keyed target-language table used by explicit
    ``data-i18n`` annotations.  ``source`` is the exact zh baseline for the
    same keys, allowing the frontend to translate legacy text nodes without
    guessing or calling an online translation service.
    """
    selected = normalize_lang(lang)
    return {
        'lang': selected,
        'dict': dict(load_locale(selected)),
        'source': dict(load_locale(BASE_LANG)),
    }


def missing_lookups() -> dict:
    """返回 t() 运行期累计的缺键计数 {key: 次数}(体检/测试观察用)。"""
    return dict(_MISSING_LOOKUPS)


def reset_runtime_state() -> None:
    """清空缓存、缺键计数并把活动语言复位到默认(主要供测试隔离)。"""
    global _ACTIVE_LANG
    _CACHE.clear()
    _MISSING_LOOKUPS.clear()
    _ACTIVE_LANG = DEFAULT_LANG


def scan_html_strings(html_text: str) -> list:
    """粗提 HTML 中需要本地化的中文文本与可访问属性。

    This deliberately remains a lightweight scanner rather than an HTML
    rewriter.  It covers visible text plus the attributes that form control
    names/help in the accessibility tree, so translation coverage tests do not
    overlook an English-looking screen reader surface.
    """
    found: list = []
    seen: set = set()

    def _push(s: str) -> None:
        s = (s or '').strip()
        if s and _CJK_RE.search(s) and s not in seen:
            seen.add(s)
            found.append(s)

    for m in re.finditer(r'>([^<>]+)<', html_text or ''):
        _push(m.group(1))
    translated_attributes = (
        'placeholder', 'title', 'aria-label', 'aria-description', 'alt',
    )
    attr_pattern = '|'.join(re.escape(item) for item in translated_attributes)
    for match in re.finditer(
            rf'(?:{attr_pattern})\s*=\s*(["\'])(.*?)\1',
            html_text or '', flags=re.IGNORECASE | re.DOTALL):
        _push(match.group(2))
    return found
