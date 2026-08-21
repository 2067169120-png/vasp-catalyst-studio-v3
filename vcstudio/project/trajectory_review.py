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
import stat
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import yaml


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
_NEB_SIGMA0_RE = re.compile(r'energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)')
_MAX_PAGE = 200
_MAX_PLOT_POINTS = 1000
_MAX_SESSIONS = 32
_MAX_FRAME_TOKENS = 8192
_SESSION_TTL_SECONDS = 30 * 60
_PAGE_DISTANCE_MAX_ATOMS = 200
_MAX_FRAME_RENDER_ATOMS = 5000
_MAX_OSZICAR_BYTES = 128 * 1024 * 1024
_MAX_OUTCAR_BYTES = 512 * 1024 * 1024
_MAX_XDATCAR_BYTES = 4 * 1024 * 1024 * 1024
_MAX_XDATCAR_ATOMS = _MAX_FRAME_RENDER_ATOMS
_MAX_XDATCAR_HEADER_LINE_BYTES = 64 * 1024
_MAX_XDATCAR_COORDINATE_LINE_BYTES = 4096
_MAX_XDATCAR_FRAME_BYTES = 32 * 1024 * 1024
_MAX_STRUCTURE_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_INCAR_BYTES = 8 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
_MAX_TEXT_LINE_BYTES = 256 * 1024
_MAX_OUTCAR_LINE_BYTES = 256 * 1024
_MAX_OUTCAR_FORCE_ROWS_PER_BLOCK = 100_000
_MAX_NEB_OUTCAR_MATERIALIZE_BYTES = 64 * 1024 * 1024
_MAX_SESSION_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_SESSION_STEPS = 200_000
_MAX_SESSION_FRAMES = 200_000
_MAX_CORRECTION_RECORDS = 512
_MAX_CORRECTION_RECORD_BYTES = 512 * 1024
_MAX_CORRECTION_TOTAL_BYTES = 32 * 1024 * 1024
_CORRECTION_DIR = '.vcstudio-corrections'
_CAS_BASE_NAMES = frozenset({'job.yaml', 'INCAR', 'CONTCAR'})
_REPARSE_ATTRIBUTE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
_CORRECTION_LOCK = threading.RLock()


class TrajectoryLimitError(ValueError):
    """Raised before reading or expanding an over-limit trajectory source."""


class StaleFrameSourceError(RuntimeError):
    """Raised when an opaque frame token no longer names immutable bytes."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')


def _json_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, 'st_file_attributes', 0) & _REPARSE_ATTRIBUTE)


def _entity_identity(value: os.stat_result) -> tuple[int, int, int]:
    inode = int(getattr(value, 'st_ino', 0) or 0)
    fallback = 0 if inode else int(getattr(value, 'st_ctime_ns', 0) or 0)
    return int(getattr(value, 'st_dev', 0) or 0), inode, fallback


def _same_entity(left: os.stat_result, right: os.stat_result) -> bool:
    return _entity_identity(left) == _entity_identity(right)


def _plain_lstat(path: Path, *, directory: bool, label: str) -> os.stat_result:
    value = path.stat(follow_symlinks=False)
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if not expected or path.is_symlink() or _is_reparse(value):
        raise ValueError(f'{label} must be an ordinary non-reparse {"directory" if directory else "file"}')
    return value


def _assert_source_ancestry(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError('trajectory source escaped the registered job root') from exc
    cursor = root
    for part in relative.parts[:-1]:
        cursor /= part
        _plain_lstat(cursor, directory=True, label='trajectory source parent')


def _source_byte_limit(path: Path) -> int:
    name = path.name
    if name == 'job.yaml':
        return _MAX_MANIFEST_BYTES
    if name == 'INCAR':
        return _MAX_INCAR_BYTES
    if name == 'OSZICAR':
        return _MAX_OSZICAR_BYTES
    if name == 'OUTCAR':
        return _MAX_OUTCAR_BYTES
    if name == 'XDATCAR':
        return _MAX_XDATCAR_BYTES
    if name in ('POSCAR', 'CONTCAR'):
        return _MAX_STRUCTURE_BYTES
    return _MAX_EVIDENCE_BYTES


def _hash_binary_handle(handle, *, max_bytes: int) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    consumed = 0
    while True:
        block = handle.read(min(1024 * 1024, max_bytes - consumed + 1))
        if not block:
            break
        consumed += len(block)
        if consumed > max_bytes:
            raise TrajectoryLimitError('frame source exceeds its immutable byte budget')
        digest.update(block)
    return digest.hexdigest()


def _open_snapshot_file(path: Path, *, max_bytes: int | None = None) -> dict:
    """Open and pin one ordinary file, rejecting link/reparse and open races."""
    path = Path(path).absolute()
    before = _plain_lstat(path, directory=False, label='trajectory source')
    limit = int(max_bytes if max_bytes is not None else _source_byte_limit(path))
    if before.st_size > limit:
        raise TrajectoryLimitError(f'{path.name} exceeds the byte limit')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    handle = os.fdopen(descriptor, 'rb')
    try:
        opened = os.fstat(handle.fileno())
        after_open = _plain_lstat(path, directory=False, label='trajectory source')
        if (not stat.S_ISREG(opened.st_mode) or _is_reparse(opened)
                or not _same_entity(before, opened)
                or not _same_entity(opened, after_open)):
            raise ValueError('trajectory source entity changed while it was opened')
        if opened.st_size > limit:
            raise TrajectoryLimitError(f'{path.name} exceeds the byte limit')
        digest = _hash_binary_handle(handle, max_bytes=limit)
        opened_after = os.fstat(handle.fileno())
        path_after = _plain_lstat(path, directory=False, label='trajectory source')
        if (not _same_entity(opened, opened_after)
                or not _same_entity(opened_after, path_after)
                or opened.st_size != opened_after.st_size
                or opened.st_mtime_ns != opened_after.st_mtime_ns):
            raise StaleFrameSourceError('trajectory source changed during snapshot')
        handle.seek(0)
        return {
            'path': path, 'handle': handle, 'handle_lock': threading.RLock(),
            'identity': _entity_identity(opened), 'size': int(opened.st_size),
            'mtime_ns': int(opened.st_mtime_ns), 'sha256': digest,
            'byte_limit': limit,
        }
    except Exception:
        handle.close()
        raise


def _close_source_rows(rows) -> None:
    for row in rows:
        try:
            row.get('handle').close()
        except (AttributeError, OSError):
            pass


def _verified_binary_lines(row: dict, *, line_limit: int, label: str):
    """Yield bounded lines from the pinned entity and prove the parsed bytes."""
    if row['handle'].closed:
        current = _open_snapshot_file(
            row['path'], max_bytes=int(row['byte_limit']))
        try:
            if (current['identity'] != row['identity']
                    or current['size'] != row['size']
                    or current['sha256'] != row['sha256']):
                raise StaleFrameSourceError(
                    'trajectory source generation no longer matches the snapshot')
            yield from _verified_binary_lines(
                current, line_limit=line_limit, label=label)
        finally:
            _close_source_rows([current])
        return
    handle = row['handle']
    expected_size = int(row['size'])
    with row['handle_lock']:
        handle.seek(0)
        digest = hashlib.sha256()
        consumed = 0
        while True:
            line = handle.readline(line_limit + 1)
            if len(line) > line_limit:
                raise TrajectoryLimitError(f'{label} exceeds the line-byte limit')
            if not line:
                break
            consumed += len(line)
            if consumed > expected_size or consumed > int(row['byte_limit']):
                raise TrajectoryLimitError(f'{label} grew beyond its immutable byte budget')
            digest.update(line)
            yield line
        current = os.fstat(handle.fileno())
        if (consumed != expected_size or digest.hexdigest() != row['sha256']
                or _entity_identity(current) != row['identity']
                or current.st_size != expected_size
                or current.st_mtime_ns != row['mtime_ns']):
            raise StaleFrameSourceError(
                'trajectory source bytes changed while they were parsed')


def _read_snapshot_bytes(row: dict, *, max_bytes: int,
                         line_limit: int = _MAX_TEXT_LINE_BYTES) -> bytes:
    if int(row['size']) > max_bytes:
        raise TrajectoryLimitError(f'{row["path"].name} exceeds the byte limit')
    return b''.join(_verified_binary_lines(
        row, line_limit=line_limit, label=row['path'].name))


def _source_snapshot(root: Path, paths: list[Path], *,
                     preopened: dict[Path, dict] | None = None) -> tuple[list[dict], bool]:
    """Pin and hash the allow-listed file set without reopening for parsing."""
    rows = []
    preopened = dict(preopened or {})
    total_bytes = 0
    seen = set()
    try:
        for raw_path in paths:
            path = Path(raw_path).absolute()
            if path in seen:
                continue
            seen.add(path)
            _assert_source_ancestry(root, path)
            row = preopened.pop(path, None) or _open_snapshot_file(path)
            total_bytes += int(row['size'])
            if total_bytes > _MAX_SESSION_SOURCE_BYTES:
                raise TrajectoryLimitError(
                    'trajectory snapshot exceeds the cumulative byte limit')
            row['name'] = path.relative_to(root).as_posix()
            rows.append(row)
    except Exception:
        _close_source_rows(rows)
        _close_source_rows(preopened.values())
        raise
    _close_source_rows(preopened.values())
    public = [
        {'name': row['name'], 'size': row['size'], 'sha256': row['sha256']}
        for row in rows
    ]
    source_hash = _json_hash(public)
    for row in rows:
        row['source_hash'] = source_hash
    return rows, True


def _snapshot_is_current(rows: list[dict], *, verify_content=()) -> bool:
    del verify_content  # Every source is content-verified; no metadata-only tier exists.
    for row in rows:
        current = None
        try:
            current = _open_snapshot_file(
                row['path'], max_bytes=int(row['byte_limit']))
            if (current['identity'] != row['identity']
                    or current['size'] != row['size']
                    or current['sha256'] != row['sha256']):
                return False
        except (OSError, ValueError, StaleFrameSourceError):
            return False
        finally:
            _close_source_rows([current] if current else [])
    return True


def _file_hash(path: Path) -> str:
    row = _open_snapshot_file(Path(path))
    try:
        return str(row['sha256'])
    finally:
        _close_source_rows([row])


def _read_text(source, *, max_bytes=_MAX_STRUCTURE_BYTES) -> tuple[str, bool]:
    owned = None
    try:
        row = source if isinstance(source, dict) else _open_snapshot_file(
            Path(source), max_bytes=max_bytes)
        if not isinstance(source, dict):
            owned = row
        data = _read_snapshot_bytes(row, max_bytes=max_bytes)
    except OSError:
        return '', False
    finally:
        if owned is not None:
            _close_source_rows([owned])
    complete_line = not data or data.endswith((b'\n', b'\r'))
    return data.decode('utf-8', errors='replace'), complete_line


def _manifest_from_snapshot(row: dict) -> dict:
    text, complete = _read_text(row, max_bytes=_MAX_MANIFEST_BYTES)
    if not text or not complete:
        raise ValueError('registered job manifest is unreadable')
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError('registered job manifest is unreadable') from exc
    if not isinstance(value, dict):
        raise ValueError('registered job manifest is unreadable')
    return value


def _current_manifest(root: Path) -> dict:
    row = _open_snapshot_file(root / 'job.yaml', max_bytes=_MAX_MANIFEST_BYTES)
    try:
        return _manifest_from_snapshot(row)
    finally:
        _close_source_rows([row])


def _finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_oszicar(source) -> tuple[list[dict], bool]:
    """Project the raw per-step fields without defining a drift/convergence result."""
    text, complete_line = _read_text(source, max_bytes=_MAX_OSZICAR_BYTES)
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


def _parse_outcar_fmax(source, convergence_mod) -> tuple[list[float], bool]:
    owned = None
    try:
        row = source if isinstance(source, dict) else _open_snapshot_file(
            Path(source), max_bytes=_MAX_OUTCAR_BYTES)
        if not isinstance(source, dict):
            owned = row
        if int(row['size']) > _MAX_OUTCAR_BYTES:
            raise TrajectoryLimitError('OUTCAR exceeds the byte limit')
        complete_line = True

        def tracked_lines():
            nonlocal complete_line
            for raw_line in _verified_binary_lines(
                    row, line_limit=_MAX_OUTCAR_LINE_BYTES, label='OUTCAR'):
                complete_line = raw_line.endswith((b'\n', b'\r'))
                yield raw_line.decode('utf-8', errors='replace')

        parsed = convergence_mod.parse_outcar_fmax_lines(
            tracked_lines(), max_blocks=_MAX_SESSION_STEPS,
            max_rows_per_block=_MAX_OUTCAR_FORCE_ROWS_PER_BLOCK,
            max_line_bytes=_MAX_OUTCAR_LINE_BYTES,
            require_terminated=True, with_status=True)
        return (parsed['values'], complete_line
                and bool(parsed['trailing_block_complete']))
    except OSError:
        return [], False
    finally:
        if owned is not None:
            _close_source_rows([owned])


def _read_bounded_binary_line(handle, limit: int, label: str) -> bytes:
    line = handle.readline(limit + 1)
    if len(line) > limit:
        raise TrajectoryLimitError(f'{label} exceeds the line-byte limit')
    return line


def _xdatcar_index(source) -> tuple[dict | None, list[dict], bool, list[str]]:
    """Index complete XDATCAR frames by byte offset; coordinates stay on disk."""
    warnings = []
    owned = None
    try:
        row = source if isinstance(source, dict) else _open_snapshot_file(
            Path(source), max_bytes=_MAX_XDATCAR_BYTES)
        if not isinstance(source, dict):
            owned = row
        path = row['path']
        source_size = int(row['size'])
        if source_size > min(_MAX_XDATCAR_BYTES, _MAX_SESSION_SOURCE_BYTES):
            raise TrajectoryLimitError('XDATCAR exceeds the byte limit')
        with row['handle_lock']:
            handle = row['handle']
            handle.seek(0)
            header_lines = [
                _read_bounded_binary_line(
                    handle, _MAX_XDATCAR_HEADER_LINE_BYTES, 'XDATCAR header')
                for _ in range(7)
            ]
            if any(not line for line in header_lines):
                if _hash_binary_handle(
                        handle, max_bytes=source_size) != row['sha256']:
                    raise StaleFrameSourceError(
                        'XDATCAR bytes changed while the header was parsed')
                return None, [], False, ['XDATCAR 头部不完整。']
            header_text = b''.join(header_lines).decode('utf-8', errors='replace')
            from vcstudio.generate.poscar import parse_poscar_species

            elements, counts = parse_poscar_species(header_text)
            if not elements or not counts:
                if _hash_binary_handle(
                        handle, max_bytes=source_size) != row['sha256']:
                    raise StaleFrameSourceError(
                        'XDATCAR bytes changed while the header was parsed')
                return None, [], False, ['XDATCAR 物种/原子计数不可解析。']
            if (any(isinstance(count, bool) or not isinstance(count, int)
                    or count < 1 for count in counts)):
                if _hash_binary_handle(
                        handle, max_bytes=source_size) != row['sha256']:
                    raise StaleFrameSourceError(
                        'XDATCAR bytes changed while the header was parsed')
                return None, [], False, ['XDATCAR 原子计数无效。']
            natoms = sum(counts)
            # Trust no declared atom count far enough to read a coordinate.  This
            # gate precedes all per-frame allocation and line traversal.
            if natoms < 1 or natoms > _MAX_XDATCAR_ATOMS:
                raise TrajectoryLimitError('XDATCAR exceeds the atom render limit')
            frames = []
            complete = True
            while True:
                offset = handle.tell()
                marker = _read_bounded_binary_line(
                    handle, _MAX_XDATCAR_HEADER_LINE_BYTES,
                    'XDATCAR configuration marker')
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
                frame_digest = hashlib.sha256()
                frame_digest.update(marker)
                frame_bytes = len(marker)
                coordinate_count = 0
                for _ in range(natoms):
                    line = _read_bounded_binary_line(
                        handle, _MAX_XDATCAR_COORDINATE_LINE_BYTES,
                        'XDATCAR coordinate')
                    if not line or not line.endswith((b'\n', b'\r')):
                        complete = False
                        break
                    frame_bytes += len(line)
                    if frame_bytes > _MAX_XDATCAR_FRAME_BYTES:
                        raise TrajectoryLimitError(
                            'XDATCAR frame exceeds the byte limit')
                    fields = line.split()
                    if len(fields) < 3:
                        complete = False
                        break
                    try:
                        values = tuple(float(value) for value in fields[:3])
                    except ValueError:
                        complete = False
                        break
                    if not all(math.isfinite(value) for value in values):
                        complete = False
                        break
                    frame_digest.update(line)
                    coordinate_count += 1
                if coordinate_count != natoms:
                    warnings.append('XDATCAR 末帧仍在写入，已只暴露此前完整帧。')
                    break
                if len(frames) >= _MAX_SESSION_FRAMES:
                    raise TrajectoryLimitError('XDATCAR exceeds the frame limit')
                frames.append({
                    'kind': 'xdatcar', 'path': path, 'offset': offset,
                    'end_offset': handle.tell(), 'frame_bytes': frame_bytes,
                    'frame_sha256': frame_digest.hexdigest(),
                    'mode': match.group(1).decode('ascii').title(),
                    'configuration': int(match.group(2)), 'natoms': natoms,
                    'header': header_text,
                })
            result = ({
                'elements': elements, 'counts': counts, 'natoms': natoms,
            }, frames, complete, warnings)
            if (_hash_binary_handle(handle, max_bytes=source_size) != row['sha256']
                    or os.fstat(handle.fileno()).st_size != source_size):
                raise StaleFrameSourceError(
                    'XDATCAR bytes changed while the frame index was built')
            return result
    except OSError:
        return None, [], False, ['XDATCAR 无法读取。']
    finally:
        if owned is not None:
            _close_source_rows([owned])


def _load_xdatcar_frame(source: dict) -> str:
    natoms = int(source['natoms'])
    if natoms < 1 or natoms > _MAX_XDATCAR_ATOMS:
        raise TrajectoryLimitError('selected frame exceeds the atom render limit')
    expected_source = str(source.get('source_sha256') or '')
    expected_frame = str(source.get('frame_sha256') or '')
    expected_size = int(source.get('source_size') or -1)
    row = source.get('snapshot')
    if (not _SHA256_RE.fullmatch(expected_source)
            or not _SHA256_RE.fullmatch(expected_frame)
            or expected_size < 1
            or expected_size > min(_MAX_XDATCAR_BYTES, _MAX_SESSION_SOURCE_BYTES)
            or not isinstance(row, dict)
            or row.get('sha256') != expected_source
            or row.get('size') != expected_size):
        raise StaleFrameSourceError('frame token lacks immutable content binding')
    active_row = row
    owned = None
    if row['handle'].closed:
        owned = _open_snapshot_file(
            row['path'], max_bytes=int(row['byte_limit']))
        if (owned['identity'] != row['identity']
                or owned['size'] != row['size']
                or owned['sha256'] != row['sha256']):
            _close_source_rows([owned])
            raise StaleFrameSourceError(
                'XDATCAR source generation no longer matches the frame token')
        active_row = owned
    try:
        with active_row['handle_lock']:
            handle = active_row['handle']
            opened_before = os.fstat(handle.fileno())
            if (opened_before.st_size != expected_size
                    or _entity_identity(opened_before) != active_row['identity']):
                raise StaleFrameSourceError('XDATCAR source size changed')
            if _hash_binary_handle(handle, max_bytes=expected_size) != expected_source:
                raise StaleFrameSourceError(
                    'XDATCAR content changed; refresh the player snapshot')
            handle.seek(int(source['offset']))
            marker = _read_bounded_binary_line(
                handle, _MAX_XDATCAR_HEADER_LINE_BYTES,
                'XDATCAR configuration marker')
            match = _XDAT_MARKER_RE.match(marker.strip())
            if not match:
                raise StaleFrameSourceError('XDATCAR frame marker changed')
            digest = hashlib.sha256()
            digest.update(marker)
            frame_bytes = len(marker)
            coordinates = []
            for _ in range(natoms):
                line = _read_bounded_binary_line(
                    handle, _MAX_XDATCAR_COORDINATE_LINE_BYTES,
                    'XDATCAR coordinate')
                if not line or not line.endswith((b'\n', b'\r')):
                    raise StaleFrameSourceError('XDATCAR frame is incomplete')
                frame_bytes += len(line)
                if frame_bytes > _MAX_XDATCAR_FRAME_BYTES:
                    raise TrajectoryLimitError('XDATCAR frame exceeds the byte limit')
                fields = line.split()
                try:
                    values = tuple(float(value) for value in fields[:3])
                except ValueError as exc:
                    raise StaleFrameSourceError(
                        'XDATCAR frame coordinates changed') from exc
                if len(fields) < 3 or not all(math.isfinite(value) for value in values):
                    raise StaleFrameSourceError('XDATCAR frame coordinates changed')
                digest.update(line)
                coordinates.append(line)
            if (handle.tell() != int(source['end_offset'])
                    or frame_bytes != int(source['frame_bytes'])
                    or digest.hexdigest() != expected_frame):
                raise StaleFrameSourceError('XDATCAR frame content changed')
            if _hash_binary_handle(handle, max_bytes=expected_size) != expected_source:
                raise StaleFrameSourceError('XDATCAR changed while reading the frame')
            opened_after = os.fstat(handle.fileno())
    finally:
        if owned is not None:
            _close_source_rows([owned])
    if (_entity_identity(opened_before) != _entity_identity(opened_after)
            or opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns):
        raise StaleFrameSourceError('XDATCAR source generation changed while reading')
    mode = match.group(1).decode('ascii').title()
    coordinate_text = b''.join(coordinates).decode('utf-8', errors='replace')
    return source['header'].rstrip('\r\n') + f'\n{mode}\n' + coordinate_text


def _structure_file_source(path: Path, snapshot: dict | None = None) -> dict:
    text, complete = _read_text(
        snapshot if snapshot is not None else path,
        max_bytes=_MAX_STRUCTURE_BYTES)
    if not text:
        raise ValueError('结构文件为空')
    try:
        from vcstudio.generate.poscar import parse_poscar_species

        _elements, counts = parse_poscar_species(text)
        natoms = sum(counts)
    except (TypeError, ValueError):
        natoms = 0
    return {'kind': 'structure_file', 'path': path, 'snapshot': snapshot,
            'natoms': natoms, 'complete': complete}


def _load_structure_text(source: dict) -> str:
    if source['kind'] == 'xdatcar':
        return _load_xdatcar_frame(source)
    snapshot = source.get('snapshot')
    expected_source = str(source.get('source_sha256') or '')
    expected_frame = str(source.get('frame_sha256') or '')
    if (not isinstance(snapshot, dict)
            or not _SHA256_RE.fullmatch(expected_source)
            or not _SHA256_RE.fullmatch(expected_frame)
            or expected_source != expected_frame
            or snapshot.get('sha256') != expected_source
            or snapshot.get('size') != source.get('source_size')):
        raise StaleFrameSourceError('structure frame lacks immutable content binding')
    text, complete = _read_text(snapshot, max_bytes=_MAX_STRUCTURE_BYTES)
    if not text or not complete:
        raise ValueError('结构帧仍在写入或不可读，请刷新播放器快照')
    return text


def _neb_frame_dirs(root: Path) -> list[Path]:
    frames = []
    for path in root.iterdir():
        if not _FRAME_DIR_RE.fullmatch(path.name):
            continue
        _plain_lstat(path, directory=True, label='NEB image directory')
        if path.is_dir():
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
    row = _open_snapshot_file(path, max_bytes=_MAX_CORRECTION_RECORD_BYTES)
    try:
        raw = _read_snapshot_bytes(
            row, max_bytes=_MAX_CORRECTION_RECORD_BYTES,
            line_limit=_MAX_CORRECTION_RECORD_BYTES)
    finally:
        _close_source_rows([row])
    payload = json.loads(raw.decode('utf-8'))
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
        'plan_token_sha256',
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
            'cas_manifest_sha256', 'cas_diagnosis_sha256', 'plan_token_sha256'):
        if not _SHA256_RE.fullmatch(str(payload.get(key) or '')):
            raise ValueError(f'{key} is invalid')
    for key in ('request_fingerprint', 'correction_binding_sha256'):
        if key in payload and not _SHA256_RE.fullmatch(
                str(payload.get(key) or '')):
            raise ValueError(f'{key} is invalid')
    for key in ('allow_round_limit_override', 'manual_round_limit_override'):
        if key in payload and not isinstance(payload.get(key), bool):
            raise ValueError(f'{key} is invalid')
    if (payload.get('manual_round_limit_override') is True
            and payload.get('allow_round_limit_override') is not True):
        raise ValueError('manual round-limit override lacks explicit authority')
    cas_files = payload.get('cas_files_sha256')
    if (not isinstance(cas_files, dict)
            or not {'job.yaml', 'INCAR', 'CONTCAR'}.issubset(cas_files)
            or any(not isinstance(name, str)
                   or not _SHA256_RE.fullmatch(str(digest or ''))
                   for name, digest in cas_files.items())):
        raise ValueError('cas_files_sha256 is invalid')
    if cas_files['job.yaml'] != payload['cas_manifest_sha256']:
        raise ValueError('manifest hash is not bound to the CAS file map')
    cas_contract = payload.get('cas_contract')
    if (not isinstance(cas_contract, dict)
            or _json_hash(cas_contract) != payload['cas_anchor_sha256']
            or cas_contract.get('files') != cas_files
            or cas_contract.get('ledger_job_id') != payload['ledger_job_id']
            or cas_contract.get('manifest_job_id') != payload['manifest_job_id']
            or cas_contract.get('diagnosis_sha256')
            != payload['cas_diagnosis_sha256']
            or cas_contract.get('source_hash') != payload['source_hash']):
        raise ValueError('cas_contract is not bound to the correction record')
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


def _bounded_correction_paths(directory: Path) -> list[Path]:
    before = _plain_lstat(
        directory, directory=True, label='correction record directory')
    paths, total_bytes = [], 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(paths) >= _MAX_CORRECTION_RECORDS:
                raise TrajectoryLimitError(
                    'correction journal exceeds the record limit')
            path = Path(entry.path)
            value = path.stat(follow_symlinks=False)
            total_bytes += int(value.st_size)
            if total_bytes > _MAX_CORRECTION_TOTAL_BYTES:
                raise TrajectoryLimitError(
                    'correction journal exceeds the cumulative byte limit')
            paths.append(path)
    after = _plain_lstat(
        directory, directory=True, label='correction record directory')
    if not _same_entity(before, after):
        raise StaleFrameSourceError('correction record directory changed while scanned')
    return sorted(paths, key=lambda path: path.name)


def correction_method_compatibility(job_dir) -> dict:
    """Validate immutable, strictly paired correction records for consumers."""
    root = Path(job_dir).absolute()
    directory = root / _CORRECTION_DIR
    records, issues = [], []
    with _CORRECTION_LOCK:
        try:
            _plain_lstat(root, directory=True, label='registered job root')
            directory.stat(follow_symlinks=False)
        except FileNotFoundError:
            return {
                'status': 'no_corrections', 'record_count': 0,
                'records': [], 'issues': [],
                'binding_sha256': _json_hash([]),
            }
        except (OSError, ValueError) as exc:
            issue = f'correction record directory is invalid: {exc}'
            return {
                'status': 'unknown_invalid_record', 'record_count': 0,
                'records': [], 'issues': [issue],
                'binding_sha256': _json_hash([issue]),
            }
        try:
            directory_entity = _plain_lstat(
                directory, directory=True,
                label='correction record directory')
            paths = _bounded_correction_paths(directory)
        except (OSError, ValueError, StaleFrameSourceError) as exc:
            issue = f'correction record directory is invalid: {exc}'
            return {
                'status': 'unknown_invalid_record', 'record_count': 0,
                'records': [], 'issues': [issue],
                'binding_sha256': _json_hash([issue]),
            }
        for path in paths:
            try:
                records.append(_validated_correction_record(path))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                    ValueError, StaleFrameSourceError) as exc:
                issues.append(
                    f'correction record {path.name} is invalid: {exc}')
        try:
            if not _same_entity(directory_entity, _plain_lstat(
                    directory, directory=True,
                    label='correction record directory')):
                issues.append('correction record directory changed while read')
        except (OSError, ValueError):
            issues.append('correction record directory changed while read')
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
                'incar_sha256_before', 'plan_token_sha256', 'cas_contract',
                'request_fingerprint', 'correction_binding_sha256',
                'allow_round_limit_override', 'manual_round_limit_override'):
            if outcome.get(key) != intent.get(key):
                issues.append(f'correction pair {record_id} disagrees on {key}')
        if outcome.get('intent_record_hash') != intent.get('record_hash'):
            issues.append(f'correction pair {record_id} has the wrong intent hash')
    if issues:
        status = 'unknown_invalid_record'
    elif set(intents) - set(outcomes):
        status = 'unknown_pending'
    elif any(row.get('status') == 'failed_or_unknown'
             for row in outcomes.values()):
        status = 'unknown_failed_or_unknown'
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
    binding = [{
        'record_id': row['record_id'], 'record_type': row['record_type'],
        'record_hash': row['record_hash'], 'status': row.get('status'),
        'method_compatibility': row.get('method_compatibility'),
    } for row in records]
    return {
        'status': status, 'record_count': len(records),
        'records': projection, 'issues': issues,
        'binding_sha256': _json_hash({
            'records': binding, 'issues': issues, 'status': status}),
    }


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
        self._frame_tokens: OrderedDict[str, dict] = OrderedDict()
        self._repair_tokens: OrderedDict[str, dict] = OrderedDict()

    def _cleanup(self) -> None:
        cutoff = self._clock() - _SESSION_TTL_SECONDS
        expired = [token for token, session in self._sessions.items()
                   if session['last_access'] < cutoff]
        for token in expired:
            session = self._sessions.pop(token, None)
            if session is not None:
                _close_source_rows(session.get('sources') or [])
        if expired:
            expired_set = set(expired)
            self._frame_tokens = OrderedDict(
                (token, value) for token, value in self._frame_tokens.items()
                if value['session_token'] not in expired_set)
            self._repair_tokens = OrderedDict(
                (token, value) for token, value in self._repair_tokens.items()
                if value['session_token'] not in expired_set)
        while len(self._sessions) > _MAX_SESSIONS:
            token, old_session = self._sessions.popitem(last=False)
            _close_source_rows(old_session.get('sources') or [])
            self._frame_tokens = OrderedDict(
                (key, value) for key, value in self._frame_tokens.items()
                if value['session_token'] != token)
        while len(self._frame_tokens) > _MAX_FRAME_TOKENS:
            self._frame_tokens.popitem(last=False)

    @staticmethod
    def _source_paths(root: Path, kind: str, manifest: dict | None = None) -> list[Path]:
        names = ('job.yaml', 'INCAR', 'OSZICAR', 'OUTCAR', 'XDATCAR',
                 'CONTCAR', 'POSCAR')
        paths = []
        for name in names:
            path = root / name
            try:
                path.stat(follow_symlinks=False)
            except OSError:
                continue
            paths.append(path)
        diagnosis = (((manifest or {}).get('results') or {}).get('diagnosis') or {})
        evidence_name = diagnosis.get('output_file') if isinstance(diagnosis, dict) else None
        if isinstance(evidence_name, str) and evidence_name.strip():
            candidate = Path(evidence_name.strip())
            if (not candidate.is_absolute() and '..' not in candidate.parts):
                try:
                    path = (root / candidate).absolute()
                    path.stat(follow_symlinks=False)
                except OSError:
                    path = None
                if path is not None and path not in paths:
                    paths.append(path)
        if kind == 'neb':
            for frame in _neb_frame_dirs(root):
                for name in ('POSCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR'):
                    path = frame / name
                    try:
                        path.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    paths.append(path)
        return paths

    def _build_neb(self, root: Path, by_name: dict[str, dict]) -> tuple[
            list[dict], list[dict], bool, list[str], dict]:
        """Project NEB evidence from the already pinned source generation."""
        frame_dirs = _neb_frame_dirs(root)
        energies, forces, energy_sources = [], [], []
        rows, frames, partial, warnings = [], [], False, []
        for index, frame_dir in enumerate(frame_dirs):
            prefix = frame_dir.relative_to(root).as_posix()
            structure_row = next(
                (by_name.get(f'{prefix}/{name}')
                 for name in ('CONTCAR', 'POSCAR')
                 if by_name.get(f'{prefix}/{name}') is not None), None)
            structure_path = structure_row['path'] if structure_row else None
            source_index = None
            natoms = 0
            if structure_row is not None:
                try:
                    source_index = len(frames)
                    frame_source = _structure_file_source(
                        structure_path, snapshot=structure_row)
                    natoms = int(frame_source.get('natoms') or 0)
                    frames.append(frame_source)
                except ValueError:
                    partial = True
                    source_index = None
            else:
                partial = True
            osz_row = by_name.get(f'{prefix}/OSZICAR')
            out_row = by_name.get(f'{prefix}/OUTCAR')
            energy, energy_source, fmax = None, None, None
            if osz_row is not None:
                parsed_steps, _complete = _parse_oszicar(osz_row)
                if parsed_steps:
                    energy = _finite(parsed_steps[-1].get('energy_ev'))
                    energy_source = 'OSZICAR:E0'
            out_text = ''
            if out_row is not None:
                out_text, _out_complete = _read_text(
                    out_row, max_bytes=min(
                        _MAX_OUTCAR_BYTES, _MAX_NEB_OUTCAR_MATERIALIZE_BYTES))
                if energy is None:
                    hits = _NEB_SIGMA0_RE.findall(out_text)
                    energy = _finite(hits[-1]) if hits else None
                    energy_source = 'OUTCAR:energy(sigma->0)' if energy is not None else None
                if natoms > 0:
                    event = self._neb.parse_last_complete_image_step(out_text, natoms)
                    if event.get('status') == 'complete':
                        fmax = _finite(event.get('fmax'))
            energies.append(energy)
            forces.append(fmax)
            energy_sources.append(energy_source)
        if len(frame_dirs) < 3 or not energies or energies[0] is None:
            parsed = {}
            partial = True
            warning = ('NEB 初态或至少三个 image 的完整能量证据不可用；'
                       '能垒与过渡态结论已暂停。')
            warnings.append(warning)
        else:
            e0 = energies[0]
            relative = [value - e0 if value is not None else None
                        for value in energies]
            valid = [(idx, value) for idx, value in enumerate(relative)
                     if value is not None]
            ts_index, barrier_f = max(valid, key=lambda item: item[1])
            e_fin = energies[-1]
            barrier_r = (max(value - e_fin for value in energies if value is not None)
                         if e_fin is not None else None)
            intermediate = forces[1:-1]
            climbing = bool(intermediate) and all(
                    value is not None
                    and value < float(getattr(self._neb, 'FORCE_TOL', 0.05))
                for value in intermediate)
            parsed = {
                'energies': energies, 'rel': relative,
                'barrier_f': barrier_f, 'barrier_r': barrier_r,
                'ts_index': ts_index, 'climbing_converged': climbing,
                'per_image_forces': forces, 'n_frames': len(frame_dirs),
                'warnings': [], 'energy_sources': energy_sources,
            }
            if ts_index in (0, len(frame_dirs) - 1):
                parsed['warnings'].append(
                    '能垒最高点落在端点,能垒可能未被 image 采样覆盖,建议加密 image 或检查路径。')
            if e_fin is None:
                parsed['warnings'].append('无法读取末态(N+1)能量,逆向能垒不可得。')
            if any(value is None for value in energies[1:-1]):
                parsed['warnings'].append(
                    '部分中间 image 能量缺失,能垒按可读 image 估计(可能偏低)。')
        gate = self._neb.neb_quality_gate(parsed) if parsed else {
            'ok': False, 'issues': list(warnings)}
        energies = list(parsed.get('energies') or [])
        relative = list(parsed.get('rel') or [])
        forces = list(parsed.get('per_image_forces') or [])
        for index, frame_dir in enumerate(frame_dirs):
            structure_key = next(
                (f'{frame_dir.relative_to(root).as_posix()}/{name}'
                 for name in ('CONTCAR', 'POSCAR')
                 if by_name.get(f'{frame_dir.relative_to(root).as_posix()}/{name}')
                 is not None), None)
            source_index = next(
                (i for i, source in enumerate(frames)
                 if structure_key is not None
                 and source['path'] == by_name[structure_key]['path']), None)
            energy = _finite(energies[index]) if index < len(energies) else None
            rel = _finite(relative[index]) if index < len(relative) else None
            fmax = _finite(forces[index]) if index < len(forces) else None
            partial = partial or energy is None
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

    def _build_series(self, root: Path, kind: str,
                      by_name: dict[str, dict]) -> tuple[
                          list[dict], list[dict], bool, list[str], dict]:
        osz_source = by_name.get('OSZICAR')
        out_source = by_name.get('OUTCAR')
        rows, osz_complete = (_parse_oszicar(osz_source)
                              if osz_source is not None else ([], False))
        forces, out_complete = (_parse_outcar_fmax(out_source, self._convergence)
                                if out_source is not None else ([], False))
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
        if by_name.get('XDATCAR') is not None:
            xdat_meta, frames, xdat_complete, xdat_warnings = _xdatcar_index(
                by_name['XDATCAR'])
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
            structure_row = next(
                (by_name.get(name) for name in ('CONTCAR', 'POSCAR')
                 if by_name.get(name) is not None), None)
            if structure_row is not None:
                try:
                    frames.append(_structure_file_source(
                        structure_row['path'], snapshot=structure_row))
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
        root = Path(job_dir).absolute()
        try:
            root_stat = _plain_lstat(
                root, directory=True, label='registered job root')
        except OSError:
            raise ValueError('registered job is unavailable')
        job_row = _open_snapshot_file(
            root / 'job.yaml', max_bytes=_MAX_MANIFEST_BYTES)
        sources = []
        try:
            value = _manifest_from_snapshot(job_row)
            task_kind = self._task_analysis.normalize_task_key(
                kind or value.get('task_type') or 'relax')
            paths = self._source_paths(root, task_kind, value)
            sources, hash_stable = _source_snapshot(
                root, paths, preopened={job_row['path']: job_row})
            if not sources:
                raise ValueError('job has no trajectory or structure evidence')
            by_name = {row['name']: row for row in sources}
            if task_kind == 'neb':
                rows, frames, partial, warnings, analysis = self._build_neb(
                    root, by_name)
            else:
                rows, frames, partial, warnings, analysis = self._build_series(
                    root, task_kind, by_name)
            if len(rows) > _MAX_SESSION_STEPS or len(frames) > _MAX_SESSION_FRAMES:
                raise TrajectoryLimitError(
                    'trajectory exceeds the session row/frame limit')
            sources_by_path = {row['path']: row for row in sources}
            for frame_source in frames:
                source_row = sources_by_path.get(
                    Path(frame_source['path']).absolute())
                if source_row is None:
                    partial = True
                    warnings.append('结构帧来源未包含在内容快照中。')
                    continue
                frame_source['snapshot'] = source_row
                frame_source['source_sha256'] = source_row['sha256']
                frame_source['source_size'] = source_row['size']
                frame_source['session_source_hash'] = source_row['source_hash']
                if frame_source.get('kind') != 'xdatcar':
                    frame_source['frame_sha256'] = source_row['sha256']
            current_value = _current_manifest(root)
            current_paths = {
                path.absolute()
                for path in self._source_paths(root, task_kind, current_value)}
            snapshot_paths = {row['path'] for row in sources}
            source_set_stable = current_paths == snapshot_paths
            root_stable = _same_entity(
                root_stat, _plain_lstat(
                    root, directory=True, label='registered job root'))
            partial = (partial or not hash_stable or not source_set_stable
                       or not root_stable or not _snapshot_is_current(sources))
            if not hash_stable or not root_stable:
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
                    energies = [row.get('energy_ev') for row in rows
                                if row.get('energy_ev') is not None]
                    temperatures = [row.get('temperature_k') for row in rows
                                    if row.get('temperature_k') is not None]
                    drift = (energies[-1] - energies[0]) if energies else None
                    temperature_mean = (sum(temperatures) / len(temperatures)
                                        if temperatures else None)
                    temp_text = (f'，平均温度={temperature_mean:.1f} K'
                                 if temperature_mean is not None else '')
                    summary = (f'解析 {len(energies)} 步，首末能量漂移='
                               f'{drift:+.6f} eV{temp_text}；'
                               '该摘要不替代轨迹长度与结构完整性检查。')
                    analysis = {
                        'source': 'vcstudio.project.task_analysis.analyze_aimd',
                        'ok': bool(energies), 'summary': summary,
                        'n_steps': len(energies),
                        'energy_first_ev': energies[0] if energies else None,
                        'energy_last_ev': energies[-1] if energies else None,
                        'energy_drift_total_ev': drift,
                        'temperature_mean_k': temperature_mean,
                        'trajectory_natoms': natoms,
                    }
        except Exception:
            _close_source_rows(sources or [job_row])
            raise
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
            'root_identity': _entity_identity(root_stat),
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
        # Parsing above used the pinned handles.  Release them before returning
        # so Windows atomic writers can replace completed VASP/manifest files;
        # later reads securely reopen and must match identity plus digest.
        _close_source_rows(sources)
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
        try:
            root_stat = _plain_lstat(
                session['root'], directory=True, label='registered job root')
            if _entity_identity(root_stat) != session['root_identity']:
                return True
            manifest = _current_manifest(session['root'])
            current_paths = {
                path.absolute() for path in self._source_paths(
                    session['root'], session['kind'], manifest)}
            snapshot_paths = {row['path'] for row in session['sources']}
            return (current_paths != snapshot_paths
                    or not _snapshot_is_current(session['sources']))
        except (OSError, ValueError, StaleFrameSourceError):
            return True

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
        source = session['frames'][frame_source]
        token = f'frame-{self._token_factory()}'
        with self._lock:
            self._frame_tokens[token] = {
                'session_token': session['token'],
                'frame_source': int(frame_source),
                'session_source_hash': session['source_hash'],
                'source_sha256': source.get('source_sha256'),
                'source_size': source.get('source_size'),
                'frame_sha256': source.get('frame_sha256'),
            }
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
                try:
                    row.update(self._frame_metric(session, frame_source))
                except StaleFrameSourceError:
                    return self._stale_payload(session, STEP_PAGE_SCHEMA)
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
        session = self._session(binding['session_token'])
        if self._is_stale(session):
            return self._stale_payload(session, FRAME_SCHEMA)
        frame_source = binding['frame_source']
        if not 0 <= frame_source < len(session['frames']):
            raise LookupError('frame token no longer resolves')
        source = session['frames'][frame_source]
        if (binding.get('session_source_hash') != session['source_hash']
                or binding.get('source_sha256') != source.get('source_sha256')
                or binding.get('source_size') != source.get('source_size')
                or binding.get('frame_sha256') != source.get('frame_sha256')):
            return self._stale_payload(session, FRAME_SCHEMA)
        natoms = int(source.get('natoms') or 0)
        if natoms > _MAX_FRAME_RENDER_ATOMS:
            raise TrajectoryLimitError('selected frame exceeds the atom render limit')
        try:
            content = _load_structure_text(source)
        except StaleFrameSourceError:
            return self._stale_payload(session, FRAME_SCHEMA)
        view = self._bounded_structure_view(content, natoms)
        metric = self._frame_metric(session, frame_source)
        return {
            'schema': FRAME_SCHEMA, 'ok': True, 'stale': False,
            'partial_write': bool(session['partial_write']),
            'session_token': session['token'], 'source_hash': session['source_hash'],
            'frame_token': str(frame_token), 'view': view, 'metrics': metric,
        }

    @staticmethod
    def _effective_incar(source) -> dict[str, str]:
        text, _complete = _read_text(source, max_bytes=_MAX_INCAR_BYTES)
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
            path = (root / candidate).absolute()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise RuntimeError('repair CAS evidence escaped the job root') from exc
            try:
                _assert_source_ancestry(root, path)
                files[candidate.as_posix()] = _file_hash(path)
            except OSError as exc:
                raise RuntimeError('repair CAS evidence is missing') from exc
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
        current_manifest = manifest or _current_manifest(session['root'])
        current = self._current_cas_contract(
            session['root'], session, current_manifest, expected)
        if current != expected:
            raise RuntimeError('repair evidence content changed; refresh before confirming')
        return current_manifest

    def _repair_execution_evidence(self, session: dict, manifest: dict) -> list[str]:
        blockers = []
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        by_name = {row['name']: row for row in session['sources']}
        for name in ('job.yaml', 'INCAR', 'CONTCAR', 'OSZICAR', 'OUTCAR'):
            if name not in by_name:
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
            rows, osz_complete = _parse_oszicar(by_name['OSZICAR'])
            forces, out_complete = _parse_outcar_fmax(
                by_name['OUTCAR'], self._convergence)
            if not osz_complete or not rows:
                blockers.append('oszicar_incomplete')
            if not out_complete or len(forces) != len(rows):
                blockers.append('outcar_incomplete')
        except (OSError, ValueError):
            blockers.append('output_parse_unavailable')
        try:
            from vcstudio.cluster import diagnose

            contcar, contcar_complete = _read_text(
                by_name['CONTCAR'], max_bytes=_MAX_STRUCTURE_BYTES)
            if not contcar_complete or not diagnose.valid_poscar(contcar):
                blockers.append('contcar_incomplete')
        except (OSError, ValueError):
            blockers.append('contcar_incomplete')
        return list(dict.fromkeys(blockers))

    def repair_preview(self, session_token: str, *,
                       allow_round_limit_override: bool = False) -> dict:
        session = self._session(session_token)
        if self._is_stale(session):
            return self._stale_payload(session, REPAIR_SCHEMA)
        if not isinstance(allow_round_limit_override, bool):
            raise ValueError('round-limit override authority must be explicit')
        manifest = _current_manifest(session['root'])
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
        by_name = {row['name']: row for row in session['sources']}
        effective = (self._effective_incar(by_name['INCAR'])
                     if by_name.get('INCAR') is not None else {})
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
        manual_round_override = bool(
            allow_round_limit_override and rounds == max_rounds)
        if rounds == max_rounds and not manual_round_override:
            blockers.append('continue_round_limit_reached')
        elif rounds > max_rounds:
            blockers.append('continue_round_limit_exceeded')
        correction_history = self._correction_projection(session['root'])
        if str(correction_history.get('status') or '').startswith('unknown'):
            blockers.append('correction_history_unknown')
        blockers = list(dict.fromkeys(blockers))
        cas_contract = self._repair_cas_contract(session, manifest)
        cas_anchor = _json_hash(cas_contract)
        execution_allowed = (restartable and session['kind'] != 'neb'
                             and (rounds < max_rounds or manual_round_override)
                             and failure_class != 'UNKNOWN'
                             and not blockers)
        action = 'continue_frozen_incar' if execution_allowed else 'pause'
        correction_binding = str(correction_history.get('binding_sha256') or '')
        fingerprint_body = {
            'schema': 'vcstudio.repair-request-fingerprint/v1',
            'session_token': session['token'],
            'source_hash': session['source_hash'],
            'cas_anchor_sha256': cas_anchor,
            'correction_binding_sha256': correction_binding,
            'correction_record_count': int(
                correction_history.get('record_count') or 0),
            'action': action, 'continue_rounds': rounds,
            'allow_round_limit_override': allow_round_limit_override,
            'manual_round_limit_override': manual_round_override,
        }
        request_fingerprint = _json_hash(fingerprint_body)
        created_at = _utc_now()
        plan_body = {
            'job_id': session['job_id'], 'source_hash': session['source_hash'],
            'failure_class': failure_class, 'diagnosis_hash': _json_hash(diagnosis),
            'action': action, 'continue_rounds': rounds,
            'cas_anchor_sha256': cas_anchor,
            'correction_binding_sha256': correction_binding,
            'correction_record_count': int(
                correction_history.get('record_count') or 0),
            'allow_round_limit_override': allow_round_limit_override,
            'manual_round_limit_override': manual_round_override,
            'request_fingerprint': request_fingerprint,
            'incar_sha256': (by_name['INCAR']['sha256']
                             if by_name.get('INCAR') is not None else None),
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
            'session_token': session['token'],
            'source_hash': session['source_hash'],
            'plan_token': plan_token, 'plan_id': plan_id,
            'request_fingerprint': request_fingerprint,
            'correction_binding_sha256': correction_binding,
            'allow_round_limit_override': allow_round_limit_override,
            'manual_round_limit_override': manual_round_override,
            'failure_class': failure_class, 'detected_evidence': evidence,
            'suggested_change': guidance['change'],
            'estimated_cost': plan['estimated_cost'],
            'scientific_impact': guidance['impact'], 'method_diff': method_diff,
            'default_decision': 'pause', 'execution_action': action,
            'execution_allowed': execution_allowed,
            'execution_blockers': blockers,
            'unknown_pauses': failure_class == 'UNKNOWN',
            'correction_history': correction_history,
        }

    @staticmethod
    def _write_immutable_record(root: Path, filename: str, payload: dict) -> dict:
        body = dict(payload)
        body['schema'] = CORRECTION_SCHEMA
        body['record_hash'] = _json_hash(body)
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        encoded_bytes = encoded.encode('utf-8')
        if len(encoded_bytes) > _MAX_CORRECTION_RECORD_BYTES:
            raise TrajectoryLimitError('correction record exceeds the byte limit')
        root = Path(root).absolute()
        directory = root / _CORRECTION_DIR
        path = directory / filename
        with _CORRECTION_LOCK:
            _plain_lstat(root, directory=True, label='registered job root')
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            directory_before = _plain_lstat(
                directory, directory=True, label='correction record directory')
            paths = _bounded_correction_paths(directory)
            existing_total = sum(
                item.stat(follow_symlinks=False).st_size for item in paths)
            if (path not in paths and len(paths) >= _MAX_CORRECTION_RECORDS):
                raise TrajectoryLimitError(
                    'correction journal exceeds the record limit')
            if (path not in paths
                    and existing_total + len(encoded_bytes) > _MAX_CORRECTION_TOTAL_BYTES):
                raise TrajectoryLimitError(
                    'correction journal exceeds the cumulative byte limit')
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NOFOLLOW', 0))
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                try:
                    existing = _validated_correction_record(path)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                        ValueError, StaleFrameSourceError) as exc:
                    raise RuntimeError(
                        'existing immutable correction record differs from proposed content') from exc
                if existing != body:
                    raise RuntimeError(
                        'existing immutable correction record differs from proposed content')
                directory_after = _plain_lstat(
                    directory, directory=True,
                    label='correction record directory')
                if not _same_entity(directory_before, directory_after):
                    raise RuntimeError(
                        'correction record directory changed while reading')
                return existing
            try:
                opened = os.fstat(descriptor)
                current_path = _plain_lstat(
                    path, directory=False, label='correction record')
                if (not stat.S_ISREG(opened.st_mode) or _is_reparse(opened)
                        or not _same_entity(opened, current_path)):
                    raise RuntimeError(
                        'correction record entity changed while created')
                with os.fdopen(
                        descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                    descriptor = -1
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            directory_after = _plain_lstat(
                directory, directory=True, label='correction record directory')
            if not _same_entity(directory_before, directory_after):
                raise RuntimeError(
                    'correction record directory changed while writing')
            return body

    def prepare_repair(self, plan_token: str, decision: str,
                       operation_key: str, *,
                       allow_round_limit_override: bool = False,
                       request_fingerprint: str | None = None) -> dict:
        with self._lock:
            self._cleanup()
            plan = self._repair_tokens.get(str(plan_token or ''))
        if plan is None:
            raise LookupError('repair plan expired or is unknown')
        if decision != 'continue_frozen_incar' or not plan['execution_allowed']:
            raise ValueError('repair remains paused; this plan does not authorize execution')
        if (not isinstance(allow_round_limit_override, bool)
                or allow_round_limit_override
                != plan['allow_round_limit_override']):
            raise ValueError('repair round-limit override authority changed')
        supplied_fingerprint = str(request_fingerprint or '')
        if (supplied_fingerprint
                and supplied_fingerprint != plan['request_fingerprint']):
            raise ValueError('repair request fingerprint changed')
        if plan['manual_round_limit_override'] and not supplied_fingerprint:
            raise ValueError(
                'manual round-limit override requires the preview fingerprint')
        key = str(operation_key or '').strip()
        if not _OPERATION_KEY_RE.fullmatch(key):
            raise ValueError('a stable idempotency key is required for repair confirmation')
        session = self._session(plan['session_token'])
        manifest = self._assert_cas_contract(session, plan['cas_contract'])
        blockers = self._repair_execution_evidence(session, manifest)
        if blockers:
            raise RuntimeError('repair terminal/output evidence is no longer complete')
        correction_id = hashlib.sha256(
            f'{plan["plan_id"]}|{key}'.encode('utf-8')).hexdigest()[:24]
        current_correction = self._correction_projection(session['root'])
        if (current_correction.get('binding_sha256')
                != plan['correction_binding_sha256']):
            own_intent = (session['root'] / _CORRECTION_DIR
                          / f'intent-{correction_id}.json')
            try:
                correction_paths = _bounded_correction_paths(
                    session['root'] / _CORRECTION_DIR)
            except (OSError, ValueError, StaleFrameSourceError):
                correction_paths = []
            idempotent_retry = (
                len(correction_paths) == int(plan['correction_record_count']) + 1
                and own_intent in correction_paths)
            if not idempotent_retry:
                raise RuntimeError(
                    'correction history changed; refresh repair preview before confirming')
        diagnosis = ((manifest.get('results') or {}).get('diagnosis') or {})
        if _json_hash(diagnosis) != plan['diagnosis_hash']:
            raise RuntimeError('diagnosis changed; refresh repair preview before confirming')
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
                'cas_contract': plan['cas_contract'],
                'plan_id': plan['plan_id'], 'failure_class': plan['failure_class'],
                'request_fingerprint': plan['request_fingerprint'],
                'correction_binding_sha256': plan['correction_binding_sha256'],
                'allow_round_limit_override': plan['allow_round_limit_override'],
                'manual_round_limit_override': plan['manual_round_limit_override'],
                'plan_token_sha256': hashlib.sha256(
                    plan['plan_token'].encode('utf-8')).hexdigest(),
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
            'request_fingerprint': plan['request_fingerprint'],
            'correction_binding_sha256': plan['correction_binding_sha256'],
            'allow_round_limit_override': plan['allow_round_limit_override'],
            'manual_round_limit_override': plan['manual_round_limit_override'],
            'ledger_job_id': plan['cas_contract']['ledger_job_id'],
            'manifest_job_id': plan['cas_contract']['manifest_job_id'],
            'cas_anchor_sha256': plan['cas_anchor_sha256'],
            'cas_manifest_sha256': plan['cas_contract']['files']['job.yaml'],
            'cas_diagnosis_sha256': plan['cas_contract']['diagnosis_sha256'],
            'cas_files_sha256': plan['cas_contract']['files'],
            'cas_contract': plan['cas_contract'],
            'plan_token_sha256': record['plan_token_sha256'],
            'record_created_at': plan['created_at'],
            'failure_class': plan['failure_class'],
            'session_token': session['token'],
            'journal_binding': self._journal_binding(record),
        }

    @staticmethod
    def _journal_binding(intent: dict) -> dict:
        return {
            'schema': 'vcstudio.correction-journal-link/v1',
            'correction_id': intent['record_id'],
            'intent_record_hash': intent['record_hash'],
            'plan_id': intent['plan_id'],
            'plan_token_sha256': intent['plan_token_sha256'],
            'job_id': intent['job_id'],
            'ledger_job_id': intent['ledger_job_id'],
            'manifest_job_id': intent['manifest_job_id'],
            'cas_anchor_sha256': intent['cas_anchor_sha256'],
            'idempotency_key': intent['idempotency_key'],
        }

    def assert_repair_cas(self, prepared: dict, *, ledger_job_id: str,
                          manifest: dict | None = None) -> dict:
        if str(ledger_job_id or '') != prepared.get('ledger_job_id'):
            raise RuntimeError('job ledger binding changed; repair remains paused')
        session = self._session(prepared.get('session_token'))
        if (session['job_id'] != prepared.get('job_id')
                or session['root'] != Path(prepared['job_dir']).absolute()):
            raise RuntimeError('repair session binding changed')
        current = self._assert_cas_contract(
            session, prepared['cas_contract'], manifest=manifest)
        blockers = self._repair_execution_evidence(session, current)
        if blockers:
            raise RuntimeError('repair terminal/output evidence is no longer complete')
        return prepared['cas_contract']

    @staticmethod
    def _prepared_from_intent(root: Path, intent: dict) -> dict:
        return {
            'job_id': intent['job_id'], 'job_dir': root,
            'operation_key': intent['idempotency_key'],
            'plan_id': intent['plan_id'], 'correction_id': intent['record_id'],
            'intent_record_hash': intent['record_hash'],
            'incar_sha256_before': intent.get('incar_sha256_before'),
            'source_hash': intent['source_hash'],
            'request_fingerprint': intent.get('request_fingerprint'),
            'correction_binding_sha256': intent.get(
                'correction_binding_sha256'),
            'allow_round_limit_override': bool(
                intent.get('allow_round_limit_override')),
            'manual_round_limit_override': bool(
                intent.get('manual_round_limit_override')),
            'ledger_job_id': intent['ledger_job_id'],
            'manifest_job_id': intent['manifest_job_id'],
            'cas_anchor_sha256': intent['cas_anchor_sha256'],
            'cas_manifest_sha256': intent['cas_manifest_sha256'],
            'cas_diagnosis_sha256': intent['cas_diagnosis_sha256'],
            'cas_files_sha256': intent['cas_files_sha256'],
            'cas_contract': intent['cas_contract'],
            'plan_token_sha256': intent['plan_token_sha256'],
            'record_created_at': intent['created_at'],
            'failure_class': intent.get('failure_class'),
            'journal_binding': TrajectoryReviewService._journal_binding(intent),
        }

    def _durable_intent(self, root: Path, job_id: str, plan_token: str,
                        operation_key: str) -> dict | None:
        directory = root / _CORRECTION_DIR
        with _CORRECTION_LOCK:
            try:
                directory.stat(follow_symlinks=False)
            except FileNotFoundError:
                return None
            directory_before = _plain_lstat(
                directory, directory=True,
                label='correction record directory')
            paths = [path for path in _bounded_correction_paths(directory)
                     if path.name.startswith('intent-')]
            token_hash = hashlib.sha256(
                str(plan_token or '').encode('utf-8')).hexdigest()
            matches = []
            for path in paths:
                intent = _validated_correction_record(path)
                if (intent['job_id'] == job_id
                        and intent['idempotency_key'] == operation_key
                        and intent['plan_token_sha256'] == token_hash):
                    matches.append(intent)
            directory_after = _plain_lstat(
                directory, directory=True,
                label='correction record directory')
            if not _same_entity(directory_before, directory_after):
                raise RuntimeError(
                    'correction record directory changed while reading')
        if len(matches) > 1:
            raise RuntimeError('correction intent identity is ambiguous')
        return matches[0] if matches else None

    def reconcile_repair_outcome(self, job_dir, job_id: str, plan_token: str,
                                 operation_key: str) -> dict | None:
        """Fill a missing outcome from exact durable local submit evidence only."""
        identifier = str(job_id or '').strip()
        key = str(operation_key or '').strip()
        if not _JOB_ID_RE.fullmatch(identifier) or not _OPERATION_KEY_RE.fullmatch(key):
            raise ValueError('durable correction replay identity is invalid')
        root = Path(job_dir).absolute()
        _plain_lstat(root, directory=True, label='registered job root')
        intent = self._durable_intent(root, identifier, plan_token, key)
        if intent is None:
            return None
        prepared = self._prepared_from_intent(root, intent)
        from vcstudio.cluster import submitter

        with submitter.job_operation(root, '修复审计重放'):
            replay = submitter.replay_repair_continuation_locked(
                root, key, prepared['journal_binding'])
            if replay is None:
                return None
            operation = {
                'ok': True, 'replayed': True, 'idempotency_key': key,
                'results': [[root, True,
                             f'已确认续算，新作业号 {replay["scheduler_job_id"]}']],
            }
            correction = self._record_repair_outcome_unlocked(prepared, operation)
        return {
            'prepared': prepared, 'operation': operation,
            'correction_outcome': correction,
        }

    def record_repair_outcome(self, prepared: dict, outcome: dict) -> dict:
        from vcstudio.cluster import submitter

        root = Path(prepared['job_dir']).absolute()
        _plain_lstat(root, directory=True, label='registered job root')
        with submitter.job_operation(root, '修复审计落盘'):
            return self._record_repair_outcome_unlocked(prepared, outcome)

    def _record_repair_outcome_unlocked(self, prepared: dict,
                                        outcome: dict) -> dict:
        root = Path(prepared['job_dir']).absolute()
        _plain_lstat(root, directory=True, label='registered job root')
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
            'plan_token_sha256': 'plan_token_sha256',
            'cas_contract': 'cas_contract',
            'request_fingerprint': 'request_fingerprint',
            'correction_binding_sha256': 'correction_binding_sha256',
            'allow_round_limit_override': 'allow_round_limit_override',
            'manual_round_limit_override': 'manual_round_limit_override',
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
        try:
            current_hash = _file_hash(root / 'INCAR')
        except FileNotFoundError:
            current_hash = None
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
                'cas_contract': prepared['cas_contract'],
                'plan_id': prepared['plan_id'],
                'plan_token_sha256': prepared['plan_token_sha256'],
                'request_fingerprint': prepared.get('request_fingerprint'),
                'correction_binding_sha256': prepared.get(
                    'correction_binding_sha256'),
                'allow_round_limit_override': bool(
                    prepared.get('allow_round_limit_override')),
                'manual_round_limit_override': bool(
                    prepared.get('manual_round_limit_override')),
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
            })
        return {
            'record_id': record['record_id'], 'record_hash': record['record_hash'],
            'status': status, 'method_compatibility': record['method_compatibility'],
        }
