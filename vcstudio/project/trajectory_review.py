"""结构—轨迹导航与透明恢复审阅服务。

本模块不定义 NEB 能垒、AIMD 漂移或收敛判据。数值字段只消费现有
``project.neb`` / ``cluster.convergence`` / ``project.task_analysis`` 解析结果；本层负责：

* 服务端文件快照、完整性与 stale 判定；
* XDATCAR/NEB 结构帧的有界索引、分页和 opaque token；
* 结构异常的只读展示；
* 现有 ``job.yaml.results.diagnosis`` 的修复预览；
* 冻结 INCAR 的既有有界续算前后，不可变 correction record 的留痕。

浏览器永远拿不到本地路径，也不能用 token 指定或拼接文件名。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path


TRAJECTORY_SCHEMA = 'vcstudio.trajectory-player/v1'
STEP_PAGE_SCHEMA = 'vcstudio.trajectory-page/v1'
FRAME_SCHEMA = 'vcstudio.trajectory-frame/v1'
REPAIR_SCHEMA = 'vcstudio.repair-review/v1'
CORRECTION_SCHEMA = 'vcstudio.correction-record/v1'

_JOB_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:~-]{0,159}')
_OPERATION_KEY_RE = re.compile(r'[A-Za-z0-9_.:-]{12,128}')
_FRAME_DIR_RE = re.compile(r'^\d+$')
_E0_RE = re.compile(r'\bE0=\s*([-+0-9.Ee]+)')
_TEMP_RE = re.compile(r'\bT=\s*([-+0-9.Ee]+)')
_STEP_RE = re.compile(r'^\s*(\d+)\s+F=')
_XDAT_MARKER_RE = re.compile(
    rb'^\s*(Direct|Cartesian)\s+configuration\s*=\s*(\d+)\s*$', re.IGNORECASE)
_MAX_PAGE = 200
_MAX_PLOT_POINTS = 1000
_MAX_SESSIONS = 32
_MAX_FRAME_TOKENS = 8192
_SESSION_TTL_SECONDS = 30 * 60
_PAGE_DISTANCE_MAX_ATOMS = 200
_CORRECTION_DIR = '.vcstudio-corrections'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')


def _json_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _source_snapshot(root: Path, paths: list[Path]) -> tuple[list[dict], bool]:
    """Hash an allow-listed file set and report writes that raced the snapshot."""
    rows, stable = [], True
    for path in paths:
        try:
            before = path.stat()
            digest = _file_hash(path)
            after = path.stat()
        except OSError:
            stable = False
            continue
        unchanged = (before.st_size == after.st_size
                     and before.st_mtime_ns == after.st_mtime_ns)
        stable = stable and unchanged
        rows.append({
            'name': path.relative_to(root).as_posix(),
            'path': path,
            'size': after.st_size,
            'mtime_ns': after.st_mtime_ns,
            'sha256': digest,
        })
    public = [
        {'name': row['name'], 'size': row['size'], 'sha256': row['sha256']}
        for row in rows
    ]
    source_hash = _json_hash(public)
    for row in rows:
        row['source_hash'] = source_hash
    return rows, stable


def _snapshot_is_current(rows: list[dict]) -> bool:
    for row in rows:
        try:
            stat = row['path'].stat()
        except OSError:
            return False
        if stat.st_size != row['size'] or stat.st_mtime_ns != row['mtime_ns']:
            return False
    return True


def _read_text(path: Path) -> tuple[str, bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return '', False
    complete_line = not data or data.endswith((b'\n', b'\r'))
    return data.decode('utf-8', errors='replace'), complete_line


def _finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_oszicar(path: Path) -> tuple[list[dict], bool]:
    """Project the raw per-step fields without defining a drift/convergence result."""
    text, complete_line = _read_text(path)
    if not text:
        return [], complete_line
    rows = []
    previous = None
    lines = text.splitlines()
    # A non-newline-terminated summary is an in-flight write and is withheld.
    if not complete_line and lines:
        lines = lines[:-1]
    for line in lines:
        if ' F=' not in line or 'E0=' not in line:
            continue
        energy_match = _E0_RE.search(line)
        if not energy_match:
            continue
        energy = _finite(energy_match.group(1))
        if energy is None:
            continue
        step_match = _STEP_RE.match(line)
        step = int(step_match.group(1)) if step_match else len(rows) + 1
        temp_match = _TEMP_RE.search(line)
        temperature = _finite(temp_match.group(1)) if temp_match else None
        rows.append({
            'step': step,
            'energy_ev': energy,
            'delta_energy_ev': None if previous is None else abs(energy - previous),
            'temperature_k': temperature,
            'fmax_ev_a': None,
        })
        previous = energy
    return rows, complete_line


def _parse_outcar_fmax(path: Path, convergence_mod) -> tuple[list[float], bool]:
    try:
        size = path.stat().st_size
        with path.open('rb') as raw:
            if size:
                raw.seek(-1, os.SEEK_END)
                complete_line = raw.read(1) in (b'\n', b'\r')
            else:
                complete_line = True
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            # The numerical parser remains the single source of force semantics.
            values = list(convergence_mod.parse_outcar_fmax_lines(handle))
        return values, complete_line
    except OSError:
        return [], False


def _xdatcar_index(path: Path) -> tuple[dict | None, list[dict], bool, list[str]]:
    """Index complete XDATCAR frames by byte offset; coordinates stay on disk."""
    warnings = []
    try:
        with path.open('rb') as handle:
            header_lines = [handle.readline() for _ in range(7)]
            if any(not line for line in header_lines):
                return None, [], False, ['XDATCAR 头部不完整。']
            header_text = b''.join(header_lines).decode('utf-8', errors='replace')
            from vcstudio.generate.poscar import parse_poscar_species

            elements, counts = parse_poscar_species(header_text)
            if not elements or not counts:
                return None, [], False, ['XDATCAR 物种/原子计数不可解析。']
            natoms = sum(counts)
            frames = []
            complete = True
            while True:
                offset = handle.tell()
                marker = handle.readline()
                if not marker:
                    break
                if not marker.endswith((b'\n', b'\r')):
                    complete = False
                    break
                match = _XDAT_MARKER_RE.match(marker.strip())
                if not match:
                    if marker.strip():
                        complete = False
                        warnings.append('XDATCAR 帧标记异常，后续帧未纳入索引。')
                    break
                coordinate_lines = []
                for _ in range(natoms):
                    line = handle.readline()
                    if not line or not line.endswith((b'\n', b'\r')):
                        complete = False
                        break
                    coordinate_lines.append(line)
                if len(coordinate_lines) != natoms:
                    warnings.append('XDATCAR 末帧仍在写入，已只暴露此前完整帧。')
                    break
                frames.append({
                    'kind': 'xdatcar', 'path': path, 'offset': offset,
                    'mode': match.group(1).decode('ascii').title(),
                    'configuration': int(match.group(2)), 'natoms': natoms,
                    'header': header_text,
                })
            return {
                'elements': elements, 'counts': counts, 'natoms': natoms,
            }, frames, complete, warnings
    except OSError as exc:
        return None, [], False, [f'XDATCAR 无法读取：{exc}']


def _load_xdatcar_frame(source: dict) -> str:
    path, natoms = source['path'], int(source['natoms'])
    with path.open('rb') as handle:
        handle.seek(int(source['offset']))
        marker = handle.readline()
        match = _XDAT_MARKER_RE.match(marker.strip())
        if not match:
            raise ValueError('XDATCAR 帧标记已变化，请刷新播放器快照')
        coordinates = [handle.readline() for _ in range(natoms)]
    if len(coordinates) != natoms or any(not line for line in coordinates):
        raise ValueError('XDATCAR 帧不完整，请等待写入完成后刷新')
    mode = match.group(1).decode('ascii').title()
    coordinate_text = b''.join(coordinates).decode('utf-8', errors='replace')
    return source['header'].rstrip('\r\n') + f'\n{mode}\n' + coordinate_text


def _structure_file_source(path: Path) -> dict:
    text, complete = _read_text(path)
    if not text:
        raise ValueError('结构文件为空')
    try:
        from vcstudio.generate.poscar import parse_poscar_species

        _elements, counts = parse_poscar_species(text)
        natoms = sum(counts)
    except (TypeError, ValueError):
        natoms = 0
    return {'kind': 'structure_file', 'path': path, 'natoms': natoms,
            'complete': complete}


def _load_structure_text(source: dict) -> str:
    if source['kind'] == 'xdatcar':
        return _load_xdatcar_frame(source)
    text, complete = _read_text(source['path'])
    if not text or not complete:
        raise ValueError('结构帧仍在写入或不可读，请刷新播放器快照')
    return text


_REPAIR_GUIDANCE = {
    'NONCONVERGED': {
        'change': '从已验证 CONTCAR 继续，INCAR 逐字冻结。',
        'cost': 'one_bounded_restart', 'impact': '结构轨迹延长；方法参数不变。',
    },
    'WALLTIME': {
        'change': '从已验证 CONTCAR 继续，INCAR 逐字冻结。',
        'cost': 'one_bounded_restart', 'impact': '增加一轮队列与剩余计算；方法参数不变。',
    },
    'ZBRENT': {
        'change': '先尝试一次冻结 INCAR 的 CONTCAR 续算；若复发，暂停并人工评估 POTIM/IBRION。',
        'cost': 'one_bounded_restart', 'impact': '首次恢复不改方法；复发后的调参需另建方法审阅。',
    },
    'SCF_SLOSHING': {
        'change': '人工审阅 ALGO/AMIX/BMIX/SIGMA；本播放器不自动改参。',
        'cost': 'unknown_manual_rerun', 'impact': '电子收敛设置可能改变，需重新核对能量可比性。',
        'diff': [('ALGO', 'expert review'), ('AMIX/BMIX', 'expert review'),
                 ('SIGMA', 'expert review')],
    },
    'TOO_FEW_BANDS': {
        'change': '人工增加 NBANDS 后重新提交；本播放器不自动改参。',
        'cost': 'full_or_partial_manual_rerun', 'impact': '基组规模改变，后续比较必须记录新方法事实。',
        'diff': [('NBANDS', 'increase after expert review')],
    },
    'TETRAHEDRON': {
        'change': '人工审阅 ISMEAR/SIGMA 与 k 点；本播放器不自动改参。',
        'cost': 'full_manual_rerun', 'impact': '占据与积分口径改变，能量可比性必须重新审计。',
        'diff': [('ISMEAR', '0 or 1 after expert review'),
                 ('SIGMA', 'review with occupation method')],
    },
    'BRMIX': {
        'change': '人工检查结构/磁矩并审阅 AMIX/BMIX/IMIX；本播放器不自动改参。',
        'cost': 'unknown_manual_rerun', 'impact': '混合与可能的初始磁态改变，需重新审计。',
        'diff': [('AMIX/BMIX/IMIX', 'expert review')],
    },
    'ZPOTRF': {
        'change': '先检查原子重叠和晶胞塌缩；不要直接重投。',
        'cost': 'unknown_structure_repair', 'impact': '结构若被修改，将形成新的结构世代。',
    },
    'NEB_NPAR_DIVIDE': {
        'change': '人工核对 IMAGES 与 MPI/NCORE/KPAR；NEB 不走通用续算。',
        'cost': 'manual_neb_resubmission', 'impact': '并行设置变化；数值 NEB 口径仍由专用分析链复核。',
        'diff': [('NCORE/KPAR', 'compatible scheduler partition after review')],
    },
}


def correction_method_compatibility(job_dir) -> dict:
    """Validate immutable correction records for downstream method consumers."""
    root = Path(job_dir).resolve()
    directory = root / _CORRECTION_DIR
    if not directory.is_dir():
        return {'status': 'no_corrections', 'record_count': 0,
                'records': [], 'issues': []}
    records, issues = [], []
    for path in sorted(directory.glob('*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            supplied = str(payload.get('record_hash') or '')
            body = dict(payload)
            body.pop('record_hash', None)
            if (payload.get('schema') != CORRECTION_SCHEMA
                    or supplied != _json_hash(body)):
                raise ValueError('record hash mismatch')
            records.append({
                key: payload.get(key) for key in (
                    'record_id', 'record_type', 'created_at', 'status',
                    'failure_class', 'action', 'method_compatibility',
                    'record_hash', 'intent_record_hash',
                ) if payload.get(key) is not None
            })
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f'correction record {path.name} is invalid: {exc}')
    intents = {row.get('record_hash') for row in records
               if row.get('record_type') == 'repair_intent'}
    resolved = {row.get('intent_record_hash') for row in records
                if row.get('record_type') == 'repair_outcome'}
    if issues:
        status = 'unknown_invalid_record'
    elif intents - resolved:
        status = 'unknown_pending'
    else:
        applied = [row for row in records
                   if row.get('record_type') == 'repair_outcome'
                   and row.get('status') == 'applied']
        status = ('verified_unchanged' if applied and all(
            row.get('method_compatibility') == 'unchanged' for row in applied)
            else 'no_applied_corrections')
    return {'status': status, 'record_count': len(records),
            'records': records[-20:], 'issues': issues}


class TrajectoryReviewService:
    """Bounded in-memory trajectory sessions over server-resolved job roots."""

    def __init__(self, *, task_analysis_mod=None, neb_mod=None, convergence_mod=None,
                 structure_view_mod=None, manifest_mod=None, clock=None,
                 token_factory=None):
        from vcstudio.cluster import convergence
        from vcstudio.generate import structure_view
        from vcstudio.project import neb, task_analysis
        from vcstudio.shared import manifest

        self._task_analysis = task_analysis_mod or task_analysis
        self._neb = neb_mod or neb
        self._convergence = convergence_mod or convergence
        self._structure_view = structure_view_mod or structure_view
        self._manifest = manifest_mod or manifest
        self._clock = clock or time.monotonic
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, dict] = OrderedDict()
        self._frame_tokens: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._repair_tokens: OrderedDict[str, dict] = OrderedDict()

    def _cleanup(self) -> None:
        cutoff = self._clock() - _SESSION_TTL_SECONDS
        expired = [token for token, session in self._sessions.items()
                   if session['last_access'] < cutoff]
        for token in expired:
            self._sessions.pop(token, None)
        if expired:
            expired_set = set(expired)
            self._frame_tokens = OrderedDict(
                (token, value) for token, value in self._frame_tokens.items()
                if value[0] not in expired_set)
            self._repair_tokens = OrderedDict(
                (token, value) for token, value in self._repair_tokens.items()
                if value['session_token'] not in expired_set)
        while len(self._sessions) > _MAX_SESSIONS:
            token, _session = self._sessions.popitem(last=False)
            self._frame_tokens = OrderedDict(
                (key, value) for key, value in self._frame_tokens.items()
                if value[0] != token)
        while len(self._frame_tokens) > _MAX_FRAME_TOKENS:
            self._frame_tokens.popitem(last=False)

    @staticmethod
    def _source_paths(root: Path, kind: str) -> list[Path]:
        names = ('job.yaml', 'INCAR', 'OSZICAR', 'OUTCAR', 'XDATCAR',
                 'CONTCAR', 'POSCAR')
        paths = [root / name for name in names if (root / name).is_file()]
        if kind == 'neb':
            frames = sorted(
                (path for path in root.iterdir()
                 if path.is_dir() and _FRAME_DIR_RE.fullmatch(path.name)),
                key=lambda path: int(path.name))
            for frame in frames:
                for name in ('POSCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR'):
                    path = frame / name
                    if path.is_file():
                        paths.append(path)
        return paths

    def _build_neb(self, root: Path) -> tuple[list[dict], list[dict], bool, list[str], dict]:
        warnings = []
        try:
            parsed = self._neb.parse_neb_energies(root)
            gate = self._neb.neb_quality_gate(parsed)
        except (OSError, ValueError, TypeError) as exc:
            parsed, gate = {}, {'ok': False, 'issues': [str(exc)]}
            warnings.append(str(exc))
        frame_dirs = sorted(
            (path for path in root.iterdir()
             if path.is_dir() and _FRAME_DIR_RE.fullmatch(path.name)),
            key=lambda path: int(path.name))
        energies = list(parsed.get('energies') or [])
        relative = list(parsed.get('rel') or [])
        forces = list(parsed.get('per_image_forces') or [])
        rows, frames, partial = [], [], False
        for index, frame_dir in enumerate(frame_dirs):
            structure_path = next(
                (frame_dir / name for name in ('CONTCAR', 'POSCAR')
                 if (frame_dir / name).is_file()), None)
            source_index = None
            if structure_path is not None:
                try:
                    source_index = len(frames)
                    frames.append(_structure_file_source(structure_path))
                except ValueError:
                    partial = True
                    source_index = None
            else:
                partial = True
            energy = _finite(energies[index]) if index < len(energies) else None
            rel = _finite(relative[index]) if index < len(relative) else None
            fmax = _finite(forces[index]) if index < len(forces) else None
            if energy is None:
                partial = True
            rows.append({
                'step': index + 1, 'image': frame_dir.name,
                'energy_ev': energy, 'relative_energy_ev': rel,
                'fmax_ev_a': fmax, 'temperature_k': None,
                'frame_source': source_index,
            })
        warnings.extend(str(item) for item in (parsed.get('warnings') or []))
        analysis = {
            'source': 'vcstudio.project.neb.parse_neb_energies',
            'quality_gate': gate,
            'n_frames': parsed.get('n_frames'),
            'ts_index': parsed.get('ts_index'),
            'barrier_forward_ev': parsed.get('barrier_f'),
            'barrier_reverse_ev': parsed.get('barrier_r'),
            'climbing_converged': parsed.get('climbing_converged'),
        }
        return rows, frames, partial, warnings, analysis

    def _build_series(self, root: Path, kind: str) -> tuple[list[dict], list[dict], bool, list[str], dict]:
        rows, osz_complete = _parse_oszicar(root / 'OSZICAR')
        forces, out_complete = _parse_outcar_fmax(root / 'OUTCAR', self._convergence)
        for index, value in enumerate(forces[:len(rows)]):
            rows[index]['fmax_ev_a'] = _finite(value)
        warnings = []
        partial = not osz_complete or not out_complete
        frames: list[dict] = []
        xdat_meta = None
        if (root / 'XDATCAR').is_file():
            xdat_meta, frames, xdat_complete, xdat_warnings = _xdatcar_index(root / 'XDATCAR')
            partial = partial or not xdat_complete
            warnings.extend(xdat_warnings)
        if frames:
            if len(frames) != len(rows):
                partial = True
                warnings.append(
                    f'完整结构帧 {len(frames)} 与能量步 {len(rows)} 数量不同；仅同步重叠部分。')
            for index, row in enumerate(rows):
                row['frame_source'] = index if index < len(frames) else None
        else:
            structure_path = next(
                (root / name for name in ('CONTCAR', 'POSCAR')
                 if (root / name).is_file()), None)
            if structure_path is not None:
                try:
                    frames.append(_structure_file_source(structure_path))
                    if not rows:
                        rows.append({'step': 1, 'energy_ev': None,
                                     'delta_energy_ev': None, 'temperature_k': None,
                                     'fmax_ev_a': None, 'frame_source': 0})
                    else:
                        for row in rows:
                            row['frame_source'] = None
                        rows[-1]['frame_source'] = 0
                    warnings.append('未找到完整 XDATCAR，仅末步结构可导航。')
                except ValueError:
                    partial = True
            elif rows:
                for row in rows:
                    row['frame_source'] = None
                partial = True
                warnings.append('未找到可用结构帧；曲线与步骤表仍可只读查看。')
        analysis = {'source': 'existing-parser', 'kind': kind}
        if kind == 'aimd':
            authoritative = self._task_analysis.analyze_aimd(root)
            result = authoritative.get('result') or {}
            analysis = {
                'source': 'vcstudio.project.task_analysis.analyze_aimd',
                'ok': authoritative.get('ok'), 'summary': authoritative.get('summary'),
                'n_steps': result.get('n_steps'),
                'energy_first_ev': result.get('energy_first_ev'),
                'energy_last_ev': result.get('energy_last_ev'),
                'energy_drift_total_ev': result.get('energy_drift_total_ev'),
                'temperature_mean_k': result.get('temperature_mean_k'),
            }
        if xdat_meta:
            analysis['trajectory_natoms'] = xdat_meta.get('natoms')
        return rows, frames, partial, warnings, analysis

    def open(self, job_dir, job_id: str, *, kind: str | None = None) -> dict:
        """Open a server-resolved job and return a path-free bounded overview."""
        identifier = str(job_id or '').strip()
        if not _JOB_ID_RE.fullmatch(identifier):
            raise ValueError('job_id is not a valid opaque identity')
        root = Path(job_dir).resolve()
        if not root.is_dir():
            raise ValueError('registered job is unavailable')
        value = self._manifest.load_manifest(root) or {}
        if not isinstance(value, dict):
            raise ValueError('registered job manifest is unreadable')
        task_kind = self._task_analysis.normalize_task_key(
            kind or value.get('task_type') or 'relax')
        paths = self._source_paths(root, task_kind)
        sources, hash_stable = _source_snapshot(root, paths)
        if not sources:
            raise ValueError('job has no trajectory or structure evidence')
        if task_kind == 'neb':
            rows, frames, partial, warnings, analysis = self._build_neb(root)
        else:
            rows, frames, partial, warnings, analysis = self._build_series(root, task_kind)
        current_paths = {path.resolve() for path in self._source_paths(root, task_kind)}
        snapshot_paths = {row['path'].resolve() for row in sources}
        source_set_stable = current_paths == snapshot_paths
        partial = (partial or not hash_stable or not source_set_stable
                   or not _snapshot_is_current(sources))
        if not hash_stable:
            warnings.append('源文件在快照期间发生变化；当前结果标为部分写入。')
        if not source_set_stable:
            warnings.append('解析期间源文件集合发生变化；当前结果标为部分写入。')
        source_hash = sources[0]['source_hash']
        token = f'trajectory-{self._token_factory()}'
        session = {
            'token': token, 'job_id': identifier, 'root': root,
            'kind': task_kind, 'manifest': value, 'rows': rows, 'frames': frames,
            'sources': sources, 'source_hash': source_hash,
            'partial_write': bool(partial), 'warnings': list(dict.fromkeys(warnings)),
            'analysis': analysis, 'created_at': _utc_now(),
            'last_access': self._clock(), 'metric_cache': {},
        }
        with self._lock:
            self._cleanup()
            self._sessions[token] = session
            self._sessions.move_to_end(token)
        return self._overview(session)

    def _session(self, token: str) -> dict:
        with self._lock:
            self._cleanup()
            session = self._sessions.get(str(token or ''))
            if session is None:
                raise LookupError('trajectory session expired or is unknown')
            session['last_access'] = self._clock()
            self._sessions.move_to_end(session['token'])
            return session

    @staticmethod
    def _stale_payload(session: dict, schema: str) -> dict:
        return {
            'schema': schema, 'ok': False, 'stale': True,
            'partial_write': bool(session['partial_write']),
            'session_token': session['token'],
            'source_hash': session['source_hash'],
            'error': 'trajectory source changed; refresh the player snapshot',
        }

    def _is_stale(self, session: dict) -> bool:
        current_paths = {
            path.resolve() for path in self._source_paths(session['root'], session['kind'])}
        snapshot_paths = {row['path'].resolve() for row in session['sources']}
        return current_paths != snapshot_paths or not _snapshot_is_current(session['sources'])

    @staticmethod
    def _plot(rows: list[dict]) -> tuple[list[dict], int]:
        count = len(rows)
        stride = max(1, math.ceil(count / _MAX_PLOT_POINTS))
        selected = list(range(0, count, stride))
        if count and selected[-1] != count - 1:
            selected.append(count - 1)
        points = []
        for index in selected:
            row = rows[index]
            points.append({
                'step': row.get('step'), 'image': row.get('image'),
                'energy_ev': row.get('energy_ev'),
                'relative_energy_ev': row.get('relative_energy_ev'),
                'fmax_ev_a': row.get('fmax_ev_a'),
                'temperature_k': row.get('temperature_k'),
            })
        return points, stride

    def _correction_projection(self, root: Path) -> dict:
        return correction_method_compatibility(root)

    def _overview(self, session: dict) -> dict:
        plot, plot_stride = self._plot(session['rows'])
        return {
            'schema': TRAJECTORY_SCHEMA, 'ok': True, 'stale': False,
            'partial_write': bool(session['partial_write']),
            'session_token': session['token'], 'job_id': session['job_id'],
            'task_kind': session['kind'], 'source_hash': session['source_hash'],
            'source_files': [
                {'name': row['name'], 'size': row['size'], 'sha256': row['sha256']}
                for row in session['sources']],
            'n_steps': len(session['rows']), 'n_frames': len(session['frames']),
            'plot': {'points': plot, 'sampling_stride': plot_stride,
                     'max_points': _MAX_PLOT_POINTS},
            'analysis': session['analysis'], 'warnings': session['warnings'],
            'method_compatibility': self._correction_projection(session['root']),
        }

    def _issue_frame_token(self, session: dict, frame_source: int) -> str:
        token = f'frame-{self._token_factory()}'
        with self._lock:
            self._frame_tokens[token] = (session['token'], int(frame_source))
            self._frame_tokens.move_to_end(token)
            self._cleanup()
        return token

    def _frame_metric(self, session: dict, frame_source: int) -> dict:
        cache = session['metric_cache']
        if frame_source in cache:
            return dict(cache[frame_source])
        source = session['frames'][frame_source]
        natoms = int(source.get('natoms') or 0)
        if natoms > _PAGE_DISTANCE_MAX_ATOMS:
            metric = {
                'minimum_distance_a': None, 'anomaly': 'unknown',
                'distance_status': 'deferred_large_structure',
                'notes': [f'{natoms} atoms; distance analysis is deferred to the selected frame.'],
            }
        else:
            try:
                view = self._structure_view.structure_view(_load_structure_text(source))
                gap = view.get('gap') or {}
                metric = {
                    'minimum_distance_a': gap.get('min_dist'),
                    'anomaly': gap.get('level') or 'none',
                    'distance_status': 'available',
                    'notes': list(gap.get('notes') or []),
                }
            except (OSError, ValueError) as exc:
                metric = {
                    'minimum_distance_a': None, 'anomaly': 'unknown',
                    'distance_status': 'unavailable', 'notes': [str(exc)],
                }
        cache[frame_source] = dict(metric)
        return metric

    def steps(self, session_token: str, *, offset=0, limit=50, stride=1) -> dict:
        session = self._session(session_token)
        if self._is_stale(session):
            return self._stale_payload(session, STEP_PAGE_SCHEMA)
        try:
            start = max(0, int(offset))
            page_limit = min(_MAX_PAGE, max(1, int(limit)))
            sample_stride = min(10000, max(1, int(stride)))
        except (TypeError, ValueError) as exc:
            raise ValueError('offset, limit and stride must be integers') from exc
        indexes = list(range(0, len(session['rows']), sample_stride))
        page_indexes = indexes[start:start + page_limit]
        public_rows = []
        for index in page_indexes:
            source = session['rows'][index]
            frame_source = source.get('frame_source')
            row = {key: source.get(key) for key in (
                'step', 'image', 'energy_ev', 'relative_energy_ev',
                'fmax_ev_a', 'temperature_k')}
            row['frame_token'] = None
            row.update({'minimum_distance_a': None, 'anomaly': 'unknown',
                        'distance_status': 'unavailable', 'notes': []})
            if isinstance(frame_source, int) and 0 <= frame_source < len(session['frames']):
                row['frame_token'] = self._issue_frame_token(session, frame_source)
                row.update(self._frame_metric(session, frame_source))
            public_rows.append(row)
        next_offset = start + len(page_indexes)
        return {
            'schema': STEP_PAGE_SCHEMA, 'ok': True, 'stale': False,
            'partial_write': bool(session['partial_write']),
            'session_token': session['token'], 'source_hash': session['source_hash'],
            'total_steps': len(session['rows']), 'sampled_steps': len(indexes),
            'offset': start, 'limit': page_limit, 'stride': sample_stride,
            'next_offset': next_offset if next_offset < len(indexes) else None,
            'rows': public_rows,
        }

    def frame(self, frame_token: str) -> dict:
        with self._lock:
            self._cleanup()
            binding = self._frame_tokens.get(str(frame_token or ''))
        if binding is None:
            raise LookupError('frame token expired or is unknown')
        session = self._session(binding[0])
        if self._is_stale(session):
            return self._stale_payload(session, FRAME_SCHEMA)
        frame_source = binding[1]
        if not 0 <= frame_source < len(session['frames']):
            raise LookupError('frame token no longer resolves')
        content = _load_structure_text(session['frames'][frame_source])
        view = self._structure_view.structure_view(content)
        metric = self._frame_metric(session, frame_source)
        return {
            'schema': FRAME_SCHEMA, 'ok': True, 'stale': False,
            'partial_write': bool(session['partial_write']),
            'session_token': session['token'], 'source_hash': session['source_hash'],
            'frame_token': str(frame_token), 'view': view, 'metrics': metric,
        }

    @staticmethod
    def _effective_incar(root: Path) -> dict[str, str]:
        text, _complete = _read_text(root / 'INCAR')
        values = {}
        for line in text.splitlines():
            code = re.split(r'[#!]', line, maxsplit=1)[0]
            if '=' not in code:
                continue
            key, value = code.split('=', 1)
            key = key.strip().upper()
            if re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}', key):
                values[key] = value.strip()
        return values

    def repair_preview(self, session_token: str) -> dict:
        session = self._session(session_token)
        if self._is_stale(session):
            return self._stale_payload(session, REPAIR_SCHEMA)
        manifest = self._manifest.load_manifest(session['root']) or {}
        results = manifest.get('results') or {}
        diagnosis = results.get('diagnosis') or {}
        failure_class = str(diagnosis.get('failure_class') or 'UNKNOWN')
        evidence = str(diagnosis.get('evidence') or 'No bounded error signature is available.')
        restartable = bool(diagnosis.get('restartable'))
        rounds = int(results.get('continue_rounds') or 0)
        guidance = _REPAIR_GUIDANCE.get(failure_class, {
            'change': '暂停并人工核对输出、调度器终态与方法设置；不自动修改或重试。',
            'cost': 'unknown', 'impact': '未知；在证据补齐前不得进入自动恢复。',
        })
        effective = self._effective_incar(session['root'])
        method_diff = []
        for key, proposed in guidance.get('diff', []):
            before = effective.get(key) if '/' not in key else None
            method_diff.append({
                'key': key, 'before': before, 'proposed': proposed,
                'execution': 'manual_only',
                'compatibility': 'requires_review',
            })
        try:
            from vcstudio.cluster.submitter import CONTINUE_MAX_ROUNDS
            max_rounds = int(CONTINUE_MAX_ROUNDS)
        except (ImportError, TypeError, ValueError):
            max_rounds = 3
        execution_allowed = (restartable and session['kind'] != 'neb'
                             and rounds < max_rounds
                             and failure_class != 'UNKNOWN')
        action = 'continue_frozen_incar' if execution_allowed else 'pause'
        plan_body = {
            'job_id': session['job_id'], 'source_hash': session['source_hash'],
            'failure_class': failure_class, 'diagnosis_hash': _json_hash(diagnosis),
            'action': action, 'continue_rounds': rounds,
            'incar_sha256': _file_hash(session['root'] / 'INCAR')
            if (session['root'] / 'INCAR').is_file() else None,
        }
        plan_id = _json_hash(plan_body)
        plan_token = f'repair-{self._token_factory()}'
        plan = {
            **plan_body, 'plan_id': plan_id, 'plan_token': plan_token,
            'session_token': session['token'], 'job_dir': session['root'],
            'detected_evidence': evidence, 'suggested_change': guidance['change'],
            'estimated_cost': {
                'class': guidance['cost'], 'additional_runs': 1 if execution_allowed else None,
                'remaining_bounded_rounds': max(0, max_rounds - rounds),
                'exact_machine_time': None,
            },
            'scientific_impact': guidance['impact'], 'method_diff': method_diff,
            'execution_allowed': execution_allowed,
        }
        with self._lock:
            self._repair_tokens[plan_token] = plan
            while len(self._repair_tokens) > 256:
                self._repair_tokens.popitem(last=False)
        return {
            'schema': REPAIR_SCHEMA, 'ok': True, 'stale': False,
            'partial_write': bool(session['partial_write']),
            'plan_token': plan_token, 'plan_id': plan_id,
            'failure_class': failure_class, 'detected_evidence': evidence,
            'suggested_change': guidance['change'],
            'estimated_cost': plan['estimated_cost'],
            'scientific_impact': guidance['impact'], 'method_diff': method_diff,
            'default_decision': 'pause', 'execution_action': action,
            'execution_allowed': execution_allowed,
            'unknown_pauses': failure_class == 'UNKNOWN',
            'correction_history': self._correction_projection(session['root']),
        }

    @staticmethod
    def _write_immutable_record(root: Path, filename: str, payload: dict) -> dict:
        directory = root / _CORRECTION_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / filename
        body = dict(payload)
        body['schema'] = CORRECTION_SCHEMA
        body['record_hash'] = _json_hash(body)
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = json.loads(path.read_text(encoding='utf-8'))
            existing_hash = str(existing.get('record_hash') or '')
            current = dict(existing)
            current.pop('record_hash', None)
            if existing_hash != _json_hash(current):
                raise RuntimeError('existing immutable correction record failed hash validation')
            return existing
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # The O_EXCL inode may exist only partially; leave it as explicit invalid evidence.
            raise
        return body

    def prepare_repair(self, plan_token: str, decision: str,
                       operation_key: str) -> dict:
        with self._lock:
            self._cleanup()
            plan = self._repair_tokens.get(str(plan_token or ''))
        if plan is None:
            raise LookupError('repair plan expired or is unknown')
        if decision != 'continue_frozen_incar' or not plan['execution_allowed']:
            raise ValueError('repair remains paused; this plan does not authorize execution')
        key = str(operation_key or '').strip()
        if not _OPERATION_KEY_RE.fullmatch(key):
            raise ValueError('a stable idempotency key is required for repair confirmation')
        session = self._session(plan['session_token'])
        if self._is_stale(session):
            raise RuntimeError('trajectory source changed; repair plan is stale')
        manifest = self._manifest.load_manifest(session['root']) or {}
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        if _json_hash(diagnosis) != plan['diagnosis_hash']:
            raise RuntimeError('diagnosis changed; refresh repair preview before confirming')
        correction_id = hashlib.sha256(
            f'{plan["plan_id"]}|{key}'.encode('utf-8')).hexdigest()[:24]
        record = self._write_immutable_record(
            session['root'], f'intent-{correction_id}.json', {
                'record_id': correction_id, 'record_type': 'repair_intent',
                'created_at': _utc_now(), 'status': 'prepared',
                'job_id': session['job_id'], 'source_hash': session['source_hash'],
                'plan_id': plan['plan_id'], 'failure_class': plan['failure_class'],
                'action': 'continue_frozen_incar', 'idempotency_key': key,
                'detected_evidence': plan['detected_evidence'],
                'suggested_change': plan['suggested_change'],
                'estimated_cost': plan['estimated_cost'],
                'scientific_impact': plan['scientific_impact'],
                'method_diff': [], 'incar_sha256_before': plan['incar_sha256'],
                'method_compatibility': 'pending',
            })
        return {
            'job_id': session['job_id'], 'job_dir': session['root'],
            'operation_key': key, 'plan_id': plan['plan_id'],
            'correction_id': correction_id, 'intent_record_hash': record['record_hash'],
            'incar_sha256_before': plan['incar_sha256'],
        }

    def record_repair_outcome(self, prepared: dict, outcome: dict) -> dict:
        root = Path(prepared['job_dir']).resolve()
        results = list(outcome.get('results') or []) if isinstance(outcome, dict) else []
        successful = bool(results) and all(bool(row[1]) for row in results
                                           if isinstance(row, (list, tuple)) and len(row) >= 3)
        if outcome.get('error') or outcome.get('requires_manual_recovery'):
            successful = False
        current_hash = _file_hash(root / 'INCAR') if (root / 'INCAR').is_file() else None
        unchanged = (bool(prepared.get('incar_sha256_before'))
                     and current_hash == prepared.get('incar_sha256_before'))
        status = 'applied' if successful else 'failed_or_unknown'
        record = self._write_immutable_record(
            root, f'outcome-{prepared["correction_id"]}.json', {
                'record_id': prepared['correction_id'],
                'record_type': 'repair_outcome', 'created_at': _utc_now(),
                'status': status, 'job_id': prepared['job_id'],
                'plan_id': prepared['plan_id'], 'failure_class': None,
                'action': 'continue_frozen_incar',
                'idempotency_key': prepared['operation_key'],
                'intent_record_hash': prepared['intent_record_hash'],
                'method_diff': [],
                'incar_sha256_before': prepared.get('incar_sha256_before'),
                'incar_sha256_after': current_hash,
                'method_compatibility': 'unchanged' if unchanged else 'unknown',
                'requires_manual_recovery': bool(outcome.get('requires_manual_recovery')),
                'result_codes': [str(row[2])[:300] for row in results
                                 if isinstance(row, (list, tuple)) and len(row) >= 3],
            })
        return {
            'record_id': record['record_id'], 'record_hash': record['record_hash'],
            'status': status, 'method_compatibility': record['method_compatibility'],
        }
