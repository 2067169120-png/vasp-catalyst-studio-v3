"""Strict i18n coverage for authored, user-visible JavaScript strings.

The WebView still contains legacy modules that create DOM nodes at runtime.  The
runtime translator can safely translate a complete text node or accessible
attribute by exact source value.  It cannot safely translate fragments joined
with ``+`` or template literals containing ``${...}``; those call sites must
use ``VCS.t``/``tr`` explicitly with interpolation parameters.

This scanner is deliberately small and deterministic.  It understands JavaScript
comments, quoted strings, template literals, and translation-call parentheses;
it does not attempt to infer data flow or translate arbitrary backend output.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vcstudio" / "gui_web" / "assets"
LOCALES = ROOT / "vcstudio" / "shared" / "locales"
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
DYNAMIC_KEY = re.compile(r"legacy\.dynamic\.[a-z0-9_]+\.\d{4}\Z")
VISIBLE_ATTRIBUTES = (
    "placeholder",
    "title",
    "aria-label",
    "aria-description",
    "alt",
)

# Every entry is (asset filename, normalized authored literal).  These are not
# exemptions: ``test_composite_cjk_literals_use_explicit_translation_calls``
# intentionally fails while any declared entry remains outside VCS.t/tr.  The
# explicit inventory makes the necessary call-site work reviewable and keeps a
# newly introduced composite phrase from disappearing into a broad allowlist.
REQUIRES_EXPLICIT_T: frozenset[tuple[str, str]] = frozenset()

# Backend error/log payloads are runtime data, not authored JavaScript
# literals.  There is consequently no reasoned exemption at present.  Keep the
# allowlist explicit and small if a future raw-payload contract requires one.
RAW_BACKEND_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()

# These literals are protocol/sort keys, not rendered copy.  Their localized
# display values are handled separately through runtime.project/runtime.taskcat
# calls.  Keeping the inventory exact prevents a broad "scientific text"
# exemption from hiding future UI strings.
SOURCE_CONTRACT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("project.js", "未识别"),
    ("taskcat.js", "INCAR 顾问"),
    ("taskcat.js", "作业生成"),
    ("taskcat.js", "结果计算器"),
})


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    start: int
    end: int
    line: int
    children: tuple["_Token", ...] = ()


@dataclass(frozen=True)
class _CjkLiteral:
    filename: str
    line: int
    value: str
    interpolated: bool
    joined_left: bool
    joined_right: bool
    translated: bool


def _normalize(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _decode_common_escapes(value: str) -> str:
    """Decode the small escape set that can affect a runtime UI string."""

    replacements = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\b": "\b",
        r"\f": "\f",
        r"\v": "\v",
        r"\'": "'",
        r'\"': '"',
        r"\`": "`",
        r"\\": "\\",
    }
    return re.sub(
        r"\\(?:[nrtbfv'\"`\\])",
        lambda match: replacements.get(match.group(0), match.group(0)),
        value,
    )


def _scan_quoted(source: str, start: int, quote: str) -> tuple[int, str]:
    index = start + 1
    content: list[str] = []
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            content.append(source[index:index + 2])
            index += 2
            continue
        if char == quote:
            return index + 1, _decode_common_escapes("".join(content))
        if char in "\r\n":
            return index, _decode_common_escapes("".join(content))
        content.append(char)
        index += 1
    return index, _decode_common_escapes("".join(content))


def _scan_template(
        source: str, start: int,
) -> tuple[int, str, bool, tuple[tuple[int, int], ...]]:
    index = start + 1
    content: list[str] = []
    interpolated = False
    brace_depth = 0
    expression_start: int | None = None
    expressions: list[tuple[int, int]] = []
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            content.append(source[index:index + 2])
            index += 2
            continue
        if brace_depth == 0 and char == "`":
            return (
                index + 1,
                _decode_common_escapes("".join(content)),
                interpolated,
                tuple(expressions),
            )
        if brace_depth == 0 and source.startswith("${", index):
            interpolated = True
            content.append("${...}")
            brace_depth = 1
            expression_start = index + 2
            index += 2
            continue
        if brace_depth:
            if source.startswith("//", index):
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline
                continue
            if source.startswith("/*", index):
                close = source.find("*/", index + 2)
                index = len(source) if close < 0 else close + 2
                continue
            if char in "'\"":
                index, _ = _scan_quoted(source, index, char)
                continue
            if char == "`":
                index, _, _, _ = _scan_template(source, index)
                continue
            if char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
                if brace_depth == 0 and expression_start is not None:
                    expressions.append((expression_start, index))
                    expression_start = None
            index += 1
            continue
        content.append(char)
        index += 1
    if expression_start is not None:
        expressions.append((expression_start, index))
    return (
        index,
        _decode_common_escapes("".join(content)),
        interpolated,
        tuple(expressions),
    )


def _offset_token(token: _Token, offset: int, line_offset: int) -> _Token:
    return _Token(
        kind=token.kind,
        value=token.value,
        start=token.start + offset,
        end=token.end + offset,
        line=token.line + line_offset,
        children=tuple(
            _offset_token(child, offset, line_offset)
            for child in token.children
        ),
    )


def _tokens(source: str) -> list[_Token]:
    result: list[_Token] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = len(source) if close < 0 else close + 2
            continue

        start = index
        line = source.count("\n", 0, start) + 1
        if char in "'\"":
            index, value = _scan_quoted(source, index, char)
            result.append(_Token("string", value, start, index, line))
            continue
        if char == "`":
            index, value, interpolated, expressions = _scan_template(
                source, index,
            )
            kind = "template_interpolated" if interpolated else "string"
            children: list[_Token] = []
            for expression_start, expression_end in expressions:
                line_offset = source.count("\n", 0, expression_start)
                children.extend(
                    _offset_token(token, expression_start, line_offset)
                    for token in _tokens(
                        source[expression_start:expression_end]
                    )
                )
            result.append(_Token(
                kind, value, start, index, line, tuple(children),
            ))
            continue
        if char.isalpha() or char in "_$":
            index += 1
            while index < len(source) and (
                    source[index].isalnum() or source[index] in "_$"):
                index += 1
            result.append(_Token(
                "identifier", source[start:index], start, index, line,
            ))
            continue
        index += 1
        result.append(_Token("punctuation", char, start, index, line))
    return result


def _is_translation_call(tokens: list[_Token], index: int) -> bool:
    # ``i18n`` is the editor module's thin, exact VCS.t wrapper; it has the
    # same explicit-key/fallback semantics as the local ``tr`` wrappers.
    if index and tokens[index - 1].value in {"tr", "i18n"}:
        return True
    return bool(
        index >= 3
        and tokens[index - 3].value == "VCS"
        and tokens[index - 2].value == "."
        and tokens[index - 1].value == "t"
    )


def _cjk_literals(path: Path) -> list[_CjkLiteral]:
    result: list[_CjkLiteral] = []

    def collect(tokens: list[_Token], inherited_translation: bool) -> None:
        translation_stack: list[bool] = (
            [True] if inherited_translation else []
        )
        for index, token in enumerate(tokens):
            if token.kind == "punctuation" and token.value == "(":
                translated = _is_translation_call(tokens, index)
                translation_stack.append(
                    translated
                    or bool(translation_stack and translation_stack[-1])
                )
                continue
            if token.kind == "punctuation" and token.value == ")":
                if translation_stack:
                    translation_stack.pop()
                continue

            translated = bool(
                translation_stack and translation_stack[-1]
            )
            if token.kind in {"string", "template_interpolated"}:
                value = _normalize(token.value)
                if value and CJK.search(value):
                    result.append(_CjkLiteral(
                        filename=path.name,
                        line=token.line,
                        value=value,
                        interpolated=(
                            token.kind == "template_interpolated"
                        ),
                        joined_left=(
                            index > 0 and tokens[index - 1].value == "+"
                        ),
                        joined_right=(
                            index + 1 < len(tokens)
                            and tokens[index + 1].value == "+"
                        ),
                        translated=translated,
                    ))
            if token.children:
                collect(list(token.children), translated)

    collect(_tokens(path.read_text(encoding="utf-8")), False)
    return result


class _VisibleHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._raw_depth = 0
        self.phrases: list[str] = []

    def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "code", "pre", "textarea"}:
            self._raw_depth += 1
        values = dict(attrs)
        for attribute in VISIBLE_ATTRIBUTES:
            phrase = _normalize(values.get(attribute))
            if phrase and CJK.search(phrase):
                self.phrases.append(phrase)

    def handle_endtag(self, tag: str) -> None:
        if (tag.lower() in {"script", "style", "code", "pre", "textarea"}
                and self._raw_depth):
            self._raw_depth -= 1

    def handle_data(self, data: str) -> None:
        phrase = _normalize(data)
        if not self._raw_depth and phrase and CJK.search(phrase):
            self.phrases.append(phrase)


def _visible_phrases(value: str) -> list[str]:
    if not re.search(r"</?[A-Za-z][^>]*>", value):
        return [value]
    parser = _VisibleHtmlParser()
    parser.feed(value)
    parser.close()
    return parser.phrases


def _runtime_phrases(literal: _CjkLiteral) -> list[tuple[str, bool]]:
    """Return (visible phrase, needs explicit interpolation) pairs.

    A concatenation boundary is represented by ``${...}``.  Parsing the
    resulting HTML mirrors the browser: if markup puts the dynamic value in a
    separate element, the adjacent Chinese remains an exact translatable text
    node; if the marker and Chinese share a node/attribute, exact replacement
    cannot work and the call site must translate explicitly.
    """

    value = literal.value
    if literal.joined_left:
        value = "${...}" + value
    if literal.joined_right:
        value += "${...}"
    return [
        (phrase, "${...}" in phrase)
        for phrase in _visible_phrases(value)
    ]


@lru_cache(maxsize=1)
def _all_literals() -> tuple[_CjkLiteral, ...]:
    return tuple(
        literal
        for path in sorted(ASSETS.glob("*.js"))
        for literal in _cjk_literals(path)
    )


def _locale(name: str) -> dict[str, str]:
    pairs = json.loads(
        (LOCALES / f"{name}.json").read_text(encoding="utf-8"),
        object_pairs_hook=lambda value: value,
    )
    assert isinstance(pairs, list)
    counts = Counter(key for key, _ in pairs)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    assert not duplicates, f"Duplicate {name} locale key(s): {duplicates}"
    return dict(pairs)


def test_template_interpolation_expressions_are_scanned_recursively(tmp_path):
    fixture = tmp_path / "nested-template.js"
    fixture.write_text(
        "const visible = `${flag ? '待确认' : "
        "`${fallback || '未知错误'}`}`;\n"
        "const translated = tr('fixture.key', {}, "
        "`${flag ? '已确认' : '人工设置'}`, 'Confirmed');\n",
        encoding="utf-8",
    )

    literals = _cjk_literals(fixture)
    by_value = {literal.value: literal for literal in literals}
    assert {"待确认", "未知错误", "已确认", "人工设置"} <= set(by_value)
    assert not by_value["待确认"].translated
    assert not by_value["未知错误"].translated
    assert by_value["已确认"].translated
    assert by_value["人工设置"].translated


def test_complete_dynamic_cjk_values_have_real_english_translations():
    zh = _locale("zh")
    en = _locale("en")
    source_keys: dict[str, list[str]] = {}
    for key, value in zh.items():
        source_keys.setdefault(_normalize(value), []).append(key)

    uncovered: list[str] = []
    untranslated: list[str] = []
    for literal in _all_literals():
        if literal.translated:
            continue
        for phrase, composite in _runtime_phrases(literal):
            if composite:
                continue
            if (literal.filename, phrase) in SOURCE_CONTRACT_ALLOWLIST:
                continue
            keys = source_keys.get(phrase, [])
            if not keys:
                uncovered.append(
                    f"{literal.filename}:{literal.line}: {phrase}"
                )
                continue
            if not any(
                    _normalize(en.get(key)) and not CJK.search(en[key])
                    for key in keys):
                untranslated.append(
                    f"{literal.filename}:{literal.line}: {phrase}"
                )

    assert not uncovered, (
        f"{len(uncovered)} complete dynamic CJK phrase(s) lack a zh locale "
        "value:\n" + "\n".join(dict.fromkeys(uncovered))
    )
    assert not untranslated, (
        "Dynamic phrases without a non-CJK English translation:\n"
        + "\n".join(dict.fromkeys(untranslated))
    )


def test_composite_cjk_literals_use_explicit_translation_calls():
    active = {
        (literal.filename, phrase)
        for literal in _all_literals()
        if not literal.translated
        for phrase, composite in _runtime_phrases(literal)
        if composite
    }
    allowlisted = active & RAW_BACKEND_ALLOWLIST
    unexpected = active - REQUIRES_EXPLICIT_T - allowlisted
    assert not unexpected, (
        "New composite CJK literal(s) must be reviewed and added to the "
        "explicit migration inventory:\n"
        + "\n".join(f"{name}: {value}" for name, value in sorted(unexpected))
    )

    unresolved = active & REQUIRES_EXPLICIT_T
    assert not unresolved, (
        "Composite UI strings cannot be translated safely by exact-value DOM "
        "replacement. Route each call site through VCS.t/tr with named "
        "parameters:\n"
        + "\n".join(f"{name}: {value}" for name, value in sorted(unresolved))
    )


def test_dynamic_locale_keys_are_stable_synced_and_translated():
    zh = _locale("zh")
    en = _locale("en")
    dynamic = {key for key in zh if key.startswith("legacy.dynamic.")}
    assert dynamic, "Dynamic JavaScript coverage must use stable locale keys"
    assert dynamic == {
        key for key in en if key.startswith("legacy.dynamic.")
    }
    malformed = sorted(key for key in dynamic if not DYNAMIC_KEY.fullmatch(key))
    assert not malformed, "Malformed dynamic locale key(s): " + ", ".join(malformed)
    assert all(_normalize(zh[key]) for key in dynamic)
    assert all(_normalize(en[key]) for key in dynamic)
    cjk_english = sorted(key for key in dynamic if CJK.search(en[key]))
    assert not cjk_english, (
        "Dynamic English values still contain CJK: " + ", ".join(cjk_english)
    )
    placeholders = sorted(
        key for key in dynamic
        if "[untranslated]" in en[key].lower()
        or "[待翻译]" in en[key]
    )
    assert not placeholders, (
        "Placeholder translations are forbidden: " + ", ".join(placeholders)
    )


def test_every_authored_runtime_translation_key_exists_in_both_locales():
    zh = _locale("zh")
    en = _locale("en")
    used: dict[str, set[str]] = {}
    patterns = (
        re.compile(r"VCS\.t\(\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\btr\(\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\bi18n\(\s*['\"]([^'\"]+)['\"]"),
        re.compile(
            r"data-i18n(?:-ph|-title|-aria-label|-aria-description|-alt|-sum)?="
            r"\\?['\"]([^'\"${}]+)\\?['\"]"
        ),
    )
    for path in [*sorted(ASSETS.glob("*.js")), ASSETS / "index.html"]:
        source = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for key in pattern.findall(source):
                used.setdefault(key, set()).add(path.name)

    missing_zh = sorted(set(used) - set(zh))
    missing_en = sorted(set(used) - set(en))
    assert not missing_zh, {
        key: sorted(used[key]) for key in missing_zh
    }
    assert not missing_en, {
        key: sorted(used[key]) for key in missing_en
    }

    cjk_english = sorted(
        key for key in used
        if key in en and CJK.search(_normalize(en[key]))
    )
    assert not cjk_english, (
        "Authored runtime English values still contain CJK: "
        + ", ".join(cjk_english)
    )
    empty_english = sorted(
        key for key in used if key in en and not _normalize(en[key])
    )
    assert not empty_english, (
        "Authored runtime English values are empty: "
        + ", ".join(empty_english)
    )
    placeholder = re.compile(r"\{([A-Za-z0-9_]+)\}")
    mismatched = sorted(
        key for key in used
        if key in zh and key in en
        and set(placeholder.findall(zh[key])) != set(placeholder.findall(en[key]))
    )
    assert not mismatched, (
        "Runtime locale placeholder sets differ between zh and en: "
        + ", ".join(mismatched)
    )


def test_raw_backend_allowlist_is_small_reasoned_and_not_a_ui_escape_hatch():
    assert len(RAW_BACKEND_ALLOWLIST) <= 8
    assert not (RAW_BACKEND_ALLOWLIST & REQUIRES_EXPLICIT_T)
    active = {
        (literal.filename, phrase)
        for literal in _all_literals()
        if not literal.translated
        for phrase, composite in _runtime_phrases(literal)
        if composite
    }
    stale = RAW_BACKEND_ALLOWLIST - active
    assert not stale, "Remove stale raw-backend allowlist entries: " + repr(stale)


def test_source_contract_allowlist_is_small_exact_and_current():
    assert len(SOURCE_CONTRACT_ALLOWLIST) <= 8
    active = {
        (literal.filename, phrase)
        for literal in _all_literals()
        if not literal.translated
        for phrase, composite in _runtime_phrases(literal)
        if not composite
    }
    stale = SOURCE_CONTRACT_ALLOWLIST - active
    assert not stale, "Remove stale source-contract entries: " + repr(stale)
