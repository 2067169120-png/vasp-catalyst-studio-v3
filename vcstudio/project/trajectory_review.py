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
_CORRECTION_ID_RE = re.compile(r'[0-9a-f]{24}')
_SHA256_RE = re.compile(r'[0-9a-f]{64}')
_FRAME_DIR_RE = re.compile(r'^\d+$')
_E0_RE = re.compile(r'\bE0=\s*([-+0-9.Ee]+)')
_TEMP_RE = re.compile(r'\bT=\s*([-+0-9.Ee]+)')
_STEP_RE = re.compile(r'^\s*(\d+)\s+.*?\bF=')
_XDAT_MARKER_RE = re.compile(
    rb'^\s*(Direct|Cartesian)\s+configuration\s*=\s*(\d+)\s*$', re.IGNORECASE)
_MAX_PAGE = 200
_MAX_PLOT_POINTS = 1000
_MAX_SESSIONS = 32
_MAX_FRAME_TOKENS = 8192
_SESSION_TTL_SECONDS = 30 * 60
_PAGE_DISTANCE_MAX_ATOMS = 200
_MAX_FRAME_RENDER_ATOMS = 5000
_MAX_OSZICAR_BYTES = 128 * 1024 * 1024
_MAX_OUTCAR_BYTES = 4 * 1024 * 1024 * 1024
_MAX_XDATCAR_BYTES = 4 * 1024 * 1024 * 1024
_MAX_STRUCTURE_BYTES = 64 * 1024 * 1024
_MAX_SESSION_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_SESSION_STEPS = 200_000
_MAX_SESSION_FRAMES = 200_000
_CORRECTION_DIR = '.vcstudio-corrections'
_CAS_BASE_NAMES = frozenset({'job.yaml', 'INCAR', 'CONTCAR'})


class TrajectoryLimitError(ValueError):
    """Raised before reading or expanding an over-limit trajectory source."""


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
    total_bytes = 0
    for path in paths:
        try:
            before = path.stat()
            total_bytes += int(before.st_size)
            if total_bytes > _MAX_SESSION_SOURCE_BYTES:
                raise TrajectoryLimitError(
                    'trajectory snapshot exceeds the cumulative byte limit')
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


def _snapshot_is_current(rows: list[dict], *, verify_content=()) -> bool:
    verify = {str(item) for item in verify_content}
    for row in rows:
        try:
            stat = row['path'].stat()
        except OSError:
            return False
        if stat.st_size != row['size'] or stat.st_mtime_ns != row['mtime_ns']:
            return False
        if (row.get('name') in verify
                or Path(str(row.get('name') or '')).name in verify):
            try:
                if _file_hash(row['path']) != row['sha256']:
                    return False
            except OSError:
                return False
    return True


def _read_text(path: Path, *, max_bytes=_MAX_STRUCTURE_BYTES) -> tuple[str, bool]:
    try:
        if path.stat().st_size > max_bytes:
            raise TrajectoryLimitError(f'{path.name} exceeds the byte limit')
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
    text, complete_line = _read_text(path, max_bytes=_MAX_OSZICAR_BYTES)
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
        # An ionic step without VASP's explicit step marker cannot be aligned
        # authoritatively to XDATCAR and is therefore not a complete row here.
        if step_match is None:
            continue
        step = int(step_match.group(1))
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
        if len(rows) > _MAX_SESSION_STEPS:
            raise TrajectoryLimitError('OSZICAR exceeds the ionic-step limit')
    return rows, complete_line


def _parse_outcar_fmax(path: Path, convergence_mod) -> tuple[list[float], bool]:
    try:
        size = path.stat().st_size
        if size > _MAX_OUTCAR_BYTES:
            raise TrajectoryLimitError('OUTCAR exceeds the byte limit')
        with path.open('rb') as raw:
            if size:
                raw.seek(-1, os.SEEK_END)
                complete_line = raw.read(1) in (b'\n', b'\r')
            else:
                complete_line = True
        trailing_force_open = False
        trailing_force_rows = False
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            # The numerical parser remains the single source of force semantics.
            values = list(convergence_mod.parse_outcar_fmax_lines(handle))
        # The shared parser intentionally accepts a force block at EOF.  A live
        # snapshot must be stricter: detect the parser's terminal block without
        # recomputing any force value, and withhold it until a delimiter arrives.
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            separator_pending = False
            for raw_line in handle:
                line = raw_line.rstrip('\r\n')
                if 'TOTAL-FORCE' in line:
                    trailing_force_open = True
                    trailing_force_rows = False
                    separator_pending = True
                    continue
                if not trailing_force_open:
                    continue
                if separator_pending:
                    separator_pending = False
                    stripped = line.strip()
                    if stripped and set(stripped) <= {'-'}:
                        continue
                fields = line.split()
                if len(fields) != 6:
                    if trailing_force_rows:
                        trailing_force_open = False
                    continue
                try:
                    float(fields[3])
                    float(fields[4])
                    float(fields[5])
                except ValueError:
                    continue
                trailing_force_rows = True
        if trailing_force_open and trailing_force_rows and values:
            values.pop()
        return values, complete_line and not trailing_force_open
    except OSError:
        return [], False


def _xdatcar_index(path: Path) -> tuple[dict | None, list[dict], bool, list[str]]:
    """Index complete XDATCAR frames by byte offset; coordinates stay on disk."""
    warnings = []
    try:
        if path.stat().st_size > _MAX_XDATCAR_BYTES:
            raise TrajectoryLimitError('XDATCAR exceeds the byte limit')
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
                if len(frames) > _MAX_SESSION_FRAMES:
                    raise TrajectoryLimitError('XDATCAR exceeds the frame limit')
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
    text, complete = _read_text(path, max_bytes=_MAX_STRUCTURE_BYTES)
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
    text, complete = _read_text(source['path'], max_bytes=_MAX_STRUCTURE_BYTES)
    if not text or not complete:
        raise ValueError('结构帧仍在写入或不可读，请刷新播放器快照')
    return text


def _neb_frame_dirs(root: Path) -> list[Path]:
    frames = []
    for path in root.iterdir():
        if path.is_dir() and _FRAME_DIR_RE.fullmatch(path.name):
            frames.append(path)
            if len(frames) > _MAX_SESSION_FRAMES:
                raise TrajectoryLimitError('NEB exceeds the frame limit')
    return sorted(frames, key=lambda path: int(path.name))


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


def _validated_correction_record(path: Path) -> dict:
    match = re.fullmatch(r'(intent|outcome)-([0-9a-f]{24})\.json', path.name)
    if match is None:
        raise ValueError('filename is not bound to a correction record id')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('record must be a JSON object')
    supplied = str(payload.get('record_hash') or '')
    body = dict(payload)
    body.pop('record_hash', None)
    expected_type = 'repair_intent' if match.group(1) == 'intent' else 'repair_outcome'
    if payload.get('schema') != CORRECTION_SCHEMA or supplied != _json_hash(body):
        raise ValueError('record hash mismatch')
    if payload.get('record_id') != match.group(2):
        raise ValueError('record_id does not match filename')
    if payload.get('record_type') != expected_type:
        raise ValueError('record_type does not match filename')
    required_strings = (
        'created_at', 'job_id', 'ledger_job_id', 'manifest_job_id', 'plan_id',
        'action', 'idempotency_key', 'cas_anchor_sha256', 'source_hash',
        'cas_manifest_sha256', 'cas_diagnosis_sha256', 'method_compatibility',
    )
    for key in required_strings:
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f'{key} is missing')
    if payload['action'] != 'continue_frozen_incar':
        raise ValueError('action is not the bounded repair seam')
    if not _OPERATION_KEY_RE.fullmatch(payload['idempotency_key']):
        raise ValueError('idempotency_key is invalid')
    for key in (
            'plan_id', 'cas_anchor_sha256', 'source_hash', 'record_hash',
            'cas_manifest_sha256', 'cas_diagnosis_sha256'):
        if not _SHA256_RE.fullmatch(str(payload.get(key) or '')):
            raise ValueError(f'{key} is invalid')
    cas_files = payload.get('cas_files_sha256')
    if (not isinstance(cas_files, dict)
            or not {'job.yaml', 'INCAR', 'CONTCAR'}.issubset(cas_files)
            or any(not isinstance(name, str)
                   or not _SHA256_RE.fullmatch(str(digest or ''))
                   for name, digest in cas_files.items())):
        raise ValueError('cas_files_sha256 is invalid')
    if cas_files['job.yaml'] != payload['cas_manifest_sha256']:
        raise ValueError('manifest hash is not bound to the CAS file map')
    derived_id = hashlib.sha256(
        f'{payload["plan_id"]}|{payload["idempotency_key"]}'.encode('utf-8'),
    ).hexdigest()[:24]
    if payload['record_id'] != derived_id:
        raise ValueError('record_id is not bound to plan and idempotency key')
    if payload['job_id'] != payload['ledger_job_id']:
        raise ValueError('job_id is not bound to the ledger identity')
    if expected_type == 'repair_intent':
        if payload.get('status') != 'prepared' or payload['method_compatibility'] != 'pending':
            raise ValueError('intent status is invalid')
    else:
        if not _SHA256_RE.fullmatch(str(payload.get('intent_record_hash') or '')):
            raise ValueError('intent_record_hash is invalid')
        if payload.get('status') not in ('applied', 'failed_or_unknown'):
            raise ValueError('outcome status is invalid')
        if payload['method_compatibility'] not in ('unchanged', 'unknown'):
            raise ValueError('outcome method compatibility is invalid')
        if not isinstance(payload.get('result_ok'), bool):
            raise ValueError('outcome result_ok is invalid')
    return payload


def correction_method_compatibility(job_dir) -> dict:
    """Validate immutable, strictly paired correction records for consumers."""
    root = Path(job_dir).resolve()
    directory = root / _CORRECTION_DIR
    if not directory.is_dir():
        return {'status': 'no_corrections', 'record_count': 0,
                'records': [], 'issues': []}
    records, issues = [], []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            records.append(_validated_correction_record(path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f'correction record {path.name} is invalid: {exc}')
    intents = {row['record_id']: row for row in records
               if row['record_type'] == 'repair_intent'}
    outcomes = {row['record_id']: row for row in records
                if row['record_type'] == 'repair_outcome'}
    for record_id, outcome in outcomes.items():
        intent = intents.get(record_id)
        if intent is None:
            issues.append(f'correction outcome {record_id} has no matching intent')
            continue
        for key in (
                'created_at', 'job_id', 'ledger_job_id', 'manifest_job_id',
                'plan_id', 'failure_class', 'action', 'idempotency_key',
                'cas_anchor_sha256', 'cas_manifest_sha256',
                'cas_diagnosis_sha256', 'cas_files_sha256', 'source_hash',
                'incar_sha256_before'):
            if outcome.get(key) != intent.get(key):
                issues.append(f'correction pair {record_id} disagrees on {key}')
        if outcome.get('intent_record_hash') != intent.get('record_hash'):
            issues.append(f'correction pair {record_id} has the wrong intent hash')
    if issues:
        status = 'unknown_invalid_record'
    elif set(intents) - set(outcomes):
        status = 'unknown_pending'
    else:
        applied = [row for row in outcomes.values() if row.get('status') == 'applied']
        status = ('verified_unchanged' if applied and all(
            row.get('method_compatibility') == 'unchanged' for row in applied)
            else 'no_applied_corrections')
    projection = [{
        key: row.get(key) for key in (
            'record_id', 'record_type', 'created_at', 'status', 'failure_class',
            'action', 'method_compatibility', 'record_hash', 'intent_record_hash',
            'cas_anchor_sha256',
        ) if row.get(key) is not None
    } for row in records[-20:]]
    return {'status': status, 'record_count': len(records),
            'records': projection, 'issues': issues}


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
    def _source_paths(root: Path, kind: str, manifest: dict | None = None) -> list[Path]:
        names = ('job.yaml', 'INCAR', 'OSZICAR', 'OUTCAR', 'XDATCAR',
                 'CONTCAR', 'POSCAR')
        paths = [root / name for name in names if (root / name).is_file()]
        diagnosis = (((manifest or {}).get('results') or {}).get('diagnosis') or {})
        evidence_name = diagnosis.get('output_file') if isinstance(diagnosis, dict) else None
        if isinstance(evidence_name, str) and evidence_name.strip():
            candidate = Path(evidence_name.strip())
            if (not candidate.is_absolute() and '..' not in candidate.parts):
                resolved = (root / candidate).resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    resolved = None
                if resolved is not None and resolved.is_file() and resolved not in paths:
                    paths.append(resolved)
        if kind == 'neb':
            for frame in _neb_frame_dirs(root):
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
        frame_dirs = _neb_frame_dirs(root)
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
        if rows and len(forces) != len(rows):
            partial = True
            warnings.append(
                f'完整 OSZICAR 步 {len(rows)} 与完整 OUTCAR 力块 {len(forces)} '
                '分母不同；仅投影明确重叠的力值。')
        frames: list[dict] = []
        xdat_meta = None
        if (root / 'XDATCAR').is_file():
            xdat_meta, frames, xdat_complete, xdat_warnings = _xdatcar_index(root / 'XDATCAR')
            partial = partial or not xdat_complete
            warnings.extend(xdat_warnings)
        if frames:
            row_steps = [row.get('step') for row in rows]
            frame_steps = [frame.get('configuration') for frame in frames]
            row_unique = len(row_steps) == len(set(row_steps))
            frame_unique = len(frame_steps) == len(set(frame_steps))
            frame_by_step = ({frame['configuration']: index
                              for index, frame in enumerate(frames)}
                             if frame_unique else {})
            if (not row_unique or not frame_unique
                    or set(row_steps) != set(frame_steps)):
                partial = True
                warnings.append(
                    'XDATCAR configuration 标记与 OSZICAR 离子步不能完整一一对齐；'
                    '未获证明的步骤不签发结构帧 token。')
            for row in rows:
                step = row.get('step')
                source = frame_by_step.get(step) if row_unique else None
                row['frame_source'] = source
                row['frame_alignment'] = ('configuration_marker'
                                          if source is not None else 'unaligned')
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
                                     'fmax_ev_a': None, 'frame_source': 0,
                                     'frame_alignment': 'final_structure_only'})
                    else:
                        for row in rows:
                            row['frame_source'] = None
                            row['frame_alignment'] = 'unaligned'
                        rows[-1]['frame_source'] = 0
                        rows[-1]['frame_alignment'] = 'final_structure_only'
                    warnings.append('未找到完整 XDATCAR，仅末步结构可导航。')
                except ValueError:
                    partial = True
            elif rows:
                for row in rows:
                    row['frame_source'] = None
                    row['frame_alignment'] = 'unaligned'
                partial = True
                warnings.append('未找到可用结构帧；曲线与步骤表仍可只读查看。')
        analysis = {'source': 'existing-parser', 'kind': kind}
        if kind == 'aimd':
            # The authoritative AIMD summary is intentionally deferred until
            # open() has proved that the *whole* source snapshot is stable.
            analysis = {
                'source': 'vcstudio.project.task_analysis.analyze_aimd',
                'ok': False, 'status': 'pending_snapshot_validation',
                'summary': None,
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
        paths = self._source_paths(root, task_kind, value)
        sources, hash_stable = _source_snapshot(root, paths)
        if not sources:
            raise ValueError('job has no trajectory or structure evidence')
        if task_kind == 'neb':
            rows, frames, partial, warnings, analysis = self._build_neb(root)
        else:
            rows, frames, partial, warnings, analysis = self._build_series(root, task_kind)
        if len(rows) > _MAX_SESSION_STEPS or len(frames) > _MAX_SESSION_FRAMES:
            raise TrajectoryLimitError('trajectory exceeds the session row/frame limit')
        current_value = self._manifest.load_manifest(root) or {}
        current_paths = {
            path.resolve() for path in self._source_paths(root, task_kind, current_value)}
        snapshot_paths = {row['path'].resolve() for row in sources}
        source_set_stable = current_paths == snapshot_paths
        partial = (partial or not hash_stable or not source_set_stable
                   or not _snapshot_is_current(sources))
        if not hash_stable:
            warnings.append('源文件在快照期间发生变化；当前结果标为部分写入。')
        if not source_set_stable:
            warnings.append('解析期间源文件集合发生变化；当前结果标为部分写入。')
        if task_kind == 'aimd':
            natoms = analysis.get('trajectory_natoms')
            if partial:
                analysis.update({
                    'ok': False, 'status': 'withheld_partial_snapshot',
                    'summary': None,
                })
            else:
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
                    'trajectory_natoms': natoms,
                }
        source_hash = sources[0]['source_hash']
        diagnosis = (((value.get('results') or {}).get('diagnosis') or {})
                     if isinstance(value, dict) else {})
        evidence_name = (diagnosis.get('output_file')
                         if isinstance(diagnosis, dict) else None)
        evidence_name = (Path(evidence_name).as_posix()
                         if isinstance(evidence_name, str) and evidence_name else None)
        token = f'trajectory-{self._token_factory()}'
        session = {
            'token': token, 'job_id': identifier, 'root': root,
            'kind': task_kind, 'manifest': value, 'rows': rows, 'frames': frames,
            'sources': sources, 'source_hash': source_hash,
            'partial_write': bool(partial), 'warnings': list(dict.fromkeys(warnings)),
            'analysis': analysis, 'created_at': _utc_now(),
            'last_access': self._clock(), 'metric_cache': {},
            'cas_names': {
                row['name'] for row in sources
                if row['name'] in _CAS_BASE_NAMES or row['name'] == evidence_name
            },
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
        manifest = self._manifest.load_manifest(session['root']) or {}
        current_paths = {
            path.resolve() for path in self._source_paths(
                session['root'], session['kind'], manifest)}
        snapshot_paths = {row['path'].resolve() for row in session['sources']}
        return (current_paths != snapshot_paths
                or not _snapshot_is_current(
                    session['sources'], verify_content=session.get('cas_names') or ()))

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
        alignments = {row.get('frame_alignment') for row in session['rows']}
        if 'unaligned' in alignments:
            frame_alignment = 'unaligned'
        elif alignments == {'configuration_marker'}:
            frame_alignment = 'configuration_marker'
        elif alignments:
            frame_alignment = 'partial_or_final_only'
        else:
            frame_alignment = 'unavailable'
        return {
            'schema': TRAJECTORY_SCHEMA, 'ok': True, 'stale': False,
            'partial_write': bool(session['partial_write']),
            'session_token': session['token'], 'job_id': session['job_id'],
            'task_kind': session['kind'], 'source_hash': session['source_hash'],
            'source_files': [
                {'name': row['name'], 'size': row['size'], 'sha256': row['sha256']}
                for row in session['sources']],
            'n_steps': len(session['rows']), 'n_frames': len(session['frames']),
            'frame_alignment': frame_alignment,
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
        sampled_count = ((len(session['rows']) + sample_stride - 1) // sample_stride
                         if session['rows'] else 0)
        page_count = min(page_limit, max(0, sampled_count - start))
        page_indexes = [(start + index) * sample_stride
                        for index in range(page_count)]
        public_rows = []
        for index in page_indexes:
            source = session['rows'][index]
            frame_source = source.get('frame_source')
            row = {key: source.get(key) for key in (
                'step', 'image', 'energy_ev', 'relative_energy_ev',
                'fmax_ev_a', 'temperature_k', 'frame_alignment')}
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
            'total_steps': len(session['rows']), 'sampled_steps': sampled_count,
            'offset': start, 'limit': page_limit, 'stride': sample_stride,
            'next_offset': next_offset if next_offset < sampled_count else None,
            'rows': public_rows,
        }

    def _bounded_structure_view(self, content: str, natoms_hint: int) -> dict:
        """Build XYZ without invoking distance analysis above its safe threshold."""
        if natoms_hint > _MAX_FRAME_RENDER_ATOMS:
            raise TrajectoryLimitError('selected frame exceeds the atom render limit')
        if natoms_hint <= _PAGE_DISTANCE_MAX_ATOMS:
            return self._structure_view.structure_view(content)
        parsed = self._structure_view.parse_positions(content)
        elements = list(parsed.get('elements') or [])
        coordinates = list(parsed.get('coords') or [])
        if len(elements) != len(coordinates) or len(elements) > _MAX_FRAME_RENDER_ATOMS:
            raise TrajectoryLimitError('selected frame exceeds the atom render limit')
        counts: OrderedDict[str, int] = OrderedDict()
        for element in elements:
            counts[str(element)] = counts.get(str(element), 0) + 1
        xyz = [str(len(elements)), 'vcstudio structure preview']
        for element, coordinate in zip(elements, coordinates):
            x, y, z = coordinate
            xyz.append(f'{element} {float(x):.6f} {float(y):.6f} {float(z):.6f}')
        return {
            'xyz': '\n'.join(xyz), 'natoms': len(elements),
            'formula': ' '.join(f'{element}{count}'
                                for element, count in counts.items()),
            'gap': {
                'min_dist': None, 'level': 'unknown',
                'notes': ['Distance analysis was gated before invocation for this structure size.'],
            },
            'notes': ['Large structure rendered without pair-distance analysis.'],
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
        source = session['frames'][frame_source]
        natoms = int(source.get('natoms') or 0)
        if natoms > _MAX_FRAME_RENDER_ATOMS:
            raise TrajectoryLimitError('selected frame exceeds the atom render limit')
        content = _load_structure_text(source)
        view = self._bounded_structure_view(content, natoms)
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

    @staticmethod
    def _repair_cas_contract(session: dict, manifest: dict) -> dict:
        by_name = {row['name']: row for row in session['sources']}
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        evidence_name = diagnosis.get('output_file') if isinstance(diagnosis, dict) else None
        evidence_name = (Path(evidence_name).as_posix()
                         if isinstance(evidence_name, str) and evidence_name else None)
        names = {'job.yaml', 'INCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR'}
        if evidence_name:
            names.add(evidence_name)
        files = {name: by_name[name]['sha256'] for name in sorted(names)
                 if name in by_name}
        return {
            'schema': 'vcstudio.repair-cas/v1',
            'ledger_job_id': session['job_id'],
            'manifest_job_id': str(manifest.get('job_id') or session['job_id']),
            'manifest_state': str(manifest.get('state') or ''),
            'scheduler_job_id': str(manifest.get('scheduler_job_id') or ''),
            'diagnosis_sha256': _json_hash(diagnosis),
            'diagnosis_evidence_file': evidence_name,
            'source_hash': session['source_hash'],
            'files': files,
        }

    @staticmethod
    def _current_cas_contract(root: Path, session: dict, manifest: dict,
                              expected: dict) -> dict:
        files = {}
        for name in expected.get('files') or {}:
            candidate = Path(str(name))
            if candidate.is_absolute() or '..' in candidate.parts:
                raise RuntimeError('repair CAS contains an unsafe evidence name')
            path = (root / candidate).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise RuntimeError('repair CAS evidence escaped the job root') from exc
            if not path.is_file():
                raise RuntimeError('repair CAS evidence is missing')
            files[candidate.as_posix()] = _file_hash(path)
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        return {
            'schema': 'vcstudio.repair-cas/v1',
            'ledger_job_id': session['job_id'],
            'manifest_job_id': str(manifest.get('job_id') or session['job_id']),
            'manifest_state': str(manifest.get('state') or ''),
            'scheduler_job_id': str(manifest.get('scheduler_job_id') or ''),
            'diagnosis_sha256': _json_hash(diagnosis),
            'diagnosis_evidence_file': expected.get('diagnosis_evidence_file'),
            'source_hash': session['source_hash'],
            'files': files,
        }

    def _assert_cas_contract(self, session: dict, expected: dict,
                             manifest: dict | None = None) -> dict:
        if self._is_stale(session):
            raise RuntimeError('trajectory source changed; repair CAS is stale')
        current_manifest = manifest or self._manifest.load_manifest(session['root']) or {}
        current = self._current_cas_contract(
            session['root'], session, current_manifest, expected)
        if current != expected:
            raise RuntimeError('repair evidence content changed; refresh before confirming')
        return current_manifest

    def _repair_execution_evidence(self, session: dict, manifest: dict) -> list[str]:
        blockers = []
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        for name in ('job.yaml', 'INCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR'):
            if not (session['root'] / name).is_file():
                blockers.append(f'{name.lower()}_missing')
        if session.get('partial_write'):
            blockers.append('trajectory_snapshot_partial')
        if manifest.get('state') not in ('FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'):
            blockers.append('scheduler_state_not_terminal')
        if not str(manifest.get('scheduler_job_id') or ''):
            blockers.append('scheduler_job_id_missing')
        if not isinstance(diagnosis, dict) or not diagnosis.get('classified_at'):
            blockers.append('diagnosis_not_freshly_classified')
        terminal_signal = (isinstance(diagnosis.get('clean_exit'), bool)
                           or isinstance(diagnosis.get('exit_code'), int)
                           or bool(diagnosis.get('scheduler_reason')))
        if not terminal_signal:
            blockers.append('scheduler_terminal_evidence_missing')
        try:
            rows, osz_complete = _parse_oszicar(session['root'] / 'OSZICAR')
            forces, out_complete = _parse_outcar_fmax(
                session['root'] / 'OUTCAR', self._convergence)
            if not osz_complete or not rows:
                blockers.append('oszicar_incomplete')
            if not out_complete or len(forces) != len(rows):
                blockers.append('outcar_incomplete')
        except (OSError, ValueError):
            blockers.append('output_parse_unavailable')
        try:
            from vcstudio.cluster import diagnose

            contcar, contcar_complete = _read_text(
                session['root'] / 'CONTCAR', max_bytes=_MAX_STRUCTURE_BYTES)
            if not contcar_complete or not diagnose.valid_poscar(contcar):
                blockers.append('contcar_incomplete')
        except (OSError, ValueError):
            blockers.append('contcar_incomplete')
        return list(dict.fromkeys(blockers))

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
        blockers = self._repair_execution_evidence(session, manifest)
        cas_contract = self._repair_cas_contract(session, manifest)
        cas_anchor = _json_hash(cas_contract)
        execution_allowed = (restartable and session['kind'] != 'neb'
                             and rounds < max_rounds
                             and failure_class != 'UNKNOWN'
                             and not blockers)
        action = 'continue_frozen_incar' if execution_allowed else 'pause'
        created_at = _utc_now()
        plan_body = {
            'job_id': session['job_id'], 'source_hash': session['source_hash'],
            'failure_class': failure_class, 'diagnosis_hash': _json_hash(diagnosis),
            'action': action, 'continue_rounds': rounds,
            'cas_anchor_sha256': cas_anchor,
            'incar_sha256': _file_hash(session['root'] / 'INCAR')
            if (session['root'] / 'INCAR').is_file() else None,
        }
        plan_id = _json_hash(plan_body)
        plan_token = f'repair-{self._token_factory()}'
        plan = {
            **plan_body, 'plan_id': plan_id, 'plan_token': plan_token,
            'session_token': session['token'], 'job_dir': session['root'],
            'created_at': created_at, 'cas_contract': cas_contract,
            'detected_evidence': evidence, 'suggested_change': guidance['change'],
            'estimated_cost': {
                'class': guidance['cost'], 'additional_runs': 1 if execution_allowed else None,
                'remaining_bounded_rounds': max(0, max_rounds - rounds),
                'exact_machine_time': None,
            },
            'scientific_impact': guidance['impact'], 'method_diff': method_diff,
            'execution_allowed': execution_allowed,
            'execution_blockers': blockers,
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
            'execution_blockers': blockers,
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
            if existing != body:
                raise RuntimeError('existing immutable correction record differs from proposed content')
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
        manifest = self._assert_cas_contract(session, plan['cas_contract'])
        blockers = self._repair_execution_evidence(session, manifest)
        if blockers:
            raise RuntimeError('repair terminal/output evidence is no longer complete')
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        if _json_hash(diagnosis) != plan['diagnosis_hash']:
            raise RuntimeError('diagnosis changed; refresh repair preview before confirming')
        correction_id = hashlib.sha256(
            f'{plan["plan_id"]}|{key}'.encode('utf-8')).hexdigest()[:24]
        record = self._write_immutable_record(
            session['root'], f'intent-{correction_id}.json', {
                'record_id': correction_id, 'record_type': 'repair_intent',
                'created_at': plan['created_at'], 'status': 'prepared',
                'job_id': session['job_id'], 'source_hash': session['source_hash'],
                'ledger_job_id': plan['cas_contract']['ledger_job_id'],
                'manifest_job_id': plan['cas_contract']['manifest_job_id'],
                'cas_anchor_sha256': plan['cas_anchor_sha256'],
                'cas_manifest_sha256': plan['cas_contract']['files']['job.yaml'],
                'cas_diagnosis_sha256': plan['cas_contract']['diagnosis_sha256'],
                'cas_files_sha256': plan['cas_contract']['files'],
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
            'source_hash': session['source_hash'],
            'ledger_job_id': plan['cas_contract']['ledger_job_id'],
            'manifest_job_id': plan['cas_contract']['manifest_job_id'],
            'cas_anchor_sha256': plan['cas_anchor_sha256'],
            'cas_manifest_sha256': plan['cas_contract']['files']['job.yaml'],
            'cas_diagnosis_sha256': plan['cas_contract']['diagnosis_sha256'],
            'cas_files_sha256': plan['cas_contract']['files'],
            'cas_contract': plan['cas_contract'],
            'record_created_at': plan['created_at'],
            'failure_class': plan['failure_class'],
            'session_token': session['token'],
        }

    def assert_repair_cas(self, prepared: dict, *, ledger_job_id: str,
                          manifest: dict | None = None) -> dict:
        if str(ledger_job_id or '') != prepared.get('ledger_job_id'):
            raise RuntimeError('job ledger binding changed; repair remains paused')
        session = self._session(prepared.get('session_token'))
        if (session['job_id'] != prepared.get('job_id')
                or session['root'] != Path(prepared['job_dir']).resolve()):
            raise RuntimeError('repair session binding changed')
        current = self._assert_cas_contract(
            session, prepared['cas_contract'], manifest=manifest)
        blockers = self._repair_execution_evidence(session, current)
        if blockers:
            raise RuntimeError('repair terminal/output evidence is no longer complete')
        return prepared['cas_contract']

    def record_repair_outcome(self, prepared: dict, outcome: dict) -> dict:
        root = Path(prepared['job_dir']).resolve()
        correction_id = str(prepared.get('correction_id') or '')
        if not _CORRECTION_ID_RE.fullmatch(correction_id):
            raise RuntimeError('prepared correction id is invalid')
        intent = _validated_correction_record(
            root / _CORRECTION_DIR / f'intent-{correction_id}.json')
        bindings = {
            'record_hash': 'intent_record_hash',
            'job_id': 'job_id', 'ledger_job_id': 'ledger_job_id',
            'manifest_job_id': 'manifest_job_id', 'plan_id': 'plan_id',
            'idempotency_key': 'operation_key',
            'cas_anchor_sha256': 'cas_anchor_sha256',
            'source_hash': 'source_hash',
        }
        if any(intent.get(intent_key) != prepared.get(prepared_key)
               for intent_key, prepared_key in bindings.items()):
            raise RuntimeError('prepared repair is not bound to its immutable intent')
        operation = outcome if isinstance(outcome, dict) else {}
        raw_results = operation.get('results')
        results = list(raw_results) if isinstance(raw_results, (list, tuple)) else []
        valid_row = (len(results) == 1
                     and isinstance(results[0], (list, tuple))
                     and len(results[0]) >= 3)
        row_matches = False
        if valid_row:
            try:
                row_matches = (Path(str(results[0][0])).resolve() == root)
            except (OSError, ValueError):
                row_matches = False
        successful = bool(
            valid_row and row_matches and results[0][1] is True
            and isinstance(results[0][2], str)
            and operation.get('ok') is not False
            and not operation.get('error')
            and not operation.get('requires_manual_recovery')
            and not operation.get('needs_trust')
            and not operation.get('busy'))
        current_hash = _file_hash(root / 'INCAR') if (root / 'INCAR').is_file() else None
        unchanged = (bool(prepared.get('incar_sha256_before'))
                     and current_hash == prepared.get('incar_sha256_before'))
        status = 'applied' if successful else 'failed_or_unknown'
        record = self._write_immutable_record(
            root, f'outcome-{prepared["correction_id"]}.json', {
                'record_id': prepared['correction_id'],
                'record_type': 'repair_outcome',
                'created_at': prepared['record_created_at'],
                'status': status, 'job_id': prepared['job_id'],
                'ledger_job_id': prepared['ledger_job_id'],
                'manifest_job_id': prepared['manifest_job_id'],
                'source_hash': prepared['source_hash'],
                'cas_anchor_sha256': prepared['cas_anchor_sha256'],
                'cas_manifest_sha256': prepared['cas_manifest_sha256'],
                'cas_diagnosis_sha256': prepared['cas_diagnosis_sha256'],
                'cas_files_sha256': prepared['cas_files_sha256'],
                'plan_id': prepared['plan_id'],
                'failure_class': prepared.get('failure_class'),
                'action': 'continue_frozen_incar',
                'idempotency_key': prepared['operation_key'],
                'intent_record_hash': prepared['intent_record_hash'],
                'method_diff': [],
                'incar_sha256_before': prepared.get('incar_sha256_before'),
                'incar_sha256_after': current_hash,
                'method_compatibility': 'unchanged' if unchanged else 'unknown',
                'requires_manual_recovery': bool(
                    operation.get('requires_manual_recovery')),
                'result_ok': successful,
                'result_row_count': len(results),
                'manifest_sha256_after': (_file_hash(root / 'job.yaml')
                                          if (root / 'job.yaml').is_file() else None),
            })
        return {
            'record_id': record['record_id'], 'record_hash': record['record_hash'],
            'status': status, 'method_compatibility': record['method_compatibility'],
        }
