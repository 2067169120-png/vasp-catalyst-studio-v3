"""Headless integrity checks for the frozen Web executable.

The release smoke test must exercise the executable produced by PyInstaller, not a
source-tree import that can succeed while bundle data or hidden imports are missing.
This module deliberately avoids creating a webview window and writes a JSON report so
it remains observable when the executable is built with ``--windowed``.
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import importlib
import json
import os
import socket
import sys
import tempfile
import time
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import unquote, urlsplit


SUPPORTED_PROFILES = ('full', 'no-charts', 'lite', 'journey')
JOURNEY_SCHEMA = 'vcstudio.frozen-journey/v1'
WORKBENCH_MODULE_CONTRACTS = {
    'vcstudio.project.analysis_sources': ('resolve_project_targets',),
    'vcstudio.project.elf': ('summarize_elfcar',),
    'vcstudio.project.lab_policies': ('LabPolicySelectionStore', 'resolve'),
    'vcstudio.project.next_calculation': ('build_recommendations', 'draft_intent'),
    'vcstudio.project.project_lifecycle': ('LifecyclePlan',),
    'vcstudio.project.report_insights': ('scientific_diff', 'evidence_graph'),
    'vcstudio.project.resource_forecast': ('forecast', 'forecast_batch'),
}


class _JourneyStopped(RuntimeError):
    """Internal control flow after one journey phase fails."""


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    previous_pycache = sys.pycache_prefix
    previous_tempdir = tempfile.tempdir
    os.environ.update(values)
    sys.pycache_prefix = values['PYTHONPYCACHEPREFIX']
    tempfile.tempdir = values['TMP']
    try:
        yield
    finally:
        tempfile.tempdir = previous_tempdir
        sys.pycache_prefix = previous_pycache
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _offline_network_guard():
    """Deny and record every socket connection attempted by the journey."""

    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def blocked_connect(_socket, address):
        attempts.append(repr(address))
        raise RuntimeError('network access is forbidden during the frozen journey')

    def blocked_connect_ex(_socket, address):
        attempts.append(repr(address))
        raise RuntimeError('network access is forbidden during the frozen journey')

    def blocked_create_connection(address, *args, **kwargs):
        del args, kwargs
        attempts.append(repr(address))
        raise RuntimeError('network access is forbidden during the frozen journey')

    socket.socket.connect = blocked_connect
    socket.socket.connect_ex = blocked_connect_ex
    socket.create_connection = blocked_create_connection
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _journey_error(exc: Exception, root: Path) -> str:
    text = f'{type(exc).__name__}: {exc}'
    return text.replace(str(root), '<journey-root>')


def _journey_phase(report: dict, root: Path, name: str,
                   operation: Callable[[], object]) -> object:
    started = time.monotonic()
    try:
        detail = operation()
    except Exception as exc:
        report['phases'].append({
            'name': name,
            'status': 'failed',
            'duration_ms': round((time.monotonic() - started) * 1000),
            'error': _journey_error(exc, root),
        })
        report['ok'] = False
        raise _JourneyStopped(name) from exc
    report['phases'].append({
        'name': name,
        'status': 'passed',
        'duration_ms': round((time.monotonic() - started) * 1000),
        'detail': detail,
    })
    return detail


def run_bridge_journey(*, journey_root: Path | None = None) -> dict:
    """Exercise the real bridge, report service, and persistent local journals."""

    temporary = None
    if journey_root is None:
        temporary = tempfile.TemporaryDirectory(prefix='vcstudio-frozen-journey-')
        root = Path(temporary.name).resolve()
    else:
        root = Path(journey_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
    isolation = root / 'isolation'
    home = isolation / 'home'
    appdata = isolation / 'appdata'
    tmp = isolation / 'tmp'
    for directory in (home, appdata, tmp, isolation / 'mpl', isolation / 'pycache'):
        directory.mkdir(parents=True, exist_ok=True)
    environment = {
        'HOME': str(home),
        'USERPROFILE': str(home),
        'APPDATA': str(appdata),
        'LOCALAPPDATA': str(appdata),
        'XDG_CONFIG_HOME': str(isolation / 'xdg-config'),
        'XDG_CACHE_HOME': str(isolation / 'xdg-cache'),
        'VCSTUDIO_CONFIG': str(appdata / 'vcstudio' / 'config.yaml'),
        'MPLCONFIGDIR': str(isolation / 'mpl'),
        'TMP': str(tmp),
        'TEMP': str(tmp),
        'PYTHONPYCACHEPREFIX': str(isolation / 'pycache'),
        'PYTHONNOUSERSITE': '1',
    }
    report = {
        'schema': JOURNEY_SCHEMA,
        'ok': True,
        'phases': [],
        'network_attempts': [],
        'isolation': {
            'temporary': journey_root is None,
            'user_config_isolated': True,
            'repository_writes_allowed': False,
        },
    }
    state: dict[str, object] = {'root': root}
    attempts: list[str] = []
    try:
        with _temporary_environment(environment), _offline_network_guard() as attempts:
            def check_isolation():
                from vcstudio.cluster import ledger
                from vcstudio.project import adsorption
                from vcstudio.shared import config

                paths = {
                    'config': config.writable_config_path(),
                    'project_registry': adsorption.default_registry_path(),
                    'job_ledger': ledger.default_ledger_path(),
                }
                outside = [name for name, path in paths.items()
                           if not _inside(root, Path(path))]
                if outside:
                    raise RuntimeError(
                        'journey persistence escaped isolation: ' + ', '.join(outside))
                return {'persistence_roots': sorted(paths), 'all_inside_isolation': True}

            _journey_phase(report, root, 'isolation', check_isolation)

            def start_bridge():
                from vcstudio.gui_web.api import Api

                api = Api()
                service = api._reports()
                state.update(api=api, service=service)
                return {
                    'api_class': f'{type(api).__module__}.{type(api).__name__}',
                    'report_service_class': (
                        f'{type(service).__module__}.{type(service).__name__}'),
                }

            _journey_phase(report, root, 'bridge-start', start_bridge)

            def create_fixture():
                from vcstudio.cluster import ledger
                from vcstudio.project import adsorption
                from vcstudio.shared import manifest

                project_root = isolation / 'project'
                clean = project_root / 'clean'
                configuration = project_root / 'configuration'
                clean.mkdir(parents=True)
                configuration.mkdir()
                jobs = ((clean, 'journey-clean'),
                        (configuration, 'journey-configuration'))
                for directory, job_id in jobs:
                    value = manifest.new_manifest(
                        job_id=job_id,
                        system='offline-journey',
                        task_type='relax',
                        calc_type='slab',
                        inputs={'engine': 'vasp', 'sha256': {}},
                    )
                    manifest.save_manifest(directory, value)
                    ledger.register(directory)
                project = {
                    'schema': 'vcstudio.adsorption-project/v1',
                    'name': 'offline-journey-project',
                    'project_uuid': '1234567890abcdef1234567890abcdef',
                    'root': str(project_root),
                    'members': {
                        'clean_slab': str(clean),
                        'gas_ref': None,
                        'configs': [str(configuration)],
                    },
                    'config_species': {str(configuration): 'H'},
                }
                project_path = adsorption.save_project(project_root, project)
                adsorption.register_project(project_path)
                api = state['api']
                listing = api.proj_list()
                if listing.get('error') or len(listing.get('projects') or []) != 1:
                    raise RuntimeError(listing.get('error') or 'project registration failed')
                project_id = listing['projects'][0]['project_id']
                state.update(
                    project_path=project_path,
                    project_id=project_id,
                    job_ids=[item[1] for item in jobs],
                    report_dir=isolation / 'reports',
                )
                return {
                    'project_id': project_id,
                    'registered_projects': 1,
                    'registered_jobs': len(jobs),
                    'job_states': ['CREATED'],
                    'scientific_evidence': 'incomplete',
                }

            _journey_phase(report, root, 'offline-fixture-register', create_fixture)

            def check_jobs():
                api = state['api']
                jobs = api.list_jobs()
                if jobs.get('error') or jobs.get('stale'):
                    raise RuntimeError(jobs.get('error') or 'job ledger contains stale rows')
                rows = jobs.get('jobs') or []
                expected = set(state['job_ids'])
                visible = {row.get('id') for row in rows}
                if visible != expected:
                    raise RuntimeError('registered jobs are not visible through Api.list_jobs')
                if any(row.get('project_id') != state['project_id'] for row in rows):
                    raise RuntimeError('visible jobs are not bound to the registered project')
                return {'visible_jobs': len(rows), 'states': sorted({row['state'] for row in rows})}

            _journey_phase(report, root, 'jobs-visible', check_jobs)

            def check_analysis():
                api = state['api']
                project_id = state['project_id']
                bootstrap = api.analysis_workbench_bootstrap(project_id)
                preview = api.analysis_workbench_preview(project_id, {
                    'analysis_id': 'adsorption-energy',
                    'project_id': project_id,
                    'data_mode': 'all',
                })
                if bootstrap.get('ok') is not True or preview.get('ok') is not True:
                    raise RuntimeError(
                        bootstrap.get('error') or preview.get('error') or
                        'analysis workbench did not produce a view')
                view = preview.get('view') or {}
                denominator = view.get('denominator') or {}
                if (view.get('scientific_status') != 'unverified'
                        or denominator.get('numeric_configurations') != 0
                        or denominator.get('missing_configurations') != 1):
                    raise RuntimeError('analysis did not remain evidence-blocked')
                return {
                    'api_ok': True,
                    'acceptance_state': 'blocked',
                    'scientific_status': view.get('scientific_status'),
                    'denominator': denominator,
                    'data_fingerprint': view.get('data_fingerprint'),
                }

            _journey_phase(report, root, 'analysis-bootstrap-preview', check_analysis)

            def publish_diagnostic_report():
                api = state['api']
                project_id = state['project_id']
                request = {
                    'operation_id': 'frozen-journey-preview',
                    'spec': {
                        'preset_id': 'diagnostic-repair',
                        'requested_kind': 'diagnostic',
                        'formats': ['html'],
                        'scope': {
                            'kind': 'project',
                            'project_ids': [project_id],
                            'job_ids': [],
                            'species': [],
                            'configuration_ids': [],
                            'stable_only': False,
                            'include_failed': True,
                        },
                    },
                }
                bootstrap = api.report_workbench_bootstrap(
                    project_id, 'diagnostic-repair')
                preview = api.report_workbench_preview(project_id, request)
                if bootstrap.get('ok') is not True or preview.get('ok') is not True:
                    raise RuntimeError(
                        bootstrap.get('error') or preview.get('error') or
                        'diagnostic report preview failed')
                if (preview.get('scientific_status') != 'diagnostic'
                        or preview.get('publication_gate_status') != 'blocked'
                        or not str(preview.get('html') or '').lstrip().lower().startswith('<!doctype html')):
                    raise RuntimeError('preview incorrectly upgraded incomplete evidence')
                state['report_dir'].mkdir(parents=True, exist_ok=True)
                api._dialog_fn = lambda kind: (
                    str(state['report_dir']) if kind == 'dir' else None)
                selected = api.report_workbench_pick_destination(project_id)
                if selected.get('ok') is not True or not selected.get('destination_token'):
                    raise RuntimeError(
                        selected.get('error') or 'report destination selection failed')
                published = api.report_workbench_publish(
                    project_id,
                    selected['destination_token'],
                    preview['preview_id'],
                    preview['preview_token'],
                )
                if (published.get('ok') is not True
                        or published.get('scientific_status') != 'diagnostic'
                        or published.get('publication_gate_status') != 'blocked'):
                    raise RuntimeError(
                        published.get('error') or 'diagnostic report publication failed')
                state.update(
                    revision_id=published['revision']['revision_id'],
                    report_model_sha256=published['report_model_sha256'],
                )
                return {
                    'preview_status': preview.get('artifact_status'),
                    'scientific_status': published.get('scientific_status'),
                    'qualification': published.get('scientific_qualification'),
                    'publication_gate_status': published.get('publication_gate_status'),
                    'revision_id': published['revision']['revision_id'],
                    'html_available': published['files']['html']['available'],
                }

            _journey_phase(report, root, 'diagnostic-report-publish', publish_diagnostic_report)

            def verify_report():
                api = state['api']
                history = api.report_workbench_history(state['project_id'])
                status = api.proj_report_status(state['project_id'])
                revisions = history.get('revisions') or []
                if (history.get('ok') is not True or len(revisions) != 1
                        or revisions[0].get('current') is not True
                        or revisions[0].get('artifact_status') != 'ready'):
                    raise RuntimeError(history.get('error') or 'report history is not current')
                if (status.get('ok') is not True
                        or status.get('artifact_current') is not True
                        or status.get('scientific_status') != 'diagnostic'):
                    raise RuntimeError(status.get('error') or 'report marker is not current')
                html = Path(status['files']['html']).resolve()
                if not _inside(root, html) or not html.is_file():
                    raise RuntimeError('published HTML escaped journey isolation')
                text = html.read_text(encoding='utf-8')
                if '<html' not in text.lower():
                    raise RuntimeError('published HTML is malformed')
                state['manifest_sha256'] = revisions[0]['manifest_sha256']
                return {
                    'history_revisions': len(revisions),
                    'history_current': True,
                    'artifact_status': status.get('artifact_status'),
                    'html_sha256': _file_sha256(html),
                }

            _journey_phase(report, root, 'history-status-current', verify_report)

            def restart_bridge():
                from vcstudio.gui_web.api import Api

                old_api = state['api']
                old_service = state['service']
                new_api = Api()
                new_service = new_api._reports()
                if new_api is old_api or new_service is old_service:
                    raise RuntimeError('Api or ReportService was not recreated')
                old_service.close()
                state.update(api=new_api, service=new_service)
                del old_api, old_service
                gc.collect()
                return {
                    'api_recreated': True,
                    'report_service_recreated': True,
                    'api_class': f'{type(new_api).__module__}.{type(new_api).__name__}',
                    'report_service_class': (
                        f'{type(new_service).__module__}.{type(new_service).__name__}'),
                }

            _journey_phase(report, root, 'bridge-restart', restart_bridge)

            def verify_persistence():
                api = state['api']
                listing = api.proj_list()
                jobs = api.list_jobs()
                history = api.report_workbench_history(state['project_id'])
                status = api.proj_report_status(state['project_id'])
                analysis = api.analysis_workbench_bootstrap(state['project_id'])
                projects = listing.get('projects') or []
                revisions = history.get('revisions') or []
                if (listing.get('error') or len(projects) != 1
                        or projects[0].get('project_id') != state['project_id']):
                    raise RuntimeError(listing.get('error') or 'project registry did not persist')
                if (jobs.get('error') or len(jobs.get('jobs') or []) != 2):
                    raise RuntimeError(jobs.get('error') or 'job ledger did not persist')
                if (history.get('ok') is not True or len(revisions) != 1
                        or revisions[0].get('revision_id') != state['revision_id']
                        or revisions[0].get('manifest_sha256') != state['manifest_sha256']
                        or revisions[0].get('current') is not True):
                    raise RuntimeError(history.get('error') or 'revision history did not persist')
                if (status.get('ok') is not True
                        or status.get('artifact_current') is not True
                        or status.get('scientific_status') != 'diagnostic'
                        or status.get('publication_gate_status') != 'blocked'):
                    raise RuntimeError(status.get('error') or 'report status did not persist')
                if analysis.get('ok') is not True:
                    raise RuntimeError(analysis.get('error') or 'analysis did not reopen')
                return {
                    'project_persisted': True,
                    'jobs_persisted': len(jobs['jobs']),
                    'history_persisted': True,
                    'status_current': True,
                    'scientific_status': status.get('scientific_status'),
                    'publication_gate_status': status.get('publication_gate_status'),
                }

            _journey_phase(report, root, 'restart-persistence', verify_persistence)

            def check_offline():
                if attempts:
                    raise RuntimeError(
                        f'journey attempted {len(attempts)} network connection(s)')
                return {'connection_attempts': 0, 'cluster_operations': 0}

            _journey_phase(report, root, 'offline-boundary', check_offline)
    except _JourneyStopped:
        pass
    finally:
        service = state.get('service')
        if service is not None:
            with contextlib.suppress(Exception):
                service.close()
        report['network_attempts'] = list(attempts)
        report['phase_count'] = len(report['phases'])
        if temporary is not None:
            temporary.cleanup()
    return report


class _AssetReferences(HTMLParser):
    """Collect local ``src``/``href`` references from the bundled entry page."""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {'src', 'href'} and value:
                self.references.append(value)


def _module_version(module) -> str:
    return str(getattr(module, '__version__', getattr(module, 'VERSION', 'unknown')))


def _probe_core_modules() -> dict:
    versions = {}
    for name in ('vcstudio', 'yaml', 'paramiko', 'keyring', 'webview'):
        module = importlib.import_module(name)
        versions[name] = _module_version(module)

    yaml = importlib.import_module('yaml')
    if yaml.safe_load('healthcheck: true') != {'healthcheck': True}:
        raise RuntimeError('PyYAML functional probe returned unexpected data')

    keyring = importlib.import_module('keyring')
    backend = keyring.get_keyring()
    if backend is None:
        raise RuntimeError('keyring did not resolve a runtime backend')

    cli = importlib.import_module('vcstudio.cli.main')
    api = importlib.import_module('vcstudio.gui_web.api')
    if not callable(getattr(cli, 'main', None)) or not isinstance(getattr(api, 'Api', None), type):
        raise RuntimeError('critical internal entry modules are incomplete')
    for module_name, attributes in WORKBENCH_MODULE_CONTRACTS.items():
        module = importlib.import_module(module_name)
        missing = [name for name in attributes if not hasattr(module, name)]
        if missing:
            raise RuntimeError(
                f'workbench module {module_name} lacks required attributes: {missing!r}'
            )
        versions[module_name] = 'available'
    versions['keyring_backend'] = f'{type(backend).__module__}.{type(backend).__name__}'
    return versions


def _probe_numeric_and_chart_modules(*, charts: bool) -> dict:
    numpy = importlib.import_module('numpy')
    values = numpy.asarray([1.0, 2.0, 3.0])
    if float(values.sum()) != 6.0:
        raise RuntimeError('NumPy functional probe returned an unexpected result')
    versions = {'numpy': _module_version(numpy)}

    if charts:
        matplotlib = importlib.import_module('matplotlib')
        matplotlib.use('Agg', force=True)
        figure_module = importlib.import_module('matplotlib.figure')
        backend_module = importlib.import_module('matplotlib.backends.backend_agg')
        figure = figure_module.Figure(figsize=(1, 1))
        figure.add_subplot(111).plot([0, 1], [0, 1])
        canvas = backend_module.FigureCanvasAgg(figure)
        canvas.draw()
        if not canvas.buffer_rgba():
            raise RuntimeError('Matplotlib Agg renderer produced no pixels')
        versions['matplotlib'] = _module_version(matplotlib)
    return versions


def _probe_molecule_and_document_modules() -> dict:
    rdkit = importlib.import_module('rdkit')
    chem = importlib.import_module('rdkit.Chem')
    if chem.MolFromSmiles('O') is None:
        raise RuntimeError('RDKit failed to parse a minimal molecule')

    docx = importlib.import_module('docx')
    document = docx.Document()
    document.add_paragraph('VASP Catalyst Studio healthcheck')
    docx_bytes = BytesIO()
    document.save(docx_bytes)
    if not docx_bytes.getvalue().startswith(b'PK'):
        raise RuntimeError('python-docx failed to produce an OOXML package')

    pypdf = importlib.import_module('pypdf')
    pdf_writer = pypdf.PdfWriter()
    pdf_writer.add_blank_page(width=72, height=72)
    pypdf_bytes = BytesIO()
    pdf_writer.write(pypdf_bytes)
    if not pypdf_bytes.getvalue().startswith(b'%PDF'):
        raise RuntimeError('pypdf failed to produce a PDF')

    reportlab = importlib.import_module('reportlab')
    canvas_module = importlib.import_module('reportlab.pdfgen.canvas')
    reportlab_bytes = BytesIO()
    canvas = canvas_module.Canvas(reportlab_bytes, pagesize=(72, 72))
    canvas.drawString(4, 36, 'VCS')
    canvas.save()
    if not reportlab_bytes.getvalue().startswith(b'%PDF'):
        raise RuntimeError('ReportLab failed to produce a PDF')

    return {
        'rdkit': _module_version(rdkit),
        'docx': _module_version(docx),
        'pypdf': _module_version(pypdf),
        'reportlab': _module_version(reportlab),
    }


def _local_asset_path(asset_root: Path, reference: str) -> Path | None:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or reference.startswith(('#', 'data:', 'mailto:')):
        return None
    relative = unquote(parsed.path).lstrip('/')
    if not relative:
        return None
    root = asset_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f'asset reference escapes bundle root: {reference!r}') from exc
    return candidate


def _probe_web_assets(asset_root: Path) -> dict:
    from vcstudio.gui_web import resources

    root = asset_root.resolve()
    located = resources.asset_dir().resolve()
    if located != root:
        raise RuntimeError(f'runtime asset locator mismatch: {located} != {root}')

    index = root / 'index.html'
    if not index.is_file():
        raise RuntimeError(f'missing Web entry asset: {index}')
    text = index.read_text(encoding='utf-8')
    parser = _AssetReferences()
    parser.feed(text)
    local_references = []
    missing = []
    for reference in parser.references:
        candidate = _local_asset_path(root, reference)
        if candidate is None:
            continue
        local_references.append(reference)
        if not candidate.is_file():
            missing.append(reference)
    if missing:
        raise RuntimeError('missing assets referenced by index.html: ' + ', '.join(sorted(missing)))

    first_party_js = sorted(path.name for path in root.glob('*.js'))
    stylesheets = sorted(path.name for path in root.glob('*.css'))
    vendor_js = sorted(path.name for path in (root / 'vendor').glob('*.js'))
    fonts = sorted(path.name for path in (root / 'fonts').glob('*') if path.is_file())
    if not first_party_js or not stylesheets or not vendor_js or not fonts:
        raise RuntimeError('Web asset tree is incomplete (JS/CSS/vendor/font resources required)')
    return {
        'root': str(root),
        'index_bytes': index.stat().st_size,
        'local_references': len(local_references),
        'first_party_js': len(first_party_js),
        'stylesheets': len(stylesheets),
        'vendor_js': len(vendor_js),
        'fonts': len(fonts),
    }


def _probe_locales(locale_root: Path) -> dict:
    from vcstudio.shared import i18n

    root = locale_root.resolve()
    located = Path(i18n._LOCALES_DIR).resolve()
    if located != root:
        raise RuntimeError(f'runtime locale locator mismatch: {located} != {root}')

    languages = i18n.available_langs()
    if not {'zh', 'en'} <= set(languages):
        raise RuntimeError(f'required locales are missing: installed={languages!r}')
    zh = i18n.load_locale('zh')
    en = i18n.load_locale('en')
    if not zh or set(zh) != set(en) or i18n.missing_keys('en'):
        raise RuntimeError('zh/en locale dictionaries are empty or have different keys')
    for language, table in (('zh', zh), ('en', en)):
        if not isinstance(table.get('app.subtitle'), str) or not table['app.subtitle'].strip():
            raise RuntimeError(f'{language} locale lacks app.subtitle')
    return {'root': str(root), 'languages': languages, 'keys': len(zh)}


def _run_check(report: dict, name: str, probe: Callable[[], object]) -> None:
    try:
        detail = probe()
    except Exception as exc:  # healthcheck must preserve all independent failures
        report['checks'][name] = {
            'ok': False,
            'error': f'{type(exc).__name__}: {exc}',
        }
        report['ok'] = False
    else:
        report['checks'][name] = {'ok': True, 'detail': detail}


def run_healthcheck(
        *, profile: str = 'full', require_frozen: bool = True,
        bundle_root: Path | None = None, asset_root: Path | None = None,
        locale_root: Path | None = None) -> dict:
    """Return a JSON-safe report for one source or frozen runtime.

    Production callers leave ``require_frozen`` enabled.  The injectable roots exist
    solely so focused tests can validate the check logic without building an EXE.
    """
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f'unsupported healthcheck profile: {profile}')

    frozen = bool(getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'))
    if bundle_root is None:
        bundle_root = (
            Path(sys._MEIPASS) if frozen
            else Path(__file__).resolve().parents[2]
        )
    bundle_root = Path(bundle_root).resolve()
    if asset_root is None:
        from vcstudio.gui_web import resources
        asset_root = resources.asset_dir()
    if locale_root is None:
        from vcstudio.shared import i18n
        locale_root = Path(i18n._LOCALES_DIR)

    report = {
        'schema': 1,
        'ok': True,
        'profile': profile,
        'frozen': frozen,
        'bundle_root': str(bundle_root),
        'checks': {},
    }
    _run_check(
        report,
        'frozen-runtime',
        lambda: _probe_frozen_runtime(frozen, bundle_root, require_frozen),
    )
    _run_check(report, 'core-modules', _probe_core_modules)
    _run_check(report, 'web-assets', lambda: _probe_web_assets(Path(asset_root)))
    _run_check(report, 'locales', lambda: _probe_locales(Path(locale_root)))
    if profile in {'full', 'no-charts'}:
        _run_check(
            report,
            'numeric-and-charts',
            lambda: _probe_numeric_and_chart_modules(charts=profile == 'full'),
        )
        _run_check(report, 'molecule-and-documents', _probe_molecule_and_document_modules)
    if profile == 'journey':
        journey = run_bridge_journey()
        report['checks']['bridge-journey'] = {
            'ok': journey['ok'],
            'detail': journey,
        }
        if not journey['ok']:
            report['ok'] = False
    return report


def _probe_frozen_runtime(frozen: bool, bundle_root: Path, require_frozen: bool) -> dict:
    if require_frozen and not frozen:
        raise RuntimeError('healthcheck must run from the PyInstaller executable')
    if not bundle_root.is_dir():
        raise RuntimeError(f'bundle root does not exist: {bundle_root}')
    return {'required': require_frozen, 'bundle_root_exists': True}


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run the frozen executable healthcheck')
    parser.add_argument('--healthcheck', action='store_true', required=True)
    parser.add_argument('--healthcheck-profile', choices=SUPPORTED_PROFILES, default='full')
    parser.add_argument('--healthcheck-output', type=Path)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    report = run_healthcheck(profile=args.healthcheck_profile, require_frozen=True)
    if args.healthcheck_output is not None:
        try:
            _write_report(args.healthcheck_output, report)
        except OSError:
            return 2
    if sys.stdout is not None:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
