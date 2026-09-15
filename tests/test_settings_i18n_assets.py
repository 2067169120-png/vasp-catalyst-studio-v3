"""Settings dynamic localization contracts.

The static page is translated by ``app.js``.  These checks cover the strings
created later by ``settings.js`` so switching language never mutates the
scientific workflow selection and never exposes Chinese DTO fields in English
mode.
"""
from __future__ import annotations

import re
from pathlib import Path


SETTINGS_JS = (
    Path(__file__).parents[1] / "vcstudio" / "gui_web" / "assets" / "settings.js"
).read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    match = re.search(
        rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{(.*?)\n  \}}",
        SETTINGS_JS,
        flags=re.DOTALL,
    )
    assert match, name
    return match.group(1)


def test_dynamic_workspace_uses_bilingual_dto_fields():
    for field in (
        "name_en",
        "description_en",
        "category_en",
        "requires_en",
        "outputs_en",
        "next_action_en",
    ):
        assert field in SETTINGS_JS
    assert "const english = () =>" in SETTINGS_JS
    assert "const ENGINE_EN = Object.freeze" in SETTINGS_JS
    assert "row.limitations_en || en.limitations" in SETTINGS_JS


def test_english_fallbacks_are_explicit_and_not_chinese_dto_fallbacks():
    assert "const fallback = english() ? (en || key) : (zh || key);" in SETTINGS_JS
    assert "if (english()) return item[enField] || enFallback || '';" in SETTINGS_JS
    assert "task && task.requires_en" in SETTINGS_JS
    assert "task && task.outputs_en" in SETTINGS_JS
    assert "scenario, 'name', 'name_en'" in SETTINGS_JS
    assert "scenario, 'description', 'description_en'" in SETTINGS_JS


def test_language_event_redraws_without_changing_scientific_configuration():
    assert "document.addEventListener('vcs:language', redrawLocalizedWorkspace)" in SETTINGS_JS
    body = _function_body("redrawLocalizedWorkspace")
    for renderer in (
        "renderKeyState",
        "renderScenarioOptions",
        "renderScenarioDesc",
        "renderEngineOptions",
        "renderEngineDesc",
        "renderCalculationOptions",
        "renderCalculationGuide",
        "redrawLocalizedSettingLogs",
    ):
        assert renderer in body
    executable = re.sub(r"//.*", "", body)
    assert "VCS.call(" not in executable
    assert "scenario_set" not in executable
    assert "engine_set" not in executable
    assert "calculation_set" not in executable


def test_density_logs_retain_semantic_keys_and_redraw_after_language_switch():
    body = _function_body("selectDensity")
    assert "logLocalizedSetting('settings.status.density_changed'" in body
    assert "logLocalizedSetting('settings.status.density_failed'" in body
    assert "row.dataset.i18nLogKey = key" in SETTINGS_JS
    assert "row.dataset.i18nLogParams = JSON.stringify(params || {})" in SETTINGS_JS
    assert "row.dataset.i18nLogFallbackZh = zh || key" in SETTINGS_JS
    assert "row.dataset.i18nLogFallbackEn = en || key" in SETTINGS_JS
    assert "message.dataset.i18nLogMessage = ''" in SETTINGS_JS
    assert "function redrawLocalizedSettingLogs()" in SETTINGS_JS
    assert "message.textContent = tr(row.dataset.i18nLogKey" in SETTINGS_JS


def test_every_authored_settings_log_uses_the_semantic_log_helper():
    expected = {
        "settings.status.load_failed",
        "settings.status.figure_saved",
        "settings.status.figure_save_failed",
        "settings.status.llm_save_failed",
        "settings.status.key_save_failed",
        "settings.status.key_saved_securely",
        "settings.status.llm_testing",
        "settings.status.llm_ok",
        "settings.status.llm_failed",
        "settings.status.prompt_save_failed",
        "settings.status.prompt_saved",
        "settings.status.prompt_reset_failed",
        "settings.status.prompt_reset",
        "settings.status.directory_failed",
        "settings.status.paths_save_failed",
        "settings.status.paths_saved",
        "settings.status.theme_failed",
        "settings.status.theme_changed",
        "settings.status.density_changed",
        "settings.status.density_failed",
        "settings.status.managed_save_failed",
        "settings.status.managed_saved",
        "settings.status.calculation_open_failed",
        "settings.status.language_failed",
        "settings.status.language_changed",
        "settings.status.language_bundle_failed",
        "settings.status.scenario_failed",
        "settings.status.scenario_changed",
        "settings.status.engine_failed",
        "settings.status.engine_changed",
        "settings.status.calculation_failed",
        "settings.status.calculation_changed",
    }
    actual = set(re.findall(
        r"logLocalizedSetting\(\s*'(settings\.status\.[^']+)'",
        SETTINGS_JS,
    ))
    assert actual == expected
    assert "external ? 'settings.status.llm_saved_external'" in SETTINGS_JS
    assert ": 'settings.status.llm_saved_local'" in SETTINGS_JS
    assert "VCS.log(tr('settings.status." not in SETTINGS_JS
    assert "VCS.log(message" not in SETTINGS_JS
    assert SETTINGS_JS.count("VCS.log(") == 1


def test_dynamic_groups_guides_and_statuses_go_through_translation_helper():
    for key in (
        "settings.scenario.primary_group",
        "settings.scenario.advanced_group",
        "settings.engine.file_adapter_suffix",
        "settings.engine.limitations",
        "settings.calculation.io",
        "settings.calculation.next",
        "settings.calculation.open_results",
        "settings.calculation.open_modeling",
        "settings.calculation.open_inputs",
        "settings.status.language_changed",
        "settings.status.scenario_changed",
        "settings.status.engine_changed",
        "settings.status.calculation_changed",
    ):
        assert re.search(
            rf"(?:tr|logLocalizedSetting)\(\s*'{re.escape(key)}'",
            SETTINGS_JS,
        )


def test_engine_english_copy_preserves_units_and_engine_boundaries():
    assert "GTH density-grid cutoff in Ry" in SETTINGS_JS
    assert "cannot be converted from VASP ENCUT" in SETTINGS_JS
    assert "periodic slabs or bulk systems require VASP, CP2K, or CASTEP" in SETTINGS_JS
    assert "VASP plane-wave/PAW" in SETTINGS_JS
    assert "CASTEP pseudopotentials differ from VASP PAW" in SETTINGS_JS


def test_non_vasp_generic_task_copy_does_not_claim_a_fixed_vasp_cell():
    assert "const GENERIC_TASK_EN = Object.freeze" in SETTINGS_JS
    assert "name: 'Geometry Optimization'" in SETTINGS_JS
    assert "name: 'Single-Point Energy'" in SETTINGS_JS
    assert "name: 'Vibrational Frequency Analysis'" in SETTINGS_JS
    assert "row.engine !== 'vasp' && GENERIC_TASK_EN[row.key]" in SETTINGS_JS
