from pathlib import Path


ASSETS = Path(__file__).parents[1] / "vcstudio" / "gui_web" / "assets"


def _read(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def test_density_contract_has_exact_three_values_and_standard_default():
    html = _read("index.html")
    app_js = _read("app.js")
    settings_js = _read("settings.js")

    assert "['comfortable', 'standard', 'compact']" in app_js
    assert "name=\"set-density\"" in html
    assert html.count('name="set-density"') == 3
    assert 'value="comfortable"' in html
    assert 'value="standard" checked' in html
    assert 'value="compact"' in html
    assert "input[name=\"set-density\"]" in settings_js
    assert "VCS.call('density_set', density)" in settings_js
    assert "spacious" not in app_js
    assert 'value="spacious"' not in html


def test_density_is_prepainted_from_strict_local_whitelist():
    html = _read("index.html")

    assert "localStorage.getItem('vcs.density')" in html
    assert "['comfortable','standard','compact'].indexOf(_d)>=0?_d:'standard'" in html
    assert "document.documentElement.dataset.density='standard'" in html


def test_density_apply_is_immediate_local_and_server_calibrated():
    app_js = _read("app.js")
    settings_js = _read("settings.js")

    assert "document.documentElement.dataset.density = density" in app_js
    assert "localStorage.setItem('vcs.density', density)" in app_js
    assert "if (ui.density) VCS.densityApply(ui.density)" in app_js
    assert "else if (!document.documentElement.dataset.density)" in app_js
    assert "selectDensity(ui.density || 'standard', false)" in settings_js
    assert "input.addEventListener('change'" in settings_js


def test_density_css_uses_tokens_without_page_scaling():
    css = _read("app.css")

    assert '[data-density="comfortable"]' in css
    assert '[data-density="standard"]' in css
    assert '[data-density="compact"]' in css
    assert "--density-font-size:15px" in css
    assert "--density-font-size:14px" in css
    assert "--density-font-size:13px" in css
    assert "--density-control-height:40px" in css
    assert "--density-control-height:36px" in css
    assert "--density-control-height:30px" in css
    assert "font:var(--density-font-size)/var(--density-line-height)" in css
    assert "min-height:var(--density-control-height)" in css
    assert "height:var(--density-table-row-height)" in css
    assert ".page{padding:var(--density-page-block-start)" in css
    assert "html[data-density] .card.acc:not(.pj-import)" in css
    assert "html[data-density] .analysis-workbench-page" in css
    assert "html[data-density] .report-workbench-page" in css

    density_definitions = css[css.index('[data-density="comfortable"]'):
                              css.index('/* 工作模式显隐')]
    assert "transform:" not in density_definitions
    assert "zoom:" not in density_definitions


def test_density_selector_is_native_radio_fieldset_with_explanation():
    html = _read("index.html")
    start = html.index('<fieldset class="density-settings"')
    end = html.index('</fieldset>', start)
    control = html[start:end]

    assert '<legend class="set-lbl"' in control
    assert control.count('type="radio"') == 3
    assert control.count('<label class="density-opt"') == 3
    assert 'aria-describedby="set-density-help"' in control
    assert "不会改变科学数据、图表坐标或导出内容" in control
