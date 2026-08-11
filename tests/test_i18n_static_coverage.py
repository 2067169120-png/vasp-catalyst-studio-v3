"""Strict coverage for static, user-visible strings in the WebView shell.

This test intentionally parses the authored HTML instead of searching raw text:
JavaScript and CSS source are not UI copy, while accessible-name attributes are.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "vcstudio" / "gui_web" / "assets" / "index.html"
LOCALES = ROOT / "vcstudio" / "shared" / "locales"
CJK = re.compile(r"[\u4e00-\u9fff]")
VISIBLE_ATTRIBUTES = (
    "placeholder",
    "title",
    "aria-label",
    "aria-description",
    "alt",
)
REFERENCE_ATTRIBUTES = (
    "data-i18n",
    "data-i18n-ph",
    "data-i18n-title",
    "data-i18n-aria-label",
)

# No Chinese-bearing static UI phrase is locale-neutral today. Scientific
# symbols and product names are retained inside translated phrases instead of
# exempting the surrounding instruction from localization.
LOCALE_NEUTRAL_CJK: frozenset[str] = frozenset()


def _normalize(value: str | None) -> str:
    return " ".join(str(value or "").split())


class _StaticStringParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.phrases: list[str] = []
        self.references: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        values = dict(attrs)
        for attribute in VISIBLE_ATTRIBUTES:
            phrase = _normalize(values.get(attribute))
            if phrase and CJK.search(phrase):
                self.phrases.append(phrase)
        for attribute in REFERENCE_ATTRIBUTES:
            key = _normalize(values.get(attribute))
            if key:
                self.references.append((attribute, key, tag))

    def handle_startendtag(
            self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        phrase = _normalize(data)
        if phrase and CJK.search(phrase):
            self.phrases.append(phrase)


def _parsed_index() -> _StaticStringParser:
    parser = _StaticStringParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _locale(name: str) -> dict[str, str]:
    result = json.loads((LOCALES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


def test_every_static_cjk_phrase_has_a_locale_entry():
    parser = _parsed_index()
    zh_values = {_normalize(value) for value in _locale("zh").values()}
    unique_phrases = list(dict.fromkeys(parser.phrases))
    uncovered = [
        phrase for phrase in unique_phrases
        if phrase not in zh_values and phrase not in LOCALE_NEUTRAL_CJK
    ]
    assert not uncovered, (
        f"{len(uncovered)} static CJK phrase(s) lack a zh locale value: "
        + " | ".join(uncovered)
    )


def test_every_authored_i18n_reference_exists_in_both_locales():
    parser = _parsed_index()
    zh = _locale("zh")
    en = _locale("en")
    missing = [
        f"{tag}[{attribute}={key!r}]"
        for attribute, key, tag in parser.references
        if key not in zh or key not in en
    ]
    assert not missing, "Missing authored i18n reference(s): " + ", ".join(missing)


def test_legacy_static_keys_are_stable_complete_and_really_translated():
    zh = _locale("zh")
    en = _locale("en")
    expected = {
        f"legacy.index.{index:04d}"
        for index in (*range(1, 576), *range(584, 590))
    }
    assert expected <= set(zh)
    assert expected <= set(en)
    assert all(_normalize(zh[key]) for key in expected)
    assert all(_normalize(en[key]) for key in expected)
    untranslated = [key for key in sorted(expected) if CJK.search(en[key])]
    assert not untranslated, "Legacy English values still contain CJK: " + ", ".join(untranslated)


def test_ai_assistant_static_copy_has_explicit_semantic_anchors():
    html = INDEX.read_text(encoding="utf-8")
    start = html.index('<section class="page shell-assistant-drawer"')
    assistant = html[start:html.index("</section>", start)]

    text_keys = {
        "ai.chat.heading",
        "ai.chat.description",
        "ai.chat.new",
        "ai.chat.initial_empty",
        "ai.chat.attach",
        "ai.chat.send",
        "ai.chat.stop",
        "ai.chat.privacy",
        "ai.guide.external_disabled",
        "ai.source.title",
        "ai.source.extract",
        "ai.spec.editor_title",
        "ai.plan.generate",
        "ai.instantiate.title",
        "ai.instantiate.autopilot",
        "ai.instantiate.autopilot_warning",
        "ai.instantiate.start_pilot",
    }
    for key in text_keys:
        assert f'data-i18n="{key}"' in assistant, key

    attribute_keys = {
        "ai-chat-session": {"data-i18n-aria-label": "ai.chat.session"},
        "ai-chat-input": {
            "data-i18n-aria-label": "ai.chat.prompt",
            "data-i18n-ph": "ai.chat.prompt",
        },
        "ai-pdf": {
            "data-i18n-aria-label": "ai.source.pdf_input",
            "data-i18n-ph": "ai.source.pdf_input",
        },
        "ai-text": {
            "data-i18n-aria-label": "ai.source.method_text",
            "data-i18n-ph": "ai.source.method_text",
        },
        "ai-outroot": {
            "data-i18n-aria-label": "ai.instantiate.output_root",
            "data-i18n-ph": "ai.instantiate.output_root",
        },
        "ai-ms-fmt": {
            "data-i18n-aria-label": "ai.cap.manuscript.format",
        },
    }
    for element_id, expected in attribute_keys.items():
        match = re.search(rf'<[^>]+\bid="{re.escape(element_id)}"[^>]*>', assistant)
        assert match, element_id
        opening_tag = match.group(0)
        for attribute, key in expected.items():
            assert f'{attribute}="{key}"' in opening_tag, (element_id, attribute, key)

    semantic_keys = text_keys | {
        key for expected in attribute_keys.values() for key in expected.values()
    }
    zh = _locale("zh")
    en = _locale("en")
    assert semantic_keys <= set(zh) == set(en)
    assert all(_normalize(zh[key]) for key in semantic_keys)
    assert all(_normalize(en[key]) and not CJK.search(en[key]) for key in semantic_keys)


def test_density_i18n_keys_are_complete_and_translated():
    keys = {
        "settings.density.legend",
        "settings.density.help",
        "settings.density.comfortable",
        "settings.density.comfortable.sub",
        "settings.density.standard",
        "settings.density.standard.sub",
        "settings.density.compact",
        "settings.density.compact.sub",
    }
    zh = _locale("zh")
    en = _locale("en")
    assert keys <= set(zh) == set(en)
    assert all(_normalize(zh[key]) for key in keys)
    assert all(_normalize(en[key]) and not CJK.search(en[key]) for key in keys)
