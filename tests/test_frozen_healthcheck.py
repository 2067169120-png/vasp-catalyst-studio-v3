"""Focused contracts for the no-window frozen executable healthcheck."""
from __future__ import annotations

import json
from pathlib import Path

from vcstudio.gui_web import frozen_entry, frozen_healthcheck


ROOT = Path(__file__).resolve().parents[1]


def test_source_tree_cannot_impersonate_the_frozen_package():
    report = frozen_healthcheck.run_healthcheck(profile='lite', require_frozen=True)

    assert report['ok'] is False
    assert report['frozen'] is False
    frozen = report['checks']['frozen-runtime']
    assert frozen['ok'] is False
    assert 'PyInstaller executable' in frozen['error']
    assert report['checks']['web-assets']['ok'] is True
    assert report['checks']['locales']['ok'] is True
    assert report['checks']['core-modules']['ok'] is True


def test_source_tree_lite_probe_can_validate_every_non_frozen_contract():
    report = frozen_healthcheck.run_healthcheck(
        profile='lite',
        require_frozen=False,
        bundle_root=ROOT,
    )

    assert report['ok'] is True
    assert set(report['checks']) == {
        'frozen-runtime', 'core-modules', 'web-assets', 'locales',
    }
    assert report['checks']['web-assets']['detail']['first_party_js'] > 0
    assert report['checks']['locales']['detail']['languages'][:1] == ['zh']
    core = report['checks']['core-modules']['detail']
    assert set(frozen_healthcheck.WORKBENCH_MODULE_CONTRACTS) <= set(core)


def test_core_probe_fails_when_a_workbench_module_contract_is_incomplete(monkeypatch):
    real_import = frozen_healthcheck.importlib.import_module
    target = next(iter(frozen_healthcheck.WORKBENCH_MODULE_CONTRACTS))

    class IncompleteModule:
        pass

    def fake_import(name):
        if name == target:
            return IncompleteModule()
        return real_import(name)

    monkeypatch.setattr(frozen_healthcheck.importlib, 'import_module', fake_import)

    try:
        frozen_healthcheck._probe_core_modules()
    except RuntimeError as exc:
        assert target in str(exc)
        assert 'lacks required attributes' in str(exc)
    else:
        raise AssertionError('an incomplete workbench module should fail the frozen probe')


def test_asset_probe_fails_when_index_references_a_missing_local_file(monkeypatch, tmp_path):
    (tmp_path / 'index.html').write_text(
        '<link href="app.css"><script src="missing.js"></script>',
        encoding='utf-8',
    )
    (tmp_path / 'app.css').write_text('body {}', encoding='utf-8')
    (tmp_path / 'present.js').write_text('void 0;', encoding='utf-8')
    (tmp_path / 'vendor').mkdir()
    (tmp_path / 'vendor' / 'vendor.js').write_text('void 0;', encoding='utf-8')
    (tmp_path / 'fonts').mkdir()
    (tmp_path / 'fonts' / 'font.txt').write_text('fixture', encoding='utf-8')

    from vcstudio.gui_web import resources
    monkeypatch.setattr(resources, 'asset_dir', lambda: tmp_path)

    try:
        frozen_healthcheck._probe_web_assets(tmp_path)
    except RuntimeError as exc:
        assert 'missing.js' in str(exc)
    else:
        raise AssertionError('missing index.html dependency should fail the asset probe')


def test_windowed_healthcheck_writes_machine_readable_report(monkeypatch, tmp_path):
    expected = {
        'schema': 1,
        'ok': True,
        'profile': 'full',
        'frozen': True,
        'checks': {'frozen-runtime': {'ok': True}},
    }
    monkeypatch.setattr(
        frozen_healthcheck,
        'run_healthcheck',
        lambda **_kwargs: expected,
    )
    output = tmp_path / 'healthcheck.json'

    result = frozen_healthcheck.main([
        '--healthcheck', '--healthcheck-profile', 'full',
        '--healthcheck-output', str(output),
    ])

    assert result == 0
    assert json.loads(output.read_text(encoding='utf-8')) == expected


def test_frozen_entry_routes_healthcheck_without_starting_gui(monkeypatch):
    called = []
    monkeypatch.setattr(
        frozen_healthcheck,
        'main',
        lambda args: called.append(list(args)) or 17,
    )

    result = frozen_entry.main(['--healthcheck', '--healthcheck-profile', 'lite'])

    assert result == 17
    assert called == [['--healthcheck', '--healthcheck-profile', 'lite']]
