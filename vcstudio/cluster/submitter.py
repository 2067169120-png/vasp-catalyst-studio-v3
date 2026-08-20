"""提交编排:preflight → 上传四件套+脚本 → 提交 → 回写 job.yaml;查状态 → 收敛判定。

client/sftp 由调用方注入(GUI 经 connection.open_client;测试注入假件),
本模块不 import paramiko——全部逻辑可离线测试。
安全阀:preflight 不过绝不出手;提交动作逐条写入 manifest.attempts(可审计)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

from contextlib import contextmanager
import functools
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import tempfile
import threading
import time

from vcstudio.cluster import script_builder
from vcstudio.cluster import diagnose
from vcstudio.cluster.schedulers import (
    JobScriptSpec, get_dialect, QUEUED, RUNNING, GONE,
)
from vcstudio.engines.calcspec import ENGINE_RUN_CONTRACTS, get_run_contract
from vcstudio.shared import manifest as manifest_mod
from vcstudio.shared.execution_environment import (
    from_cluster_profile, validate_execution_environment,
)
from vcstudio.shared.scientific_inputs import (
    closure_record_matches, recorded_closure, resolve_input_closure,
)

SCRIPT_NAME = 'vcs_job.sh'
_INPUT_FILES = ('INCAR', 'POTCAR', 'KPOINTS', 'POSCAR')
# 有些 VASP 派生任务除了传统“四件套”还会在启动阶段硬读额外文件。过去提交器
# 只上传四件套，导致能带(ICHARG=11)和 Dimer 目录在本地看似完整、到集群却立刻
# 因 CHGCAR/MODECAR 缺失失败。这里把额外输入视为任务契约：缺失时在联网前拦截。
_VASP_TASK_INPUTS = {
    'bands': ('CHGCAR',),
    'band': ('CHGCAR',),                 # 兼容早期清单别名
    'dimer': ('MODECAR',),
}
_SUPPORTED_ENGINES = tuple(ENGINE_RUN_CONTRACTS)
_ENGINE_LABELS = {
    'vasp': 'VASP',
    'gaussian': 'Gaussian',
    'cp2k': 'CP2K',
    'castep': 'CASTEP',
}
_ENGINE_COMMAND_EXAMPLES = {
    key: contract.command_example for key, contract in ENGINE_RUN_CONTRACTS.items()
}
# NEB 根目录共享文件(POSCAR 在各 image 子目录,不在根)
_NEB_ROOT_FILES = ('INCAR', 'POTCAR', 'KPOINTS')
_E0_RE = re.compile(r'E0=\s*([-+.\dEe]+)')
_NEB_FRAME_RE = re.compile(r'^\d+$')
# NEB 收敛标志(各 image 力收敛后 VASP 向 stdout 打印,与离子弛豫同串)
_NEB_CONVERGED_MARK = 'reached required accuracy'
_REMOTE_NAMESPACE_RE = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')

_JOB_LOCKS: dict[str, threading.RLock] = {}
_JOB_LOCKS_GUARD = threading.Lock()
_JOB_OPERATION_LOCK_FILE = '.vcstudio-job-operation.lock'
_SUBMISSION_RECOVERY_FILE = '.vcstudio-submit-recovery.json'
_SUBMISSION_RECOVERY_SCHEMA = 1
_JOB_ACTION_JOURNAL_FILE = '.vcstudio-job-actions.json'
_JOB_ACTION_JOURNAL_SCHEMA = 1
_JOB_ACTION_STATUSES = {
    'prepared', 'remote_accepted', 'succeeded', 'failed', 'unknown_remote_outcome',
}
_JOB_ACTION_UNRESOLVED = {'prepared', 'remote_accepted', 'unknown_remote_outcome'}


class JobOperationBusy(RuntimeError):
    """A different process or thread currently owns a job mutation."""

    code = 'job_busy'
    busy = True
    requires_manual_recovery = False


class UnknownRemoteSubmission(RuntimeError):
    """A scheduler may have accepted a job whose local identity is not durable."""

    code = 'unknown_remote_submission'
    busy = False
    requires_manual_recovery = True

    def __init__(self, message, *, recovery_status='unknown_remote_submission',
                 scheduler_job_id=''):
        super().__init__(message)
        self.recovery_status = str(recovery_status or 'unknown_remote_submission')
        self.scheduler_job_id = str(scheduler_job_id or '')


class UnknownRemoteJobOperation(RuntimeError):
    """A continue/cancel command may have reached the scheduler but is unresolved."""

    code = 'unknown_remote_job_operation'
    busy = False
    requires_manual_recovery = True

    def __init__(self, message, *, action='', recovery_status='unknown_remote_outcome',
                 scheduler_job_id=''):
        super().__init__(message)
        self.action = str(action or '')
        self.recovery_status = str(recovery_status or 'unknown_remote_outcome')
        self.scheduler_job_id = str(scheduler_job_id or '')


class ReplayedJobOperationFailure(RuntimeError):
    """A durable operation key already has a terminal failed result."""

    code = 'job_operation_failed'
    busy = False
    requires_manual_recovery = False
    replayed = True


def _canonical_job_dir(job_dir) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.normpath(str(job_dir)))))


def _fsync_directory(path: str) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_job_file_lock(job_dir, action, *, create_dir=False):
    """Take a non-blocking OS lock on a stable per-job lock-file inode."""
    root = _canonical_job_dir(job_dir)
    if not os.path.isdir(root):
        if create_dir:
            os.makedirs(root, exist_ok=True)
        else:
            raise ValueError('作业目录不存在，无法执行远程操作')
    lock_path = os.path.join(root, _JOB_OPERATION_LOCK_FILE)
    try:
        handle = open(lock_path, 'a+b')
    except OSError as exc:
        raise RuntimeError('作业操作锁不可用，未执行任何远程操作') from exc
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError) as exc:
            raise JobOperationBusy(
                f'{action}已跳过：该作业正在执行另一项提交/刷新/下载/续算操作') from exc
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor also releases the kernel lock.
                pass
        handle.close()


@contextmanager
def job_operation(job_dir, action='远程操作', *, create_dir=False):
    """Serialize manifest/remote mutations for one job across all GUI processes.

    The in-process lock rejects concurrent pywebview threads promptly.  The
    stable advisory file lock is the authority across independent desktop/EXE
    instances and covers the entire read-check-remote-write transaction.
    """
    key = _canonical_job_dir(job_dir)
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        raise JobOperationBusy(
            f'{action}已跳过：该作业正在执行另一项提交/刷新/下载/续算操作')
    try:
        with _exclusive_job_file_lock(key, action, create_dir=create_dir):
            yield
    finally:
        lock.release()


def _submission_recovery_path(job_dir) -> str:
    return os.path.join(_canonical_job_dir(job_dir), _SUBMISSION_RECOVERY_FILE)


def _durable_json_write(path: str, payload: dict) -> None:
    parent = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}.', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def _read_submission_recovery(job_dir) -> dict | None:
    path = _submission_recovery_path(job_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnknownRemoteSubmission(
            '提交恢复记录不可读；为避免重复提交，必须人工核对调度器后再处理。',
            recovery_status='invalid_recovery_journal') from exc
    valid_statuses = {
        'preparing', 'submitting', 'remote_accepted',
        'unknown_remote_submission',
    }
    neb_hashes = payload.get('neb_image_poscar_sha256') \
        if isinstance(payload, dict) else None
    neb_hashes_valid = (
        neb_hashes is None
        or (isinstance(neb_hashes, dict) and 3 <= len(neb_hashes) <= 1000
            and all(re.fullmatch(r'[0-9]{2,4}', str(key or ''))
                    and re.fullmatch(r'[0-9a-f]{64}', str(value or ''))
                    for key, value in neb_hashes.items())
            and list(neb_hashes) == [
                f'{index:02d}' for index in range(len(neb_hashes))]))
    if (not isinstance(payload, dict)
            or payload.get('schema') != _SUBMISSION_RECOVERY_SCHEMA
            or payload.get('status') not in valid_statuses
            or not isinstance(payload.get('transaction_id'), str)
            or (payload.get('incar_sha256') is not None
                and re.fullmatch(
                    r'[0-9a-f]{64}', str(payload.get('incar_sha256') or ''))
                is None)
            or not neb_hashes_valid):
        raise UnknownRemoteSubmission(
            '提交恢复记录无效；为避免重复提交，必须人工核对调度器后再处理。',
            recovery_status='invalid_recovery_journal')
    return payload


def _write_submission_recovery(job_dir, payload: dict) -> None:
    _durable_json_write(_submission_recovery_path(job_dir), payload)


def _clear_submission_recovery(job_dir) -> None:
    path = _submission_recovery_path(job_dir)
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    _fsync_directory(os.path.dirname(path))


def _job_action_journal_path(job_dir) -> str:
    return os.path.join(_canonical_job_dir(job_dir), _JOB_ACTION_JOURNAL_FILE)


def _validate_job_action_key(value) -> str:
    key = str(value or '').strip()
    if key and (len(key) > 128
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{12,128}', key)):
        raise ValueError('无效的作业操作请求标识')
    return key


def _empty_job_action_journal() -> dict:
    return {'schema': _JOB_ACTION_JOURNAL_SCHEMA, 'operations': []}


def _job_action_request_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _read_job_action_journal(job_dir) -> dict:
    """Read and validate the durable continue/cancel operation ledger.

    An unreadable journal is never treated as empty: doing so after a crash
    could repeat a scheduler mutation whose result was already accepted.
    """
    path = _job_action_journal_path(job_dir)
    if not os.path.isfile(path):
        return _empty_job_action_journal()
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnknownRemoteJobOperation(
            '作业远端操作 journal 不可读；为避免重复续算/取消，必须人工核对调度器。',
            recovery_status='invalid_journal') from exc
    operations = payload.get('operations') if isinstance(payload, dict) else None
    valid = (payload.get('schema') == _JOB_ACTION_JOURNAL_SCHEMA
             if isinstance(payload, dict) else False)
    valid = valid and isinstance(operations, list)
    if valid:
        for record in operations:
            if (not isinstance(record, dict)
                    or not isinstance(record.get('transaction_id'), str)
                    or not record.get('transaction_id')
                    or record.get('action') not in {
                        'continue', 'tune_continue', 'cancel'}
                    or record.get('status') not in _JOB_ACTION_STATUSES
                    or not isinstance(record.get('source_job_id'), str)
                    or record.get('idempotency_key') is not None
                    and not isinstance(record.get('idempotency_key'), str)
                    or record.get('request_sha256') is not None
                    and (not isinstance(record.get('request_sha256'), str)
                         or not re.fullmatch(
                             r'[0-9a-f]{64}', record['request_sha256']))
                    or record.get('intent_sha256') is not None
                    and (not isinstance(record.get('intent_sha256'), str)
                         or not re.fullmatch(
                             r'[0-9a-f]{64}', record['intent_sha256']))
                    or record.get('request') is not None
                    and not isinstance(record.get('request'), dict)):
                valid = False
                break
            request = record.get('request')
            request_digest = str(record.get('request_sha256') or '')
            if (request is not None
                    and _job_action_request_sha256(request) != request_digest):
                valid = False
                break
    if not valid:
        raise UnknownRemoteJobOperation(
            '作业远端操作 journal 无效；为避免重复续算/取消，必须人工核对调度器。',
            recovery_status='invalid_journal')
    return payload


def _write_job_action_journal(job_dir, payload: dict) -> None:
    _durable_json_write(_job_action_journal_path(job_dir), payload)


def _job_action_attempt(manifest: dict | None, *, action: str,
                        transaction_id: str = '', idempotency_key: str = '') -> dict | None:
    attempts = (manifest or {}).get('attempts') if isinstance(manifest, dict) else None
    if not isinstance(attempts, list):
        return None
    expected_action = {
        'continue': 'contcar_restart',
        'tune_continue': 'incar_tuned_restart',
        'cancel': 'cancel',
    }.get(action, '')
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict) or attempt.get('action') != expected_action:
            continue
        if transaction_id and str(attempt.get('operation_transaction_id') or '') == transaction_id:
            return attempt
        if idempotency_key and str(attempt.get('idempotency_key') or '') == idempotency_key:
            return attempt
    return None


def _current_scheduler_attempt(manifest: dict | None) -> dict | None:
    """Return the newest attempt that owns the manifest's current scheduler id."""
    current_job_id = str((manifest or {}).get('scheduler_job_id') or '')
    attempts = (manifest or {}).get('attempts') if isinstance(manifest, dict) else None
    if not current_job_id or not isinstance(attempts, list):
        return None
    return next((
        attempt for attempt in reversed(attempts)
        if isinstance(attempt, dict)
        and str(attempt.get('job_id') or '') == current_job_id
    ), None)


def _job_action_replay(manifest: dict, action: str, evidence: dict) -> dict:
    replay = dict(manifest)
    replay[f'_{action}_replayed'] = True
    if action == 'cancel':
        replay['_cancelled_job_id'] = str(
            evidence.get('target_job_id') or evidence.get('source_job_id') or '')
    return replay


def _job_action_generation_matches(manifest: dict, record: dict,
                                   evidence: dict | None) -> bool:
    if not evidence:
        return False
    if record.get('action') in {'continue', 'tune_continue'}:
        if (str(record.get('request_sha256') or '') !=
                str(evidence.get('operation_request_sha256') or '')):
            return False
        if (str(record.get('intent_sha256') or '') !=
                str(evidence.get('operation_intent_sha256') or '')):
            return False
    current = str(manifest.get('scheduler_job_id') or '')
    if record.get('action') in {'continue', 'tune_continue'}:
        expected = str(record.get('result_job_id') or evidence.get('job_id') or '')
        current_attempt = _current_scheduler_attempt(manifest)
        record_transaction = str(record.get('transaction_id') or '')
        evidence_transaction = str(
            evidence.get('operation_transaction_id') or '')
        authority = manifest.get('execution_authority')
        authority_transaction = (
            str(authority.get('transaction_id') or '')
            if isinstance(authority, dict) else '')
        authority_incar = (
            _valid_sha256(authority.get('current_incar_sha256'))
            if isinstance(authority, dict) else '')
        evidence_incar = _valid_sha256(evidence.get('incar_sha256'))
        authority_job_id = (
            str(authority.get('scheduler_job_id') or '')
            if isinstance(authority, dict) else '')
        current_attempt_token = str(
            (current_attempt or {}).get('attempt_token') or '')
        evidence_attempt_token = str(evidence.get('attempt_token') or '')
        if (not current_attempt or current_attempt is not evidence
                or not record_transaction
                or record_transaction != evidence_transaction
                or authority_transaction != record_transaction
                or authority_job_id != current
                or not authority_incar
                or authority_incar != evidence_incar
                or not current_attempt_token
                or current_attempt_token != evidence_attempt_token):
            return False
        request = record.get('request')
        binding = manifest.get('cluster_binding')
        cluster_fingerprint = (
            str(binding.get('fingerprint') or '')
            if isinstance(binding, dict) else '')
        expected_profile = ''
        expected_remote = ''
        if isinstance(request, dict):
            expected_profile = str(request.get('profile_fingerprint') or '')
            expected_remote = str(request.get('remote_dir_sha256') or '')
        expected_profile = expected_profile or str(
            evidence.get('profile_fingerprint') or '')
        expected_remote = expected_remote or str(
            evidence.get('remote_dir_sha256') or '')
        actual_remote = hashlib.sha256(
            str(manifest.get('remote_dir') or '').encode('utf-8')).hexdigest()
        if (not expected_profile or cluster_fingerprint != expected_profile
                or not expected_remote or actual_remote != expected_remote):
            return False
    else:
        expected = str(record.get('source_job_id') or
                       evidence.get('target_job_id') or '')
    return bool(expected and current == expected)


def _job_action_unknown(record: dict, *, message: str | None = None):
    action = str(record.get('action') or '')
    label = {
        'continue': '续算',
        'tune_continue': '改参续算',
        'cancel': '取消',
    }.get(action, '远端操作')
    status = str(record.get('status') or 'unknown_remote_outcome')
    job_id = str(record.get('result_job_id') or record.get('source_job_id') or '')
    raise UnknownRemoteJobOperation(
        message or f'上次{label}的远端结果尚未安全落盘；已禁止自动重试，请人工核对调度器。',
        action=action, recovery_status=status, scheduler_job_id=job_id)


def _reconcile_job_action(job_dir, manifest: dict, action: str,
                          idempotency_key: str, *,
                          intent_sha256: str = '') -> dict | None:
    """Replay one completed key or reject every unresolved remote outcome."""
    journal = _read_job_action_journal(job_dir)
    changed = False
    for record in journal['operations']:
        if record.get('status') not in _JOB_ACTION_UNRESOLVED:
            continue
        evidence = _job_action_attempt(
            manifest, action=str(record.get('action') or ''),
            transaction_id=str(record.get('transaction_id') or ''))
        if (record.get('status') == 'remote_accepted'
                and _job_action_generation_matches(manifest, record, evidence)):
            # The manifest replacement is authoritative.  A crash while marking
            # the journal terminal can be reconciled without another scheduler call.
            record['status'] = 'succeeded'
            record['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            changed = True
            continue
        _job_action_unknown(record)
    if changed:
        try:
            _write_job_action_journal(job_dir, journal)
        except OSError:
            # The manifest already proves the exact transaction.  Retaining the
            # remote_accepted record remains safe and will be reconciled again.
            pass

    if idempotency_key:
        matches = [record for record in journal['operations']
                   if str(record.get('idempotency_key') or '') == idempotency_key]
        if matches:
            record = matches[-1]
            if record.get('action') != action:
                raise ValueError('同一作业操作请求标识不能用于不同的远端动作')
            stored_intent = str(record.get('intent_sha256') or '')
            if intent_sha256 and stored_intent != intent_sha256:
                subject = ('改参续算内容' if action == 'tune_continue'
                           else '续算授权内容')
                raise ValueError(f'同一作业操作请求标识不能用于不同的{subject}')
            status = record.get('status')
            if status == 'failed':
                raise ReplayedJobOperationFailure(
                    str(record.get('message') or '远端作业操作失败'))
            if status == 'succeeded':
                evidence = _job_action_attempt(
                    manifest, action=action,
                    transaction_id=str(record.get('transaction_id') or ''))
                if not _job_action_generation_matches(manifest, record, evidence):
                    _job_action_unknown(
                        record,
                        message='远端操作 journal 与当前作业代次不一致；'
                        '已禁止用旧请求重放，请刷新后重新确认。')
                return _job_action_replay(manifest, action, evidence)

        # A successfully committed manifest remains sufficient if a user or
        # maintenance tool removed only the bounded terminal journal history.
        evidence = _job_action_attempt(
            manifest, action=action, idempotency_key=idempotency_key)
        if evidence:
            evidence_intent = str(evidence.get('operation_intent_sha256') or '')
            if intent_sha256 and evidence_intent != intent_sha256:
                subject = ('改参续算内容' if action == 'tune_continue'
                           else '续算授权内容')
                raise ValueError(f'同一作业操作请求标识不能用于不同的{subject}')
            fallback_record = {
                'action': action,
                'transaction_id': str(
                    evidence.get('operation_transaction_id') or ''),
                'request_sha256': str(
                    evidence.get('operation_request_sha256') or ''),
                'intent_sha256': str(
                    evidence.get('operation_intent_sha256') or ''),
                'source_job_id': str(
                    evidence.get('prev_job_id') if action in {
                        'continue', 'tune_continue'}
                    else evidence.get('target_job_id') or ''),
                'result_job_id': str(
                    evidence.get('job_id') if action in {
                        'continue', 'tune_continue'}
                    else evidence.get('target_job_id') or ''),
            }
            if _job_action_generation_matches(manifest, fallback_record, evidence):
                return _job_action_replay(manifest, action, evidence)
            _job_action_unknown(
                fallback_record,
                message='作业操作请求属于旧的调度器代次；请刷新目标列表后重新确认。')
    return None


def _start_job_action(job_dir, action: str, idempotency_key: str,
                      source_job_id: str, *, request: dict | None = None,
                      request_sha256: str = '', intent_sha256: str = '') -> dict:
    journal = _read_job_action_journal(job_dir)
    # Reconciliation should have rejected every unresolved record.  Re-check
    # before writing so this helper is safe if used by a future caller directly.
    unresolved = [record for record in journal['operations']
                  if record.get('status') in _JOB_ACTION_UNRESOLVED]
    if unresolved:
        _job_action_unknown(unresolved[-1])
    terminal = [record for record in journal['operations']
                if record.get('status') in {'succeeded', 'failed'}]
    journal['operations'] = terminal[-63:]
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    record = {
        'transaction_id': os.urandom(16).hex(),
        'action': action,
        'status': 'prepared',
        'idempotency_key': idempotency_key or None,
        'source_job_id': str(source_job_id or ''),
        'result_job_id': None,
        'created_at': now,
        'updated_at': now,
        'message': None,
    }
    if request is not None:
        record['request'] = request
        record['request_sha256'] = request_sha256
        record['intent_sha256'] = intent_sha256
    journal['operations'].append(record)
    _write_job_action_journal(job_dir, journal)
    return record


def _update_job_action(job_dir, record: dict, status: str, **fields) -> None:
    journal = _read_job_action_journal(job_dir)
    transaction_id = str(record.get('transaction_id') or '')
    stored = next((item for item in journal['operations']
                   if item.get('transaction_id') == transaction_id), None)
    if stored is None:
        raise OSError('作业远端操作 journal 丢失当前事务')
    stored.update(fields)
    stored['status'] = status
    stored['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    if status in {'succeeded', 'failed'}:
        stored['finished_at'] = stored['updated_at']
    _write_job_action_journal(job_dir, journal)
    record.update(stored)


def _mark_job_action_unknown(job_dir, record: dict, message: str) -> None:
    try:
        _update_job_action(
            job_dir, record, 'unknown_remote_outcome', message=str(message or ''))
    except Exception:  # noqa: BLE001 - the fsynced prepared record is the safety gate
        pass


def _submission_attempt_for_key(manifest: dict | None, idempotency_key: str) -> dict | None:
    if not idempotency_key or not isinstance(manifest, dict):
        return None
    attempts = manifest.get('attempts')
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if (isinstance(attempt, dict)
                and str(attempt.get('idempotency_key') or '') == idempotency_key
                and str(attempt.get('job_id') or '')):
            return attempt
    return None


def _reconcile_submission_recovery(job_dir, manifest: dict | None,
                                   idempotency_key: str) -> dict | None:
    """Return an authoritative replay or fail closed on an uncertain submit."""
    recovery = _read_submission_recovery(job_dir)
    if recovery is None:
        attempt = _submission_attempt_for_key(manifest, idempotency_key)
        if attempt and str((manifest or {}).get('scheduler_job_id') or '') == \
                str(attempt.get('job_id') or ''):
            replay = dict(manifest)
            replay['_submission_replayed'] = True
            return replay
        return None

    if recovery.get('status') == 'preparing':
        # The scheduler is unreachable in this phase, but the remote leaf may
        # already contain a partial upload.  Silent retry would reuse that
        # unverified directory, so require explicit operator cleanup/recovery.
        raise UnknownRemoteSubmission(
            '上次提交停在远端目录准备阶段；调度器尚未确认接收，但远端可能有半成品。'
            '请人工核对并清理后再显式恢复。',
            recovery_status='preparing')

    recovery_job_id = str(recovery.get('scheduler_job_id') or '')
    current_job_id = str((manifest or {}).get('scheduler_job_id') or '')
    attempts = (manifest or {}).get('attempts') or []
    matching_attempt = next((
        attempt for attempt in reversed(attempts)
        if isinstance(attempt, dict)
        and str(attempt.get('job_id') or '') == recovery_job_id
    ), None)
    recorded = matching_attempt is not None
    recovered_incar_sha256 = _valid_sha256(recovery.get('incar_sha256'))
    authority = (manifest or {}).get('execution_authority') or {}
    authority_matches = (
        not recovered_incar_sha256
        or (isinstance(authority, dict)
            and str(authority.get('scheduler_job_id') or '') == recovery_job_id
            and _valid_sha256(authority.get('current_incar_sha256'))
            == recovered_incar_sha256)
    )
    recovered_neb_hashes = recovery.get('neb_image_poscar_sha256') or {}
    manifest_neb_hashes = (
        ((manifest or {}).get('inputs') or {}).get(
            'image_poscar_sha256') or {})
    neb_authority_matches = (
        not recovered_neb_hashes
        or (recovered_neb_hashes == manifest_neb_hashes
            and isinstance(authority, dict)
            and authority.get('neb_image_poscar_sha256')
            == recovered_neb_hashes
            and (matching_attempt or {}).get('neb_image_poscar_sha256')
            == recovered_neb_hashes)
    )
    if (recovery.get('status') == 'remote_accepted' and recovery_job_id
            and current_job_id == recovery_job_id and recorded
            and authority_matches and neb_authority_matches):
        # Crash/cleanup failure after the manifest replacement: the canonical
        # manifest already carries the exact remote identity, so cleanup and
        # replay are safe without another scheduler call.
        try:
            _clear_submission_recovery(job_dir)
        except OSError:
            # A stale journal is safe once the same remote identity is durable
            # in job.yaml; keep returning the authoritative current state.
            pass
        replay = dict(manifest)
        replay['_submission_replayed'] = True
        return replay

    raise UnknownRemoteSubmission(
        '上次提交的远端结果尚未安全写入本地；已禁止自动重试，请人工核对调度器。',
        recovery_status=str(recovery.get('status') or 'unknown_remote_submission'),
        scheduler_job_id=recovery_job_id)


def _serialized_job_argument(position: int, action: str, *, create_dir=False):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            job_dir = kwargs.get('job_dir')
            if job_dir is None and len(args) > position:
                job_dir = args[position]
            if job_dir is None:
                raise TypeError('缺少 job_dir')
            with job_operation(job_dir, action, create_dir=create_dir):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def _job_engine(m: dict | None) -> str:
    """Return the manifest engine; old manifests remain VASP-compatible.

    ``inputs.engine`` was introduced by quick submit.  Older generated VASP
    jobs do not carry it, so an absent/blank value intentionally means VASP.
    A present but unsupported value is *not* coerced to VASP.
    """
    inputs = (m or {}).get('inputs') or {}
    if not isinstance(inputs, dict):
        return 'vasp'
    return str(inputs.get('engine') or 'vasp').strip().lower() or 'vasp'


_PROFILE_BINDING_FIELDS = (
    'name', 'hostname', 'port', 'username', 'scheduler', 'scheduler_bin',
    'remote_root', 'use_jump', 'jump_host', 'jump_port', 'jump_user',
)


def _normalise_binding_value(field: str, value):
    if field in {'port', 'jump_port'}:
        try:
            return int(value or 22)
        except (TypeError, ValueError):
            return 22
    if field == 'use_jump':
        return bool(value)
    text = str(value or '').strip()
    if field in {'hostname', 'jump_host', 'scheduler'}:
        return text.lower()
    if field == 'remote_root' and text:
        return posixpath.normpath(text)
    return text


def profile_binding(profile) -> dict:
    """Return the immutable remote endpoint identity stored with every job.

    A display name alone is not an endpoint identity: users may edit a saved
    profile in-place and keep the same name.  The digest deliberately excludes
    resource choices (queue/nodes/walltime) but includes every field that can
    redirect a remote action to a different account, scheduler, jump host or
    directory namespace.
    """
    endpoint = {
        field: _normalise_binding_value(field, getattr(profile, field, None))
        for field in _PROFILE_BINDING_FIELDS
    }
    encoded = json.dumps(endpoint, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return {
        'schema': 1,
        'fingerprint': hashlib.sha256(encoded).hexdigest(),
        'endpoint': endpoint,
    }


def assert_profile_binding(profile, job_dir: str, action: str,
                           manifest: dict | None = None, *,
                           allow_legacy_read: bool = False) -> dict:
    """Fail closed when a remote action targets a job owned by another server.

    Scheduler job ids and remote paths are only meaningful inside the cluster
    recorded in ``job.yaml``.  Letting the UI-selected profile override that
    identity could download unrelated files, mutate a foreign work directory,
    or even cancel an unrelated same-numbered job on another scheduler.
    """
    m = manifest if manifest is not None else manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError(f'{action}失败：作业目录缺可读 job.yaml')
    expected = str(getattr(profile, 'name', '') or '').strip()
    actual = str(m.get('cluster') or '').strip()
    if not actual:
        raise ValueError(f'{action}失败：作业尚未绑定服务器，不能使用「{expected}」执行远程操作')
    if not expected or actual != expected:
        raise ValueError(
            f'{action}失败：作业属于服务器「{actual}」，当前选择的是「{expected}」；'
            '为防止操作错误服务器，已阻止本次操作')
    stored = m.get('cluster_binding')
    stored_fingerprint = (str(stored.get('fingerprint') or '').strip()
                          if isinstance(stored, dict) else '')
    if not stored_fingerprint:
        if allow_legacy_read and stored is None:
            return m
        raise ValueError(
            f'{action}失败：旧作业缺服务器端点绑定，不能仅凭同名 profile 执行远程操作；'
            '请先通过显式认领/重新绑定流程核验服务器后再操作')
    current = profile_binding(profile)
    if stored_fingerprint != current['fingerprint']:
        raise ValueError(
            f'{action}失败：服务器「{expected}」的连接端点已与作业提交时不同'
            '（主机/端口/用户/调度器/跳板机/远程根目录之一已改变）；'
            '为防止误操作同名的另一台服务器，已阻止本次操作')
    return m


def _environment_identity(value: dict) -> tuple[str, str, str, str]:
    evidence = value.get('evidence') or {}
    return (
        str(value.get('engine') or '').strip().lower(),
        str(value.get('vasp_version') or '').strip(),
        str(value.get('build_identity') or '').strip(),
        str(evidence.get('sha256') or '').strip().lower(),
    )


def _planned_execution_environment(profile) -> dict | None:
    """Validate the profile's separate scientific build attestation.

    Endpoint identity remains in ``cluster_binding`` and is deliberately not
    folded into the scientific build identity.
    """
    return from_cluster_profile(profile)


def _execution_environment_issues(profile, manifest: dict | None) -> list[str]:
    if _job_engine(manifest) != 'vasp':
        return []
    try:
        planned = _planned_execution_environment(profile)
    except ValueError as exc:
        return [f'集群 VASP 版本/编译环境证据不完整：{exc}']
    inputs = (manifest or {}).get('inputs') or {}
    current = inputs.get('execution_environment') if isinstance(inputs, dict) else None
    if current is None:
        return []
    try:
        authoritative = validate_execution_environment(current)
    except ValueError as exc:
        return [f'job.yaml 的 VASP 执行环境证据无效：{exc}']
    if planned is None:
        return [
            'job.yaml 已绑定 VASP 版本/编译身份，但当前集群 profile 没有可核验的'
            '对应环境证据；为防止方法漂移，已拒绝提交']
    if _environment_identity(authoritative) != _environment_identity(planned):
        return [
            'job.yaml 已绑定的 VASP 版本/编译身份与当前集群 profile 证据冲突；'
            '为防止方法漂移，已拒绝提交']
    return []


def _bind_execution_environment_locked(job_dir: str, profile,
                                       manifest: dict | None = None) -> dict:
    """Bind trusted profile evidence while the caller owns the job lock."""
    item = manifest if manifest is not None else manifest_mod.load_manifest(job_dir)
    if item is None:
        raise ValueError('作业目录缺可读 job.yaml，无法绑定执行环境')
    if _job_engine(item) != 'vasp':
        return item
    issues = _execution_environment_issues(profile, item)
    if issues:
        raise ValueError('；'.join(issues))
    planned = _planned_execution_environment(profile)
    if planned is None:
        return item
    inputs = item.setdefault('inputs', {})
    current = inputs.get('execution_environment')
    if current is not None:
        # Validation and equality were checked above.  Preserve the original
        # authoritative record byte-for-byte on an idempotent replay.
        return item
    inputs['execution_environment'] = planned
    manifest_mod.save_manifest(job_dir, item)
    return item


def bind_execution_environment(job_dir: str, profile) -> dict:
    """Atomically bind a profile's VASP build evidence before reuse advice.

    This public entry point owns the per-job lock.  ``submit_job`` already owns
    that lock and therefore calls the private ``*_locked`` helper instead.
    Rebinding the same identity is idempotent; conflicting authority fails
    closed without changing ``job.yaml``.
    """
    with job_operation(job_dir, '绑定 VASP 执行环境'):
        return _bind_execution_environment_locked(job_dir, profile)


def current_attempt_token(m: dict | None) -> str:
    """Return a stable token for the manifest's current scheduler attempt.

    New attempts persist the token.  Legacy and continuation attempts derive
    the same value deterministically, allowing final fetch evidence to bind to
    one scheduler job without rewriting old audit history.
    """
    m = m or {}
    job_id = str(m.get('scheduler_job_id') or '')
    attempts = list(m.get('attempts') or [])
    attempt = next((item for item in reversed(attempts)
                    if isinstance(item, dict)
                    and str(item.get('job_id') or '') == job_id), {})
    persisted = str((attempt or {}).get('attempt_token') or '').strip()
    if persisted:
        return persisted
    binding = m.get('cluster_binding') or {}
    payload = {
        'job_id': job_id,
        'remote_dir': str(m.get('remote_dir') or ''),
        'cluster': str(m.get('cluster') or ''),
        'cluster_fingerprint': (str(binding.get('fingerprint') or '')
                                if isinstance(binding, dict) else ''),
        'attempt_n': (attempt or {}).get('n'),
        'attempt_at': str((attempt or {}).get('at') or ''),
        'attempt_action': str((attempt or {}).get('action') or
                              (attempt or {}).get('result') or ''),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _engine_command_template(profile, engine: str) -> str:
    """Select one engine's configured command without unsafe cross-fallback.

    ``vasp_cmd`` remains the fallback for VASP only.  Non-VASP engines must
    have their own entry in ``engine_commands`` and can therefore never run a
    stale ``vasp_std`` line by accident.
    """
    raw = getattr(profile, 'engine_commands', {}) or {}
    commands = {}
    if isinstance(raw, dict):
        commands = {str(key).strip().lower(): str(value or '').strip()
                    for key, value in raw.items()}
    command = commands.get(engine, '')
    if engine == 'vasp' and not command:
        command = str(getattr(profile, 'vasp_cmd', '') or '').strip()
    return command


def _missing_command_message(profile, engine: str) -> str:
    label = _ENGINE_LABELS.get(engine, engine or '未知引擎')
    if engine == 'vasp':
        return (
            f'集群「{getattr(profile, "name", "")}」未配置 VASP 执行命令；'
            '请填写 vasp_cmd（旧配置继续兼容）或 engine_commands.vasp。')
    example = _ENGINE_COMMAND_EXAMPLES.get(engine, '<完整执行命令>')
    return (
        f'集群「{getattr(profile, "name", "")}」未配置 {label} 执行命令；'
        f'请在 engine_commands.{engine} 中填写，例如「{example}」。'
        '为防止误算，系统不会用 VASP 命令代替。')


def _declared_input_files(job_dir: str, m: dict | None) -> tuple[list[str], list[str]]:
    """Resolve and validate the files uploaded for one manifest.

    VASP consumes the shared task/INCAR-aware scientific closure; NEB image
    paths are the only supported nested names.  Other engines consume their
    explicit ``inputs.files`` closure.  Escapes, links and reserved journal
    files are rejected before any SSH operation.
    """
    engine = _job_engine(m)
    if m is None:
        # Preserve the established diagnostics for a directory with no
        # manifest; preflight will separately report the missing job.yaml.
        return [], ['作业目录缺 INCAR/POSCAR/KPOINTS/POTCAR(先在生成页产出四件套)']
    closure = resolve_input_closure(job_dir, m)
    names = list(closure.get('requirements') or {})
    reasons = closure.get('requirements') or {}
    errs: list[str] = []
    for name in closure.get('missing') or []:
        reason = str(reasons.get(name) or '清单声明/闭包校验')
        if str(name).startswith(('invalid input declaration:',
                                 'invalid required input:')):
            errs.append(f'作业清单输入文件名非法：{name}')
            continue
        if engine == 'vasp':
            errs.append(f'作业目录缺或无法安全读取 {name}（{reason} 的必需输入）')
        else:
            errs.append(
                f'{_ENGINE_LABELS.get(engine, engine)} 输入闭包不完整：{name}（{reason}）')
    names = [name for name in names if name in (closure.get('files') or {})]
    if not names and not errs:
        errs.append('作业清单没有可上传的输入文件')
    return names, errs


def _local_vasp_icharg(job_dir: str) -> int | None:
    """读取本地 INCAR 的 ICHARG；提交/续算文件契约只以实际输入为准。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8',
                  errors='replace') as handle:
            value = parse_incar(handle.read()).get('ICHARG')
        return int(value) if not isinstance(value, bool) else None
    except (OSError, TypeError, ValueError):
        return None


def _primary_input(engine: str, names: list[str]) -> str:
    """Choose the main file used by ``{input}``/``{stem}`` placeholders."""
    try:
        return get_run_contract(engine).primary_input(names)
    except ValueError:
        return names[0] if names else ''


def _engine_task(m: dict | None, job_dir: str | None = None) -> str | None:
    """Resolve a non-VASP task from manifest evidence, then its local input."""
    m = m or {}
    inputs = m.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    raw = (inputs.get('gaussian_task') or inputs.get('task')
           or (m.get('task_type') if m.get('task_type') != 'quick' else None))
    if raw:
        return str(raw).strip().lower()
    if job_dir:
        try:
            from vcstudio.engines import get_backend
            return get_backend(_job_engine(m)).infer_task(job_dir)
        except (OSError, TypeError, ValueError):
            return None
    return None


def _non_vasp_input_contract_issues(engine: str, job_dir: str, names: list[str]) -> list[str]:
    """Run the engine checker before any SSH action."""
    try:
        from vcstudio.engines import get_backend
        contract = get_run_contract(engine)
        primary = contract.primary_input(names)
        issues = []
        if not primary or not primary.lower().endswith(contract.input_suffixes):
            issues.append(
                f'{_ENGINE_LABELS.get(engine, engine)} 清单没有受支持的主输入'
                f'({"/".join(contract.input_suffixes)})。')
        issues.extend(str(item) for item in get_backend(engine).check_inputs(job_dir))
        return issues
    except (OSError, TypeError, ValueError) as exc:
        return [f'{_ENGINE_LABELS.get(engine, engine)} 输入检查失败：{exc}']


def _render_engine_command(profile, engine: str, names: list[str],
                           job_name: str) -> str:
    """Render the small, documented placeholder set in an engine command.

    Unknown braces (including shell ``${VAR}``) are deliberately untouched.
    Filenames are shell-quoted before insertion.
    """
    command = _engine_command_template(profile, engine)
    if not command:
        return ''
    primary = _primary_input(engine, names)
    if ('{input}' in command or '{stem}' in command) and not primary:
        raise ValueError(
            f'{_ENGINE_LABELS.get(engine, engine)} 执行命令使用了 '
            '{input}/{stem}，但 job.yaml 没有可用的 inputs.files。')
    try:
        ppn = int(getattr(profile, 'ppn', 0) or 0)
    except (TypeError, ValueError):
        ppn = 0
    try:
        nodes = int(getattr(profile, 'nodes', 1) or 1)
    except (TypeError, ValueError):
        nodes = 1
    values = {
        'engine': engine,
        'input': shlex.quote(primary) if primary else '',
        'stem': shlex.quote(os.path.splitext(primary)[0]) if primary else '',
        'job_name': shlex.quote(job_name),
        'nodes': str(nodes),
        # 未配置核数时先保留 token；preflight/build_script_text 只在
        # 命令真正被脚本使用时报错。这样不会误伤自带运行行的旧 VASP 模板。
        'ppn': str(ppn) if ppn > 0 else '{ppn}',
        'cores': str(nodes * ppn) if ppn > 0 else '{cores}',
    }
    out = command
    for key, value in values.items():
        out = out.replace('{' + key + '}', value)
    return out


# ── NEB 多 image 作业辅助(一目录多 image 子目录,根共享 INCAR/POTCAR/KPOINTS) ──────
def _is_neb(m: dict | None) -> bool:
    """manifest task_type=='neb' → 走 NEB 专用上传/刷新/取证路径。"""
    return bool(m) and str(m.get('task_type') or '') == 'neb'


def _neb_n_images(m: dict | None) -> int | None:
    """中间 image 数:优先 manifest inputs.n_images。"""
    if not m:
        return None
    n = (m.get('inputs') or {}).get('n_images')
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def _neb_local_frames(job_dir: str) -> list:
    """本地 image 子目录名(纯数字升序):00,01,...,N+1。"""
    try:
        names = [d for d in os.listdir(job_dir)
                 if os.path.isdir(os.path.join(job_dir, d)) and _NEB_FRAME_RE.match(d)]
    except OSError:
        return []
    return sorted(names, key=lambda s: int(s))


def _neb_expected_frames(m: dict | None) -> list[str]:
    """Return the one canonical ``00..N+1`` frame set declared by the manifest."""
    n_images = _neb_n_images(m)
    if n_images is None or n_images < 1 or n_images > 998:
        return []
    return [f'{index:02d}' for index in range(n_images + 2)]


def _read_incar_images(job_dir: str) -> int | None:
    """本地根 INCAR 的 IMAGES 值(缺/读不到 → None)。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8', errors='replace') as f:
            v = parse_incar(f.read()).get('IMAGES')
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    except (OSError, ValueError, TypeError):
        return None


def _neb_input_check(job_dir: str, m: dict) -> list:
    """NEB 提交前输入检查:根共享文件 + 各 image 子目录 POSCAR + IMAGES 与目录数一致性。"""
    errs = []
    for f in _NEB_ROOT_FILES:
        if not os.path.isfile(os.path.join(job_dir, f)):
            errs.append(f'NEB 作业根目录缺 {f}(先在生成页产出 NEB 目录树)')
    frames = _neb_local_frames(job_dir)
    expected_frames = _neb_expected_frames(m)
    if len(frames) < 3:
        errs.append(f'NEB 作业 image 子目录不足(找到 {len(frames)} 个,至少需 00/01/02)')
    for fr in frames:
        if not os.path.isfile(os.path.join(job_dir, fr, 'POSCAR')):
            errs.append(f'NEB image 子目录 {fr} 缺 POSCAR')
    n = _neb_n_images(m)
    if not expected_frames:
        errs.append('NEB manifest n_images 必须是 1..998 的整数')
    elif frames != expected_frames:
        errs.append(
            f'NEB image 子目录必须严格为 00..{expected_frames[-1]}'
            f'（manifest n_images={n}），当前为 {", ".join(frames) or "空"}')
    recorded = (m.get('inputs') or {}).get('image_poscar_sha256')
    if not isinstance(recorded, dict):
        errs.append('NEB 清单缺 image_poscar_sha256；请重新生成目录树后再提交')
    elif set(recorded) != set(frames):
        errs.append(
            'NEB 清单的 image POSCAR 摘要集合与数字 image 目录不一致；'
            '请重新生成目录树')
    else:
        for frame in frames:
            expected = str(recorded.get(frame) or '').strip().lower()
            if re.fullmatch(r'[0-9a-f]{64}', expected) is None:
                errs.append(f'NEB image {frame}/POSCAR 的清单 SHA256 无效')
                continue
            try:
                actual = manifest_mod.sha256_file(
                    os.path.join(job_dir, frame, 'POSCAR')).lower()
            except OSError as exc:
                errs.append(f'NEB image {frame}/POSCAR 无法核对摘要：{exc}')
                continue
            if actual != expected:
                errs.append(
                    f'NEB image {frame}/POSCAR 在生成后已变化；'
                    '拒绝提交不再匹配清单的反应路径')
    return errs


def _freeze_neb_image_authority(job_dir: str, m: dict) -> dict[str, str]:
    """Re-open and freeze the exact canonical local NEB image set."""
    errors = _neb_input_check(job_dir, m)
    if errors:
        raise ValueError('；'.join(errors))
    recorded = (m.get('inputs') or {}).get('image_poscar_sha256') or {}
    return {
        frame: str(recorded[frame]).strip().lower()
        for frame in _neb_expected_frames(m)
    }


def _assert_neb_image_authority(job_dir: str,
                                authority: dict[str, str]) -> None:
    """Ensure no image was added, removed, or changed while upload was running."""
    frames = _neb_local_frames(job_dir)
    if frames != list(authority):
        raise ValueError(
            'NEB image 集合在提交准备期间发生变化；已在联系调度器前停止')
    for frame, expected in authority.items():
        actual = manifest_mod.sha256_file(
            os.path.join(job_dir, frame, 'POSCAR')).lower()
        if actual != expected:
            raise ValueError(
                f'NEB image {frame}/POSCAR 在上传期间发生变化；'
                '已在联系调度器前停止')


def _upload_neb_tree(client, sftp, job_dir: str, remote_dir: str,
                     image_authority: dict[str, str],
                     manifest: dict | None = None) -> int:
    """Upload the task-aware closure, bounded by the frozen NEB frame set."""
    item = manifest if manifest is not None else manifest_mod.load_manifest(job_dir)
    names, issues = _declared_input_files(job_dir, item)
    if issues:
        raise ValueError('；'.join(issues))
    required_poscars = {f'{frame}/POSCAR' for frame in image_authority}
    if not required_poscars.issubset(names):
        raise ValueError('NEB 输入闭包缺少冻结 image POSCAR；拒绝上传')
    for name in names:
        parts = name.split('/')
        if len(parts) == 1:
            continue
        if (len(parts) != 2 or parts[0] not in image_authority
                or parts[1] not in {'POSCAR', 'CHGCAR', 'WAVECAR'}):
            raise ValueError(f'NEB 输入闭包含未授权路径：{name}')
    count = 0
    made_dirs: set[str] = set()
    for name in names:
        relative_dir = posixpath.dirname(name)
        if relative_dir and relative_dir not in made_dirs:
            target_dir = posixpath.join(remote_dir, relative_dir)
            run_cmd(client, f'mkdir -p {shlex.quote(target_dir)}', check=True)
            made_dirs.add(relative_dir)
        local = os.path.join(job_dir, *name.split('/'))
        if name in required_poscars:
            frame = name.split('/', 1)[0]
            if manifest_mod.sha256_file(local).lower() != image_authority[frame]:
                raise ValueError(
                    f'NEB image {frame}/POSCAR 在上传前发生变化；拒绝上传')
        sftp.put(local, posixpath.join(remote_dir, name))
        count += 1
    return count


def _remote_neb_frame_set_guard(frames) -> str:
    """POSIX-shell guard requiring exactly the frozen numeric frame directories."""
    canonical = [str(frame) for frame in frames]
    if (not canonical or len(set(canonical)) != len(canonical)
            or any(re.fullmatch(r'[0-9]{2,4}', frame) is None
                   for frame in canonical)):
        raise ValueError('远端 NEB image 集合绑定无效')
    expected = ' '.join(sorted(canonical))
    return (
        "vcs_frames=''; for vcs_dir in [0-9]*; do "
        '[ -d "$vcs_dir" ] || continue; '
        'case "$vcs_dir" in *[!0-9]*) continue ;; esac; '
        'vcs_frames="${vcs_frames}${vcs_frames:+ }${vcs_dir}"; done; '
        f'[ "$vcs_frames" = {shlex.quote(expected)} ]')


def run_cmd(client, cmd: str, timeout: int = 30, check: bool = False):
    """exec_command 薄封装 → (stdout_text, stderr_text)。

    check=True 时退出码非零抛 RuntimeError(先读尽输出再取退出码,防 paramiko 死锁)。
    """
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    o = out.read().decode('utf-8', errors='replace')
    e = err.read().decode('utf-8', errors='replace')
    if check:
        status = out.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(f'远程命令失败(退出码 {status}):{cmd};{e.strip()[:200]}')
    return o, e


# ── preflight(全部本地检查,不联网) ─────────────────────────────────────────
def preflight(profile, job_dir: str) -> list:
    """提交前检查,返回错误文案列表(空=可以出手)。"""
    errs = []
    m0 = manifest_mod.load_manifest(job_dir)
    engine = _job_engine(m0)
    # 通用提交只能消费全新的 CREATED 清单。已有远端身份或任何后续状态都必须走
    # 查询/续算/显式重提流程；否则会覆盖正在运行的远端目录并产生孤儿双作业。
    if m0 is not None:
        if str(m0.get('state') or '') != 'CREATED':
            errs.append(
                f'作业状态为 {m0.get("state") or "?"}，仅全新的 CREATED 作业可提交；'
                '已有作业请查询状态或使用明确的续算流程')
        occupied = [key for key in ('cluster', 'remote_dir', 'scheduler_job_id')
                    if m0.get(key) not in (None, '')]
        if occupied:
            errs.append(
                '作业已带远端身份(' + '、'.join(occupied) + ')，拒绝重复提交；'
                '如需重算请新建作业目录或使用续算流程')
        attempts = m0.get('attempts')
        if isinstance(attempts, list) and attempts:
            errs.append(
                '作业已有提交尝试记录，不能按全新 CREATED 作业再次提交；'
                '请核对调度器或使用明确的恢复/续算流程')
    if engine not in _SUPPORTED_ENGINES:
        errs.append(
            f'不支持的计算引擎:{engine!r}；可选 '
            + ' / '.join(_ENGINE_LABELS[key] for key in _SUPPORTED_ENGINES))
    if _is_neb(m0):
        # NEB 多 image:POSCAR 在各 image 子目录,根只放共享 INCAR/POTCAR/KPOINTS
        if engine != 'vasp':
            errs.append('NEB 目录只支持 VASP；请修正 job.yaml 的 inputs.engine')
        errs += _neb_input_check(job_dir, m0)
    elif engine in _SUPPORTED_ENGINES:
        _names, input_errs = _declared_input_files(job_dir, m0)
        errs += input_errs
        if engine != 'vasp' and not input_errs:
            errs += _non_vasp_input_contract_issues(engine, job_dir, _names)
    if m0 is None:
        errs.append('作业目录缺 job.yaml(旧目录可重新生成一次以补台账)')
    else:
        inputs0 = m0.get('inputs') or {}
        if not isinstance(inputs0, dict):
            inputs0 = {}
        namespace = str(inputs0.get('remote_namespace') or '')
        if namespace and not _REMOTE_NAMESPACE_RE.fullmatch(namespace):
            errs.append('远程命名空间非法：只允许单段字母/数字/._-')
        # POTCAR TITEL 闸(原版提交前防线):截断/拼错的 POTCAR 会给出"收敛但静默错"
        # 的能量——TITEL 段数必须等于 POSCAR 物种数,不等拒绝提交
        if engine == 'vasp':
            errs += _execution_environment_issues(profile, m0)
            errs += _managed_vasp_input_hash_gate(job_dir, m0)
            errs += _potcar_gate(job_dir, m0)
    if not profile.remote_root:
        errs.append('集群配置未填远程工作目录 remote_root')
    elif not str(profile.remote_root).startswith('/'):
        errs.append('remote_root 需为绝对路径(以 / 开头)')
    try:
        get_dialect(profile.scheduler)
    except ValueError as e:
        errs.append(str(e))
    try:
        profile_ppn = int(getattr(profile, 'ppn', 0) or 0)
    except (TypeError, ValueError):
        profile_ppn = 0
    mode = getattr(profile, 'script_mode', 'auto')
    if mode == 'template':
        tp = getattr(profile, 'template_path', '')
        if not tp or not os.path.isfile(tp):
            errs.append('模板模式但模板文件不存在;请在集群页重新选择')
        elif engine in _SUPPORTED_ENGINES:
            try:
                with open(tp, 'r', encoding='utf-8', errors='replace') as handle:
                    template_text = handle.read()
            except OSError as exc:
                errs.append(f'提交模板无法读取:{exc}')
                template_text = ''
            # 旧 VASP 模板常自带 vasp_std，继续允许。非 VASP 必须
            # 通过 {command} 明确接入对应引擎命令，避免误跑模板里的 VASP。
            if engine != 'vasp' and '{command}' not in template_text:
                errs.append(
                    f'{_ENGINE_LABELS[engine]} 模板模式必须在提交模板中放置 '
                    f'{{command}}；它会替换为 engine_commands.{engine}，'
                    '防止误跑 VASP。')
            if ('{command}' in template_text or engine != 'vasp') and not _engine_command_template(
                    profile, engine):
                errs.append(_missing_command_message(profile, engine))
            command_template = _engine_command_template(profile, engine)
            if (('{command}' in template_text or engine != 'vasp')
                    and command_template
                    and any(token in command_template for token in ('{cores}', '{ppn}'))
                    and profile_ppn <= 0):
                errs.append(
                    f'{_ENGINE_LABELS[engine]} 执行命令使用了核数占位符，'
                    '但集群配置的 ppn 未填写或无效')
    elif mode == 'auto':
        if not profile.queue:
            errs.append('自动脚本模式缺队列名')
        if profile_ppn <= 0:
            errs.append('自动脚本模式缺每节点核数 ppn')
        if engine in _SUPPORTED_ENGINES and not _engine_command_template(profile, engine):
            errs.append(_missing_command_message(profile, engine))
    else:
        errs.append(f'未知脚本模式: {mode!r}')
    return errs


def _managed_vasp_input_hash_gate(job_dir: str, manifest: dict) -> list[str]:
    """Verify managed VASP inputs against hashes captured at preparation.

    ``inputs.sha256`` was added after the original manifest schema shipped, so
    its complete absence remains a supported legacy case.  Once the field is
    present, however, every recorded managed input is immutable evidence: a
    malformed digest, an unreadable file, or a content mismatch must stop the
    submission before SSH is touched.  This prevents a prepared/reused project
    from silently uploading an INCAR that bypassed the method gate.
    """
    inputs = manifest.get('inputs') or {}
    if not isinstance(inputs, dict) or 'sha256' not in inputs:
        return []
    recorded = inputs.get('sha256')
    if not isinstance(recorded, dict):
        return [
            '作业清单 inputs.sha256 格式无效；'
            '请重新准备该作业后再提交']

    errors: list[str] = []
    authoritative_closure = recorded_closure(manifest)
    if authoritative_closure:
        current = resolve_input_closure(job_dir, manifest)
        if not closure_record_matches(authoritative_closure, current):
            prior_files = authoritative_closure.get('files') or {}
            now_files = current.get('files') or {}
            changed = sorted(
                name for name in set(prior_files) | set(now_files)
                if prior_files.get(name) != now_files.get(name))
            if changed:
                return [
                    f'受管科学输入 {name} 在准备后已变化或缺失，与 job.yaml 记录的 '
                    'SHA256 不一致；请重新准备该作业后再提交'
                    for name in changed
                ]
            return [
                '科学输入闭包与 job.yaml 准备时记录不一致（任务依赖集合或资源边界变化）；'
                '请重新准备该作业后再提交']
        if current.get('status') != 'complete':
            return [
                '科学输入闭包不完整：' + '、'.join(current.get('missing') or [])
                + '；请补齐依赖并重新准备作业']
        return []
    if len(recorded) > 256:
        return ['作业清单 inputs.sha256 条目过多；请重新准备该作业后再提交']
    for name in recorded:
        relative = str(name or '').replace('\\', '/')
        parts = relative.split('/')
        if (not relative or any(part in {'', '.', '..'} for part in parts)
                or len(parts) > 2):
            errors.append(f'作业清单输入哈希文件名非法：{name!r}')
            continue
        expected = str(recorded.get(name) or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', expected):
            errors.append(
                f'作业清单中 {name} 的 SHA256 无效；'
                '请重新准备该作业后再提交')
            continue
        path = os.path.join(job_dir, *relative.split('/'))
        if not os.path.isfile(path):
            errors.append(
                f'受管输入 {name} 不存在，无法核对准备时哈希；'
                '请重新准备该作业后再提交')
            continue
        try:
            actual = manifest_mod.sha256_file(path).lower()
        except OSError as exc:
            errors.append(
                f'受管输入 {name} 无法读取并核对哈希：{exc}；'
                '请重新准备该作业后再提交')
            continue
        if actual != expected:
            errors.append(
                f'受管输入 {name} 在准备后已变化，'
                '与 job.yaml 记录的 SHA256 不一致；'
                '请重新准备该作业后再提交')
    return errors


def _potcar_gate(job_dir: str, m: dict) -> list:
    """本地 POTCAR 的 TITEL 段数 == manifest 物种数,否则拒提交(读不到不硬拦)。"""
    inputs = m.get('inputs') or {}
    elements = (inputs.get('elements') or []) if isinstance(inputs, dict) else []
    if not elements:
        return []
    try:
        with open(os.path.join(job_dir, 'POTCAR'), 'r', encoding='utf-8',
                  errors='replace') as f:
            n_titel = f.read().count('TITEL')
    except OSError:
        return []                                       # 缺文件已有独立检查项
    if n_titel != len(elements):
        return [f'POTCAR 完整性检查失败:TITEL 段数 {n_titel} ≠ 物种数 {len(elements)}'
                f'({" ".join(elements)});文件疑被截断/手改,请重新生成后再提交']
    return []


def _remote_dir_for(profile, job_dir: str, manifest: dict | None = None) -> str:
    """Build a stable, collision-resistant remote directory for one profile.

    Reusing only the local basename lets two projects called ``clean_slab``
    overwrite each other across separate submissions.  The readable prefix is
    therefore followed by a deterministic digest.  An already persisted path
    for the same profile is a compatibility boundary and is never migrated.
    """
    item = manifest if manifest is not None else manifest_mod.load_manifest(job_dir)
    existing = str((item or {}).get('remote_dir') or '').strip()
    existing_cluster = str((item or {}).get('cluster') or '').strip()
    if existing and existing_cluster == str(profile.name):
        return existing

    dir_name = os.path.basename(os.path.normpath(job_dir))
    inputs = (item or {}).get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    namespace = str(inputs.get('remote_namespace') or '')
    if namespace and not _REMOTE_NAMESPACE_RE.fullmatch(namespace):
        raise ValueError('远程命名空间非法，拒绝生成远程路径')
    canonical = os.path.normcase(os.path.realpath(os.path.abspath(job_dir)))
    identity = '\0'.join((
        'vcstudio-remote-v1', str(profile.name),
        str((item or {}).get('job_id') or ''),
        str((item or {}).get('created_at') or ''), canonical,
    ))
    suffix = hashlib.sha256(
        identity.encode('utf-8', errors='surrogatepass')).hexdigest()[:16]
    readable = script_builder.sanitize_job_name(dir_name, max_len=48)
    root = (posixpath.join(profile.remote_root, namespace)
            if namespace else profile.remote_root)
    return posixpath.join(root, f'{readable}--{suffix}')


def _spec_for(profile, job_dir: str, manifest: dict | None = None) -> JobScriptSpec:
    dir_name = os.path.basename(os.path.normpath(job_dir))
    manifest = manifest if manifest is not None else (manifest_mod.load_manifest(job_dir) or {})
    engine = _job_engine(manifest)
    names, _input_errs = _declared_input_files(job_dir, manifest)
    return JobScriptSpec(
        job_name=script_builder.sanitize_job_name(dir_name),
        remote_dir=_remote_dir_for(profile, job_dir, manifest),
        queue=getattr(profile, 'queue', ''),
        nodes=int(getattr(profile, 'nodes', 1) or 1),
        ppn=int(getattr(profile, 'ppn', 0) or 0),
        walltime=getattr(profile, 'walltime', '24:00:00') or '24:00:00',
        env_lines=list(getattr(profile, 'env_lines', []) or []),
        # JobScriptSpec 保留历史字段名，但此处装入的是 manifest
        # 指定引擎的命令，绝不是无条件 vasp_cmd。
        vasp_cmd=_render_engine_command(
            profile, engine, names, script_builder.sanitize_job_name(dir_name)),
    )


def planned_remote_dir(profile, job_dir: str) -> str:
    """Return the exact remote directory submit_job would use, without I/O."""
    return _spec_for(profile, job_dir).remote_dir


def build_script_text(profile, job_dir: str) -> str:
    """按 profile 双轨生成最终 job 脚本文本(预览按钮与真提交共用,所见即所交)。"""
    manifest = manifest_mod.load_manifest(job_dir) or {}
    engine = _job_engine(manifest)
    if engine not in _SUPPORTED_ENGINES:
        raise ValueError(f'不支持的计算引擎:{engine!r}')
    spec = _spec_for(profile, job_dir, manifest)
    dialect = get_dialect(profile.scheduler)
    template_text = None
    mode = getattr(profile, 'script_mode', 'auto')
    if mode == 'template':
        with open(profile.template_path, 'r', encoding='utf-8') as f:
            template_text = f.read()
        if engine != 'vasp' and '{command}' not in template_text:
            raise ValueError(
                f'{_ENGINE_LABELS[engine]} 模板必须包含 {{command}} 占位符；'
                f'它会使用 engine_commands.{engine}。')
    if (mode == 'auto' or engine != 'vasp'
            or (template_text is not None and '{command}' in template_text)) and not spec.vasp_cmd:
        raise ValueError(_missing_command_message(profile, engine))
    if ((mode == 'auto' or engine != 'vasp'
         or (template_text is not None and '{command}' in template_text))
            and any(token in spec.vasp_cmd for token in ('{cores}', '{ppn}'))):
        raise ValueError(
            f'{_ENGINE_LABELS[engine]} 执行命令使用了核数占位符，'
            '但集群配置的 ppn 未填写或无效。')
    return script_builder.build_script(
        mode, dialect, spec, template_text)


# ── 提交 ───────────────────────────────────────────────────────────────────
@_serialized_job_argument(3, '提交')
def submit_job(client, sftp, profile, job_dir: str, *,
               idempotency_key: str | None = None) -> dict:
    """Upload and submit one job under a durable, fail-closed transaction.

    The per-job OS lock is already held by the decorator before this function
    re-reads ``job.yaml``.  A recovery journal is fsynced before the scheduler
    command, then advanced with the returned job id before ``job.yaml`` is
    replaced.  A crash can therefore be reported honestly as an unknown remote
    outcome and can never trigger an automatic second submission.
    """
    operation_key = str(idempotency_key or '').strip()
    if operation_key and (len(operation_key) > 128
                          or not re.fullmatch(r'[A-Za-z0-9_.:-]{12,128}',
                                              operation_key)):
        raise ValueError('无效的作业操作请求标识')

    # Re-read only after acquiring both the in-process and OS advisory locks.
    # A matching durable operation id may safely replay an already-recorded
    # submission; every other non-CREATED/uncertain state remains fail closed.
    authoritative = manifest_mod.load_manifest(job_dir)
    replay = _reconcile_submission_recovery(
        job_dir, authoritative, operation_key)
    if replay is not None:
        return replay

    errs = preflight(profile, job_dir)
    if errs:
        raise ValueError('；'.join(errs))
    m = _bind_execution_environment_locked(job_dir, profile, authoritative)
    spec = _spec_for(profile, job_dir, m)
    dialect = get_dialect(profile.scheduler)
    script_text = build_script_text(profile, job_dir)

    engine = _job_engine(m)
    incar_authority_sha256 = ''
    neb_image_authority: dict[str, str] = {}
    if engine == 'vasp':
        incar_authority_sha256 = _valid_sha256(
            ((m.get('inputs') or {}).get('sha256') or {}).get('INCAR'))
        if not incar_authority_sha256:
            raise ValueError(
                'VASP 作业缺准备期受管 INCAR 摘要；请重新准备后再提交')
        current_incar_sha256 = manifest_mod.sha256_file(
            os.path.join(job_dir, 'INCAR'))
        if current_incar_sha256 != incar_authority_sha256:
            raise ValueError(
                '本地 INCAR 已偏离准备期权威摘要；请重新准备后再提交')
        if _is_neb(m):
            # Preflight is advisory; freeze again immediately before the durable
            # recovery record so a directory added in between cannot enter upload.
            neb_image_authority = _freeze_neb_image_authority(job_dir, m)

    recovery = {
        'schema': _SUBMISSION_RECOVERY_SCHEMA,
        'transaction_id': os.urandom(16).hex(),
        'status': 'preparing',
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'manifest_job_id': str(m.get('job_id') or ''),
        'engine': engine,
        'cluster': str(profile.name or ''),
        'remote_dir': str(spec.remote_dir or ''),
        'idempotency_key': operation_key or None,
        'scheduler_job_id': None,
        'incar_sha256': incar_authority_sha256 or None,
        'neb_image_poscar_sha256': neb_image_authority or None,
    }
    # Freeze the exact generation authority before even the first upload.  A
    # crash in ``preparing`` is known not to have contacted the scheduler.
    _write_submission_recovery(job_dir, recovery)

    # 远程目录 + 上传(脚本统一 LF,防 Windows CRLF 毒害 shell)
    try:
        remote_parent = posixpath.dirname(spec.remote_dir.rstrip('/')) or '/'
        run_cmd(
            client,
            f'mkdir -p {shlex.quote(remote_parent)} && '
            f'mkdir {shlex.quote(spec.remote_dir)}',
            check=True)
        if _is_neb(m):
            # Upload only the frozen run inputs.  A fresh os.walk here would let
            # a concurrently-created numeric image escape the authority map.
            n_up = _upload_neb_tree(
                client, sftp, job_dir, spec.remote_dir,
                neb_image_authority, m)
            _assert_neb_image_authority(job_dir, neb_image_authority)
            upload_note = f'NEB 目录树 {n_up} 文件 + {SCRIPT_NAME}'
        else:
            input_files = _declared_input_files(job_dir, m)[0]
            for fname in input_files:
                sftp.put(os.path.join(job_dir, fname),
                         posixpath.join(spec.remote_dir, fname))
            upload_note = (
                f'{_ENGINE_LABELS.get(engine, engine)} {len(input_files)} 输入 + {SCRIPT_NAME}')
        with sftp.file(posixpath.join(spec.remote_dir, SCRIPT_NAME), 'w') as f:
            f.write(script_text.replace('\r\n', '\n'))
    except Exception:
        # Preserve the fsynced ``preparing`` record.  Although qsub is not yet
        # reachable, the remote leaf may contain a partial upload and must not
        # be silently reused by a retry.
        raise
    m['cluster'] = profile.name
    m['cluster_binding'] = profile_binding(profile)
    m['remote_dir'] = spec.remote_dir
    manifest_mod.set_state(m, 'UPLOADED', note=upload_note)

    recovery['status'] = 'submitting'
    _write_submission_recovery(job_dir, recovery)
    try:
        submit_command = dialect.submit_cmd(
            posixpath.join(spec.remote_dir, SCRIPT_NAME),
            getattr(profile, 'scheduler_bin', ''))
        if engine == 'vasp':
            guards = [
                f'cd {shlex.quote(spec.remote_dir)}',
                _remote_sha256_guard('INCAR', incar_authority_sha256),
            ]
            if neb_image_authority:
                guards.append(_remote_neb_frame_set_guard(
                    neb_image_authority))
            guards.extend(
                _remote_sha256_guard(f'{frame}/POSCAR', digest)
                for frame, digest in neb_image_authority.items())
            guards.append(submit_command)
            submit_command = ' && '.join(guards)
        out, err = run_cmd(client, submit_command, check=True)
    except Exception as exc:  # noqa: BLE001 - remote acceptance may be unknowable
        recovery['status'] = 'unknown_remote_submission'
        try:
            _write_submission_recovery(job_dir, recovery)
        except OSError:
            # The already-fsynced ``submitting`` record is the conservative
            # recovery gate even if advancing it fails.
            pass
        try:
            manifest_mod.save_manifest(job_dir, m)
        except OSError:
            pass
        raise UnknownRemoteSubmission(
            '提交命令返回前连接中断，远端是否受理未知；已禁止自动重试，请人工核对调度器。',
            recovery_status='unknown_remote_submission') from exc

    job_id = dialect.parse_job_id(out)
    if not job_id:
        recovery['status'] = 'unknown_remote_submission'
        try:
            _write_submission_recovery(job_dir, recovery)
        except OSError:
            # The original fsynced ``submitting`` journal still blocks retry.
            pass
        try:
            # Preserve the UPLOADED evidence, but keep the recovery gate: a
            # malformed response is not proof that the scheduler rejected it.
            manifest_mod.save_manifest(job_dir, m)
        except OSError:
            pass
        raise UnknownRemoteSubmission(
            f'提交失败：命令未返回可核验作业号（{dialect.name}）；'
            '已禁止自动重试，请人工核对调度器。',
            recovery_status='unknown_remote_submission')

    recovery['status'] = 'remote_accepted'
    recovery['scheduler_job_id'] = str(job_id)
    try:
        _write_submission_recovery(job_dir, recovery)
    except Exception as exc:  # noqa: BLE001 - retain prior durable submitting gate
        raise UnknownRemoteSubmission(
            '远端已返回作业号但恢复记录未能推进；已禁止自动重试，请人工核对调度器。',
            recovery_status='submitting', scheduler_job_id=job_id) from exc

    try:
        m['scheduler_job_id'] = job_id
        m['attempts'] = list(m.get('attempts') or [])
        attempt = {
            'n': len(m['attempts']) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'job_id': job_id,
            'cluster': profile.name,
            'queue': spec.queue or None,
            'script_mode': getattr(profile, 'script_mode', 'auto'),
            'engine': _job_engine(m),
            # v3.3.0 实际核时统计:提交时点核数(nodes×ppn;ppn 未配 → None,usage 端不编数)
            'cores': (spec.nodes * spec.ppn) if spec.ppn else None,
        }
        if engine == 'vasp':
            attempt['incar_sha256'] = incar_authority_sha256
            if neb_image_authority:
                attempt['neb_image_poscar_sha256'] = dict(
                    neb_image_authority)
            _set_incar_authority(
                m, scheduler_job_id=str(job_id),
                incar_sha256=incar_authority_sha256,
                transaction_id=recovery['transaction_id'],
                neb_image_poscar_sha256=(
                    neb_image_authority or None))
        if operation_key:
            attempt['idempotency_key'] = operation_key
        # The token is part of the immutable attempt audit record and later binds
        # downloaded files to this exact scheduler generation.
        token_manifest = {**m, 'scheduler_job_id': job_id,
                          'attempts': [*m['attempts'], attempt]}
        attempt['attempt_token'] = current_attempt_token(token_manifest)
        m['attempts'].append(attempt)
        manifest_mod.set_state(m, 'SUBMITTED', note=f'{dialect.name} {job_id}')
        manifest_mod.save_manifest(job_dir, m)
    except Exception as exc:  # noqa: BLE001 - journal owns the unresolved identity
        raise UnknownRemoteSubmission(
            '远端已受理作业，但本地清单未能安全持久化；已禁止自动重试，请人工恢复。',
            recovery_status='remote_accepted', scheduler_job_id=job_id) from exc

    try:
        _clear_submission_recovery(job_dir)
    except OSError:
        # The manifest is already authoritative.  A later retry reconciles the
        # matching journal and removes it without contacting the scheduler.
        pass
    return m


@_serialized_job_argument(2, '取消作业')
def cancel_job(client, profile, job_dir: str, *,
               idempotency_key: str | None = None,
               expected_job_id: str | None = None) -> dict:
    """Cancel one scheduler generation under a durable at-most-once contract.

    The operation journal is fsynced before qdel/scancel.  A known non-zero
    scheduler result is a terminal replayable failure; a transport interruption
    or local persistence failure is retained as an unresolved fail-closed gate.
    """
    operation_key = _validate_job_action_key(idempotency_key)
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('无 job.yaml,无法取消')
    assert_profile_binding(profile, job_dir, '取消作业', manifest=m)
    job_id = str(m.get('scheduler_job_id') or '')
    expected = str(expected_job_id or '').strip()
    if expected and job_id != expected:
        raise RuntimeError('取消前作业代次已变化，请刷新目标列表后重新确认')
    replay = _reconcile_job_action(job_dir, m, 'cancel', operation_key)
    if replay is not None:
        return replay
    if not job_id:
        raise ValueError('缺 scheduler_job_id,无法取消')
    dialect = get_dialect(profile.scheduler)
    record = _start_job_action(job_dir, 'cancel', operation_key, job_id)
    try:
        run_cmd(
            client,
            dialect.cancel_cmd(job_id, getattr(profile, 'scheduler_bin', '')),
            check=True)
    except RuntimeError as exc:
        # run_cmd(check=True) has received a complete non-zero scheduler result,
        # so no cancellation was accepted.  Persist that terminal result so a
        # restarted client with the same operation id does not issue it again.
        try:
            _update_job_action(
                job_dir, record, 'failed', message=str(exc), result_job_id=job_id)
        except Exception as journal_exc:  # noqa: BLE001 - uncertainty wins
            _mark_job_action_unknown(job_dir, record, str(exc))
            raise UnknownRemoteJobOperation(
                '取消命令失败且最终结果未能安全记录；已禁止自动重试，请人工核对。',
                action='cancel', recovery_status=record.get('status') or 'prepared',
                scheduler_job_id=job_id) from journal_exc
        raise
    except Exception as exc:  # noqa: BLE001 - acceptance may be unknowable
        _mark_job_action_unknown(job_dir, record, str(exc))
        raise UnknownRemoteJobOperation(
            '取消命令返回前连接中断，调度器是否受理未知；'
            '已禁止自动重试，请人工核对。',
            action='cancel', recovery_status=record.get('status') or 'prepared',
            scheduler_job_id=job_id) from exc

    try:
        _update_job_action(
            job_dir, record, 'remote_accepted', result_job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - prepared record still blocks retry
        raise UnknownRemoteJobOperation(
            '调度器已确认取消，但 journal 未能推进；已禁止自动重试，请人工核对。',
            action='cancel', recovery_status='prepared',
            scheduler_job_id=job_id) from exc

    attempt = {
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'cancelled',
        'action': 'cancel',
        'target_job_id': job_id,
        'operation_transaction_id': record['transaction_id'],
    }
    if operation_key:
        attempt['idempotency_key'] = operation_key
    m.setdefault('attempts', []).append(attempt)
    manifest_mod.set_state(m, 'FAILED', note='用户取消')
    try:
        manifest_mod.save_manifest(job_dir, m)
    except Exception as exc:  # noqa: BLE001 - remote_accepted journal blocks retry
        raise UnknownRemoteJobOperation(
            '调度器已取消作业，但本地 job.yaml 未能安全持久化；'
            '已禁止自动重试，请人工恢复。',
            action='cancel', recovery_status='remote_accepted',
            scheduler_job_id=job_id) from exc
    try:
        _update_job_action(
            job_dir, record, 'succeeded', result_job_id=job_id, message='')
    except Exception:  # noqa: BLE001 - job.yaml proves the exact transaction
        pass
    result = dict(m)
    result['_cancelled_job_id'] = job_id
    return result


# ── 状态刷新(含最小收敛判定,S3 雏形) ────────────────────────────────────────
_QOK = '___VCSQOK___'


def query_scheduler(client, profile) -> tuple:
    """调度器一次查询 → ({job_id: QUEUED|RUNNING}, {job_id: 终态原因})。

    在命令尾追加 `&& echo 哨兵`:qstat/squeue 退出码非零(调度器抖动/不可达)时哨兵
    不出现 → 抛错本轮跳过,**绝不**把空输出误判成"所有作业都结束了"再逐个标终态
    (瞬时一次抖动会永久错标 RUNNING 作业为 FAILED/UNCONVERGED,原版用跨轮确认防此)。
    终态原因(Slurm TO/CA/NF/OOM 短暂可见)喂 diagnose 消歧——超墙钟 137 若无原因
    会被误判 OOM→FAILED 困死可续算作业(审查确认的接线断点)。
    """
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.status_cmd(profile.username, getattr(profile, 'scheduler_bin', ''))
    out, _ = run_cmd(client, f'{cmd} && echo {_QOK}')
    if _QOK not in out:
        raise RuntimeError('调度器状态查询失败(qstat/squeue 无响应或报错);本轮跳过,不误判作业已结束')
    raw = out.replace(_QOK, '')
    return dialect.parse_status(raw), dialect.parse_terminal(raw)


def query_states(client, profile) -> dict:
    """兼容入口:只要统一态。新代码请用 query_scheduler(带终态原因)。"""
    return query_scheduler(client, profile)[0]


def query_queue_detail(client, profile) -> list:
    """调度器全量明细(该用户所有在队/在跑作业,含外部提交的)。

    → [{'job_id','state','name','workdir'}]。用途:「集群队列」视图 + 认领外部任务。
    同 query_scheduler 的哨兵防抖:查询失败抛错,绝不静默返回空当"队列空"。
    """
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.detail_cmd(profile.username, getattr(profile, 'scheduler_bin', ''))
    out, _ = run_cmd(client, f'{cmd} && echo {_QOK}')
    if _QOK not in out:
        raise RuntimeError('调度器队列查询失败(qstat/squeue 无响应或报错)')
    return dialect.parse_detail(out.replace(_QOK, ''))


def query_workdir(client, profile, job_id: str) -> str:
    """认领辅助:按作业号查远程工作目录(PBS 走 qstat -f;Slurm 的 detail 已带 %Z
    → 方言返回空命令,这里不发远程调用直接 '')。查不到 → ''(交前端让用户手填)。"""
    dialect = get_dialect(profile.scheduler)
    cmd = dialect.workdir_cmd(job_id, getattr(profile, 'scheduler_bin', ''))
    if not cmd:
        return ''
    out, _ = run_cmd(client, cmd)
    return dialect.parse_workdir(out)


def _validate_adopt_request(profile, job_id, remote_dir, name, task_type) -> None:
    """Reject malformed adoption requests before creating a target lock file."""
    if not str(getattr(profile, 'name', '') or '').strip():
        raise ValueError('认领作业需要一个具名集群配置')
    normalized_job_id = str(job_id).strip() if job_id is not None else ''
    if not normalized_job_id:
        raise ValueError('认领作业需要非空调度器作业号')
    if not isinstance(remote_dir, str) or not remote_dir.startswith('/'):
        raise ValueError('远程目录需为绝对路径(以 / 开头)')
    if name is not None and not isinstance(name, str):
        raise ValueError('作业名称必须是文本')
    if isinstance(name, str) and '\0' in name:
        raise ValueError('作业名称不能包含空字符')
    requested_task = str(task_type).strip() if task_type is not None else ''
    if requested_task:
        manifest_mod.normalize_task_type(requested_task)


def adopt_external_job(local_dir: str, profile, job_id: str, remote_dir: str,
                       name: str = '', task_type: str | None = None) -> dict:
    """Validate an adoption request before its directory-level mutation lock."""
    _validate_adopt_request(profile, job_id, remote_dir, name, task_type)
    return _adopt_external_job_locked(
        local_dir, profile, job_id, remote_dir, name=name, task_type=task_type)


@_serialized_job_argument(0, '认领', create_dir=True)
def _adopt_external_job_locked(local_dir: str, profile, job_id: str, remote_dir: str,
                       name: str = '', task_type: str | None = None) -> dict:
    """认领一个非本软件提交的集群作业:落 job.yaml + 入台账,之后查状态/拉回/续算全走原生路径。

    local_dir 是用户指定的本地目录(存在则直接用,不存在则创建;作为结果落点)。
    不上传/不动远端——认领只是登记事实:该作业号在该集群、结果在 remote_dir。
    状态置 SUBMITTED,下一次「查询状态」会按调度器现状推进(QUEUED/RUNNING/终态取证)。
    """
    if not str(remote_dir).startswith('/'):
        raise ValueError('远程目录需为绝对路径(以 / 开头)')
    existing = manifest_mod.load_manifest(local_dir)
    requested_task = (str(task_type).strip() if task_type is not None else '')
    requested_task = (manifest_mod.normalize_task_type(requested_task)
                      if requested_task else None)
    if existing:
        raw_existing_task = str(existing.get('task_type') or '').strip()
        if not raw_existing_task:
            if requested_task is None:
                raise ValueError('现有 job.yaml 缺 task_type；请显式选择计算类型后再认领')
            effective_task = requested_task
        else:
            effective_task = manifest_mod.normalize_task_type(raw_existing_task)
            if requested_task is not None and requested_task != effective_task:
                raise ValueError(
                    f'显式任务类型 {requested_task!r} 与现有 job.yaml '
                    f'的 {effective_task!r} 冲突；为避免改错收敛口径，拒绝覆盖')
    else:
        # 旧 API 调用没有 task_type 时仅对「全新目录」兼容为 relax。
        effective_task = requested_task or 'relax'
    if existing and existing.get('scheduler_job_id'):
        existing_job_id = str(existing.get('scheduler_job_id') or '')
        exact_binding = (
            existing_job_id == str(job_id)
            and str(existing.get('cluster') or '') == str(profile.name)
            and posixpath.normpath(str(existing.get('remote_dir') or ''))
            == posixpath.normpath(str(remote_dir)))
        if exact_binding:
            stored = existing.get('cluster_binding')
            if not (isinstance(stored, dict)
                    and str(stored.get('fingerprint') or '').strip()):
                # This explicit adopt call is the only name-only legacy migration.
                # Normal remote actions never acquire endpoint authority silently.
                binding = profile_binding(profile)
                existing['cluster_binding'] = binding
                existing.setdefault('attempts', []).append({
                    'n': len(existing.get('attempts') or []) + 1,
                    'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'action': 'cluster_binding_claim',
                    'claimed_scheduler_job_id': existing_job_id,
                    'cluster_fingerprint': binding['fingerprint'],
                    'remote_dir_sha256': hashlib.sha256(
                        str(remote_dir).encode('utf-8')).hexdigest(),
                })
                manifest_mod.save_manifest(local_dir, existing)
            else:
                assert_profile_binding(
                    profile, local_dir, '认领外部作业', manifest=existing)
            # 认领响应可能在前端收到前中断；同一 job/profile/remote 的重试只修复
            # 台账登记，不重复追加 attempts，也不改状态。
            from vcstudio.cluster import ledger
            ledger.register(local_dir)
            return existing
        raise ValueError(
            f'该本地目录已关联服务器「{existing.get("cluster") or "?"}」的作业号 '
            f'{existing_job_id}，与本次「{profile.name}」/{job_id} 不同；'
            '请换一个目录或先移出台账')
    # 所有参数/任务类型验证通过后才创建目录，失败调用不留空文件夹。
    os.makedirs(local_dir, exist_ok=True)
    dir_name = os.path.basename(os.path.normpath(local_dir))
    m = existing or manifest_mod.new_manifest(
        job_id=f'external-{dir_name}-{time.strftime("%Y%m%d-%H%M%S")}',
        system=name or dir_name, task_type=effective_task, calc_type='slab',
        inputs={'adopted': True, 'adopted_from': f'{profile.name}:{job_id}'})
    m['cluster'] = profile.name
    m['cluster_binding'] = profile_binding(profile)
    m['remote_dir'] = remote_dir
    m['scheduler_job_id'] = str(job_id)
    m['task_type'] = effective_task
    attempt = {
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'adopted',
        'job_id': str(job_id),
        'cluster': profile.name,
    }
    token_manifest = {**m, 'attempts': [*(m.get('attempts') or []), attempt]}
    attempt['attempt_token'] = current_attempt_token(token_manifest)
    m.setdefault('attempts', []).append(attempt)
    manifest_mod.set_state(m, 'SUBMITTED', note=f'认领外部作业 {job_id}(remote: {remote_dir})')
    manifest_mod.save_manifest(local_dir, m)
    from vcstudio.cluster import ledger
    ledger.register(local_dir)
    return m


# 续算沉降护栏:续算后连续 SETTLE_MAX_CHECKS 次仍"未见 RUNNING +
# OUTCAR 未刷新"，就转 NEEDS_HUMAN。绝不放行解析上一轮遗留输出。
SETTLE_MAX_CHECKS = 3


def _mark_observed_alive(m: dict, *, running=False) -> None:
    """记录本轮调度状态；只有 RUNNING 才证明新作业真正执行过。"""
    r = m.setdefault('results', {})
    if running:
        r['observed_running'] = True
        r['observed_alive'] = True  # 兼容旧项目/旧界面只读字段
        r.pop('settling', None)
    else:
        r['observed_queued'] = True


def _set_continue_baseline(m: dict, old_outcar_mtime, *, outputs_archived=False) -> None:
    """续算重投时落基线:上一轮 OUTCAR 的 mtime + 复位本轮存活标记与沉降态。

    refresh_job 的沉降护栏据此判断"新一轮是否真的重写过 OUTCAR",
    避免新作业还没启动时误读旧 OUTCAR 而判终态(见 _in_continue_settling)。
    """
    r = m.setdefault('results', {})
    r['continue_baseline'] = {'outcar_mtime': old_outcar_mtime, 'settle_checks': 0,
                              'outputs_archived': bool(outputs_archived),
                              'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    r['observed_alive'] = False
    r['observed_running'] = False
    r['observed_queued'] = False
    r.pop('settling', None)


def _in_continue_settling(m: dict, reason, outcar_mtime, outcar_size=None) -> bool:
    """续算后作业仍未真正启动本轮 → 返回 True(保持 SUBMITTED,不判终态)。

    仅对续算过(continue_rounds≥1)且仍在活动态的作业生效。"已启动本轮"的证据只能是
    本轮真正进入 RUNNING，或 OUTCAR 相对续算基线被重写(mtime 变化)。只见 QUEUED
    不代表运行过。两者皆无时，手里的 OUTCAR 就可能属于上一轮，绝不能用于终态。
    达到上限后失败即停地转 NEEDS_HUMAN，不解析旧文件。
    调用方已确保 u==GONE(非 QUEUED/RUNNING)。返回 True 时已就地更新 results,调用方需落盘。
    """
    r = m.get('results') or {}
    rounds = int(r.get('continue_rounds', 0) or 0)
    if rounds < 1 or m.get('state') not in {'SUBMITTED', 'QUEUED', 'RUNNING'}:
        return False
    baseline = r.get('continue_baseline') or {}
    base_mtime = baseline.get('outcar_mtime')
    if baseline.get('outputs_archived'):
        # 上轮输出已原子移入审计目录，工作目录中重新出现的 OUTCAR
        # 天然属于本轮；不再受远程文件系统秒级 mtime 精度影响。
        rewritten = isinstance(outcar_size, int) and outcar_size > 0
    else:
        # 旧清单兼容路径：没有归档证据时必须看到 mtime 变化。
        rewritten = outcar_mtime is not None and outcar_mtime != base_mtime
    if rewritten:
        return False  # 本轮确已重写 OUTCAR → 真终态,放行取证
    # 仍是旧 OUTCAR(或暂无 OUTCAR):记一次沉降观测。调度器已给终态原因
    # 时无需继续等待，但仍不能去解析旧 OUTCAR。
    checks = (SETTLE_MAX_CHECKS if reason else
              int(baseline.get('settle_checks', 0) or 0) + 1)
    baseline['settle_checks'] = checks
    r['continue_baseline'] = baseline
    r['settling'] = {
        'checks': checks,
        'reason': ('续算后新作业尚未进入 RUNNING，且未写出新 OUTCAR；'
                   '暂不判终态，旧轮输出不会被复用'),
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    m['results'] = r
    if checks >= SETTLE_MAX_CHECKS:
        evidence = ('续算重投后未观测到本轮 RUNNING 或 OUTCAR 更新'
                    + (f'；调度器原因：{reason}' if reason else ''))
        r['diagnosis'] = {
            'failure_class': 'CONTINUE_RUN_NOT_OBSERVED',
            'restartable': False,
            'evidence': evidence,
            'scheduler_reason': reason,
            'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        }
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed', 'failure_class': 'CONTINUE_RUN_NOT_OBSERVED',
            'to_state': 'NEEDS_HUMAN',
        })
        manifest_mod.set_state(m, 'NEEDS_HUMAN', note=evidence)
    return True


def _engine_output_evidence(client, remote: str, m: dict, job_dir: str | None = None):
    """Read the main non-VASP output's size and tail without downloading it."""
    files = fetch_files_for_manifest(m, job_dir=job_dir)
    name = files[0] if files else ''
    if not remote or not name:
        return name, None, ''
    path = shlex.quote(posixpath.join(remote, name))
    out, _ = run_cmd(
        client,
        f'stat -c "%s" {path} 2>/dev/null || true; '
        f'echo "___VCSENGINE___"; tail -n 2000 {path} 2>/dev/null')
    size_text, _sep, tail = out.partition('___VCSENGINE___')
    size = None
    for token in size_text.split():
        if token.isdigit():
            size = int(token)
            break
    return name, size, tail


def _refresh_non_vasp_terminal(client, job_dir: str, m: dict, reason):
    """Terminal-state evidence for Gaussian/CP2K/CASTEP quick jobs.

    These engines do not create VASP's OUTCAR/OSZICAR, so applying the VASP
    classifier always produced NO_OUTPUT.  A job is DONE only when its own
    normal-termination footer is present and the wrapper did not report a
    non-zero exit.  Ambiguous output is sent to NEEDS_HUMAN, never guessed.
    """
    engine = _job_engine(m)
    remote = str(m.get('remote_dir') or '')
    jid = str(m.get('scheduler_job_id') or '')
    output_name, output_size, output_tail = _engine_output_evidence(
        client, remote, m, job_dir=job_dir)
    exit_code, log_tail = _read_log(client, remote, jid)
    combined = (output_tail or '') + '\n' + (log_tail or '')
    task = _engine_task(m, job_dir)
    try:
        from vcstudio.engines import get_backend
        parsed = dict(get_backend(engine).parse_output_text(combined, task=task) or {})
    except (AttributeError, TypeError, ValueError) as exc:
        parsed = {'energy_ev': None, 'converged': False,
                  'normal_termination': False, 'task_converged': False,
                  'failed': True, 'error': f'解析器异常：{exc}'}
    normal = bool(parsed.get('normal_termination'))
    task_converged = bool(parsed.get('task_converged'))
    parser_failed = bool(parsed.get('failed'))

    reason_map = {
        diagnose.R_TIMEOUT: ('WALLTIME', 'UNCONVERGED'),
        diagnose.R_OOM: ('OOM', 'FAILED'),
        diagnose.R_CANCELLED: ('CANCELLED', 'FAILED'),
        diagnose.R_NODE_FAIL: ('NODE_FAIL', 'FAILED'),
        diagnose.R_FAILED: ('SCHEDULER_FAILED', 'FAILED'),
    }
    contract = get_run_contract(engine)
    restartable = bool(contract.restart_supported)
    if reason in reason_map:
        failure_class, state = reason_map[reason]
        evidence = f'调度器报 {reason}；{contract.restart_note}'
    elif exit_code not in (None, 0):
        failure_class, state = 'ENGINE_EXIT_NONZERO', 'FAILED'
        evidence = f'{_ENGINE_LABELS.get(engine, engine)} 退出码 {exit_code}'
    elif parser_failed:
        failure_class, state = 'ENGINE_OUTPUT_ERROR', 'FAILED'
        evidence = str(parsed.get('error') or
                       f'{_ENGINE_LABELS.get(engine, engine)} 输出含失败标志')
    elif (parsed.get('converged') and parsed.get('energy_ev') is not None
          and (output_size is None or output_size > 0)):
        failure_class, state = diagnose.CONVERGED, 'DONE'
        evidence = (f'{_ENGINE_LABELS.get(engine, engine)} 正常结束、任务级完成与最终能量'
                    f'三项证据齐全（{output_name or "主输出"}）')
    elif normal and not task_converged:
        failure_class, state = 'TASK_NOT_CONVERGED', 'UNCONVERGED'
        evidence = str(parsed.get('error') or
                       f'{_ENGINE_LABELS.get(engine, engine)} 正常退出但任务未收敛；'
                       f'{contract.restart_note}')
    elif normal and parsed.get('energy_ev') is None:
        failure_class, state = 'FINAL_ENERGY_MISSING', 'NEEDS_HUMAN'
        evidence = str(parsed.get('error') or '正常退出但缺最终能量，拒绝冒充完成')
    elif not output_size:
        failure_class, state = diagnose.NO_OUTPUT, 'NEEDS_HUMAN'
        evidence = f'{_ENGINE_LABELS.get(engine, engine)} 主输出 {output_name or "未声明"} 缺失或为空'
    else:
        failure_class, state = 'NORMAL_TERMINATION_NOT_FOUND', 'NEEDS_HUMAN'
        evidence = (f'{output_name} 有内容但未见 {_ENGINE_LABELS.get(engine, engine)} '
                    '正常结束标志，拒绝冒充完成')

    energy = parsed.get('energy_ev')
    if (isinstance(energy, bool) or not isinstance(energy, (int, float))
            or not math.isfinite(float(energy))):
        energy = None
    results = m.setdefault('results', {})
    if energy is not None:
        if state == 'DONE':
            results['energy_e0_eV'] = float(energy)
            results.pop('raw_energy_e0_eV', None)
        else:
            results['raw_energy_e0_eV'] = float(energy)
            results.pop('energy_e0_eV', None)
    elif state != 'DONE':
        results.pop('energy_e0_eV', None)
    results['diagnosis'] = {
        'failure_class': failure_class,
        'restartable': restartable,
        'evidence': evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'engine': engine,
        'task': parsed.get('task') or task,
        'parser_error': parsed.get('error'),
        'normal_termination': normal,
        'task_converged': task_converged,
        'restart_note': contract.restart_note,
        'output_file': output_name,
        'output_bytes': output_size,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if state != 'DONE':
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed', 'failure_class': failure_class, 'to_state': state,
        })
    manifest_mod.set_state(m, state, note=f'{failure_class}: {evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


@_serialized_job_argument(2, '刷新状态')
def refresh_job(client, profile, job_dir: str, live_states: dict | None = None,
                terminal_reasons: dict | None = None) -> dict:
    """按调度器现状更新一个作业的 manifest;终态时做取证 + 失败分类。

    live_states/terminal_reasons 可传入 query_scheduler 结果避免逐作业重复查询。
    终态分支不再只有 DONE/UNCONVERGED:调 diagnose.classify 综合 调度器原因 + 退出码 +
    OUTCAR/OSZICAR 完整性 + 日志签名 + 收敛串 + 能量合理性 → DONE/UNCONVERGED/FAILED/
    NEEDS_HUMAN,并把结构化诊断写回 results.diagnosis + 失败时追加 attempts(可审计)。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None or not m.get('scheduler_job_id'):
        return m
    assert_profile_binding(
        profile, job_dir, '刷新状态', manifest=m,
        allow_legacy_read=True)
    states = live_states if live_states is not None else query_states(client, profile)
    jid = str(m['scheduler_job_id'])
    u = states.get(jid, GONE)
    remote = m.get('remote_dir') or ''

    # NEB 多 image 走专用路径:主输出在各 image 子目录,根无 OUTCAR,收敛判定在 stdout
    if _is_neb(m):
        return _refresh_neb_job(client, profile, job_dir, m, u,
                                (terminal_reasons or {}).get(jid))

    if u == QUEUED:
        # 本轮已在调度器现身:记下"活过",续算沉降护栏据此放行(不再疑为旧 OUTCAR)
        _mark_observed_alive(m)
        if m['state'] != 'QUEUED':
            manifest_mod.set_state(m, 'QUEUED')
        manifest_mod.save_manifest(job_dir, m)
        return m
    if u == RUNNING:
        _mark_observed_alive(m, running=True)
        if m['state'] != 'RUNNING':
            manifest_mod.set_state(m, 'RUNNING')
        engine = _job_engine(m)
        task = _canonical_vasp_task(m)
        # 静态/频率没有“离子步”，若套用 relax 的 0 步告警，连续刷新三次便会假报
        # 首步卡死。只对真正沿结构/时间推进的 VASP 任务启用该活体判据；其他任务
        # 保留无告警的类型提示，终态再按各自完成证据裁决。
        if engine == 'vasp' and (task in _IONIC_TASKS or task == 'aimd'):
            live = _live_check(client, remote, (m.get('results') or {}).get('live'))
            if task == 'aimd':
                live['md_steps'] = live.get('ionic_steps')
                live['progress_kind'] = 'md'
            else:
                live['progress_kind'] = 'ionic'
            m.setdefault('results', {})['live'] = live
        else:
            m.setdefault('results', {})['live'] = {
                'warning': '', 'progress_kind': ('engine' if engine != 'vasp' else task),
            }
        manifest_mod.save_manifest(job_dir, m)
        return m

    # 终态候选(GONE / 调度器终态原因):先取 OUTCAR 现状(含 mtime)
    reason = (terminal_reasons or {}).get(jid)
    if _job_engine(m) != 'vasp':
        return _refresh_non_vasp_terminal(client, job_dir, m, reason)
    outcar_size, oszicar_size, outcar_mtime = _stat_outcar_full(client, remote)

    # ── 续算沉降护栏(修复:续算后仍在排队却被误判终态)─────────────────────────
    # 症状:续算重投 → 新作业号还没进 qstat(登记有延迟)→ 这里查为 GONE → 直接进终态分支
    # → 抓到的却是**上一轮**留下的完整 OUTCAR(收敛/SCF 震荡串还在)→ 明明在排队,
    # 却被标成 DONE / 需续算 / sloshing。只对**续算过**的作业设防(唯有它们才可能有旧 OUTCAR),
    # 且要求"当前这一轮确有产出"才认终态:本轮在调度器现过身,或 OUTCAR 相对续算基线被重写过。
    if _in_continue_settling(m, reason, outcar_mtime, outcar_size):
        manifest_mod.save_manifest(job_dir, m)
        return m
    m.setdefault('results', {}).pop('settling', None)  # 放行终态 → 清沉降态
    # ────────────────────────────────────────────────────────────────────────

    task = _canonical_vasp_task(m)
    inputs = m.get('inputs') or {}
    expected_steps = inputs.get('steps') if isinstance(inputs, dict) and task == 'aimd' else None
    converged, clean_exit, stopped = _grep_marks(
        client, remote, task, expected_steps=expected_steps)
    exit_code, log_tail = _read_log(client, remote, jid)
    energy, oszicar_tail = _read_oszicar(client, remote)

    d = diagnose.classify(
        scheduler_reason=reason, exit_code=exit_code,
        outcar_size=outcar_size, oszicar_size=oszicar_size,
        log_tail=log_tail, converged=converged, energy=energy,
        oszicar_tail=oszicar_tail, nelm=_read_nelm(job_dir),
        clean_exit=clean_exit, stopped=stopped)

    if energy is not None:
        # 物理合理性闸(审查#4):BAD_ENERGY 的垃圾数不进 energy_e0_eV(防经自由能路径
        # 漏进 ΔG/U_L 图),原始值留 raw_energy_e0_eV 供人工核查
        key = 'raw_energy_e0_eV' if d.failure_class == diagnose.BAD_ENERGY else 'energy_e0_eV'
        m.setdefault('results', {})[key] = energy
    m.setdefault('results', {})['diagnosis'] = {
        'failure_class': d.failure_class,
        'restartable': d.restartable,
        'evidence': d.evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'clean_exit': clean_exit,
        'outcar_bytes': outcar_size,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if d.failure_class != diagnose.CONVERGED:
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed',
            'failure_class': d.failure_class,
            'to_state': d.state,
        })
    manifest_mod.set_state(m, d.state, note=f'{d.failure_class}: {d.evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _neb_remote_intermediate_count(client, remote: str):
    """远端 image 子目录探测 → 中间 image 数(总帧数−2);列不到 → None(不据此误判)。"""
    if not remote:
        return None
    out, _ = run_cmd(client, f'cd {shlex.quote(remote)} && ls -1d [0-9][0-9] 2>/dev/null')
    dirs = [ln.strip() for ln in out.splitlines() if _NEB_FRAME_RE.match(ln.strip())]
    if not dirs:
        return None
    return max(len(dirs) - 2, 0)


def _neb_image_energies(client, remote: str, n_images):
    """各中间 image(01..N)末 E0 列表(缺 → None);运行中进度 + 终态取证共用。"""
    energies = []
    n = int(n_images or 0)
    for i in range(1, n + 1):
        e0, _tail = _read_oszicar(client, posixpath.join(remote, f'{i:02d}'))
        energies.append(e0)
    return energies


def _neb_forensics(client, remote: str, n_images):
    """终态取证:逐 image(01..N)读 OSZICAR → (energies, image_status, images_found)。

    image_status 每项 {'index','energy','empty','scf_fail'}:empty=OSZICAR 无内容(启动即死),
    scf_fail=末离子步 SCF 震荡(diagnose 判据)。images_found 为远端探测的中间 image 数。
    """
    energies, status = [], []
    n = int(n_images or 0)
    for i in range(1, n + 1):
        e0, tail = _read_oszicar(client, posixpath.join(remote, f'{i:02d}'))
        empty = not (tail or '').strip()
        scf_fail = diagnose.scan_oszicar_sloshing(tail) is not None
        energies.append(e0)
        status.append({'index': i, 'energy': e0, 'empty': empty, 'scf_fail': scf_fail})
    images_found = _neb_remote_intermediate_count(client, remote)
    return energies, status, images_found


def _grep_neb_converged(client, remote: str, job_id: str = '') -> bool:
    """NEB 收敛判定:stdout 出现 'reached required accuracy'(各 image 力收敛)→ True。

    NEB 根目录无 OUTCAR,收敛标志打到作业 stdout(*.o<num>/slurm-<num>.out/vasp.out/log)。
    按本作业号精确定位(同 _read_log,避免续算读到旧轮)。
    """
    if not remote:
        return False
    num = str(job_id).split('.')[0].strip()
    if num:
        targets = f'*.o{num} slurm-{num}.out vasp.out log stdout'
    else:
        targets = '*.o* slurm-*.out vasp.out log stdout'
    out, _ = run_cmd(
        client,
        f'cd {shlex.quote(remote)} && grep -h "{_NEB_CONVERGED_MARK}" {targets} 2>/dev/null | head -1')
    return _NEB_CONVERGED_MARK in out


def _refresh_neb_job(client, profile, job_dir: str, m: dict, u: str, reason):
    """NEB 作业状态刷新:QUEUED/RUNNING 沿用状态机(活体取各 image 进度),终态走 NEB 取证。

    终态取证:stdout 收敛标志 + 逐 image OSZICAR(能量/崩溃)+ 远端 image 数,
    交 diagnose.classify_neb 裁定(IMAGES 不符/image 缺输出/image SCF 崩/收敛/未收敛)。
    """
    remote = m.get('remote_dir') or ''
    n = _neb_n_images(m)

    if u == QUEUED:
        _mark_observed_alive(m)
        if m['state'] != 'QUEUED':
            manifest_mod.set_state(m, 'QUEUED')
        manifest_mod.save_manifest(job_dir, m)
        return m
    if u == RUNNING:
        _mark_observed_alive(m, running=True)
        if m['state'] != 'RUNNING':
            manifest_mod.set_state(m, 'RUNNING')
        # 活体进度:各 image 当前 E0(NEB 根无 OUTCAR,不查根收敛/SCF)
        m.setdefault('results', {})['neb_energies'] = _neb_image_energies(client, remote, n)
        manifest_mod.save_manifest(job_dir, m)
        return m

    # 终态取证
    jid = str(m.get('scheduler_job_id') or '')
    converged = _grep_neb_converged(client, remote, jid)
    exit_code, log_tail = _read_log(client, remote, jid)
    energies, status, images_found = _neb_forensics(client, remote, n)
    images_expected = _read_incar_images(job_dir)
    if images_expected is None:
        images_expected = n

    d = diagnose.classify_neb(
        images_expected=images_expected, images_found=images_found,
        image_status=status, converged=converged, scheduler_reason=reason,
        exit_code=exit_code, log_tail=log_tail)

    r = m.setdefault('results', {})
    r['neb_energies'] = energies
    r['diagnosis'] = {
        'failure_class': d.failure_class,
        'restartable': d.restartable,
        'evidence': d.evidence,
        'scheduler_reason': reason,
        'exit_code': exit_code,
        'images_found': images_found,
        'classified_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if d.failure_class != diagnose.CONVERGED:
        m.setdefault('attempts', []).append({
            'n': len(m.get('attempts') or []) + 1,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'result': 'failed',
            'failure_class': d.failure_class,
            'to_state': d.state,
        })
    manifest_mod.set_state(m, d.state, note=f'{d.failure_class}: {d.evidence}')
    manifest_mod.save_manifest(job_dir, m)
    return m


def _live_check(client, remote: str, prev: dict | None) -> dict:
    """运行中作业一次 SSH 活体取数 → live dict(健康计数器+进度)。

    取 离子步数(grep -c 'F=' 全文精确)/ 当前 |F|max(OUTCAR 'FORCES: max atom'
    末行,原版 running_progress 口径)/ OSZICAR 尾部(震荡扫描),交 diagnose.live_health
    做跨轮确认。远端取数失败 → 保留上轮计数器不误清零。
    """
    if not remote:
        return dict(prev or {})
    q = shlex.quote
    out, _ = run_cmd(
        client,
        f'grep -c "F=" {q(remote + "/OSZICAR")} 2>/dev/null || echo 0; '
        f"echo '___VCSLIVE___'; "
        f'grep "FORCES: max atom" {q(remote + "/OUTCAR")} 2>/dev/null | tail -1; '
        f"echo '___VCSLIVE___'; "
        f'tail -n 200 {q(remote + "/OSZICAR")} 2>/dev/null')
    parts = out.split('___VCSLIVE___')
    if len(parts) != 3:
        return dict(prev or {})                     # 取数异常:保留上轮计数,不误清零
    steps = _first_int(parts[0])
    fmax = ''
    toks = parts[1].split()
    if 'RMS' in toks:                                # 'FORCES: max atom, RMS  0.031  0.012'
        i = toks.index('RMS')
        if len(toks) > i + 1:
            fmax = toks[i + 1]
    live = diagnose.live_health(parts[2], steps, prev)
    live['ionic_steps'] = steps
    live['fmax'] = fmax
    return live


def _stat_sizes(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 字节数 → (outcar, oszicar);缺失文件对应 None。

    与收敛 grep 分开:区分"缺输出/启动即死"(size None/0)与"跑了但没收敛",
    堵掉旧版 '|| echo 0' 把缺 OUTCAR 误当未收敛的假阴性(缺口分析 P0)。
    """
    outcar, oszicar, _mtime = _stat_outcar_full(client, remote)
    return outcar, oszicar


def _stat_outcar_full(client, remote: str):
    """一次 stat 取 OUTCAR/OSZICAR 的字节数与 OUTCAR mtime(远端 epoch 秒)。

    → (outcar_size, oszicar_size, outcar_mtime);缺失/无 remote 对应 None。
    mtime 用于续算沉降护栏:判断"当前这一轮是否真的重写过 OUTCAR",
    避免续算后新作业尚未启动时误读上一轮旧 OUTCAR(见 refresh_job)。
    mtime 与 baseline 同取自集群 stat,同一时钟,无 client/server 时钟偏差问题。
    """
    if not remote:
        return None, None, None
    out, _ = run_cmd(client, f"cd {shlex.quote(remote)} && "
                             f"stat -c '%n %s %Y' OUTCAR OSZICAR 2>/dev/null")
    sizes: dict[str, int] = {}
    mtimes: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        # 兼容旧格式(仅 name size,2 列)与新格式(name size mtime,3 列)
        if len(parts) >= 2 and parts[1].lstrip('-').isdigit():
            sizes[parts[0]] = int(parts[1])
        if len(parts) >= 3 and parts[2].lstrip('-').isdigit():
            mtimes[parts[0]] = int(parts[2])
    return sizes.get('OUTCAR'), sizes.get('OSZICAR'), mtimes.get('OUTCAR')


# 收敛证据必须按任务语义分流：离子优化、电子单点、有限差分频率和 AIMD 的“完成”
# 不是同一件事。旧版只把 static/dos/band 当电子任务，其余新目录都会误用离子标志。
_IONIC_MARK = 'reached required accuracy'
_ELEC_MARK = 'aborting loop because EDIFF is reached'
_FREQ_MARK = 'THz'
_AIMD_MARK = 'F='

_IONIC_TASKS = frozenset({'relax', 'cellopt', 'dimer'})
_ELECTRONIC_TASKS = frozenset({
    'static', 'dos', 'dos_pdos', 'pdos', 'band', 'bands', 'bader',
    'chgdiff', 'elf', 'eos', 'workfunction', 'esp', 'vaspsol',
    'conv_scan', 'conv_encut', 'conv_kmesh', 'conv_vacuum',
    'conv_thickness', 'surface_energy', 'formation_binding',
})


def _canonical_vasp_task(m_or_task) -> str:
    """Normalise old aliases and estatic ``inputs.purpose`` into one task key."""
    if isinstance(m_or_task, dict):
        task = str(m_or_task.get('task_type') or '').strip().lower()
        inputs = m_or_task.get('inputs') or {}
        if not isinstance(inputs, dict):
            inputs = {}
        purpose = str(inputs.get('purpose') or '').strip().lower()
        declared_task = str(inputs.get('task') or '').strip().lower()
        # 老 estatic 清单曾统一写 static + purpose；规范清单可直接写具体任务。
        if task in ('', 'static'):
            task = {
                'pdos': 'dos_pdos', 'bader': 'bader', 'chgdiff': 'chgdiff',
                'esp': 'workfunction', 'elf': 'elf',
            }.get(purpose, task or 'static')
        if declared_task == 'workfunction':
            task = 'workfunction'
    else:
        task = str(m_or_task or '').strip().lower()
    return {'band': 'bands', 'dos': 'dos_pdos', 'pdos': 'dos_pdos'}.get(task, task)


# 干净退出页脚 + STOPCAR 叫停(网研核对:Kitchin/sisl 口径 completed≠converged;
# pymatgen Outcar.is_stopped 的 soft stop 双空格字面串)
_CLEAN_EXIT_MARK = 'General timing and accounting informations for this job'
_STOP_MARK = 'soft stop encountered'


def _grep_marks(client, remote: str, task_type: str = 'relax', *, expected_steps=None):
    """一次 SSH 取 OUTCAR 三个标志计数 → (converged, clean_exit, stopped)。

    - 离子优化/Dimer:OUTCAR 的力收敛标志；
    - 电子静态/性质/收敛扫描:OUTCAR 的 EDIFF 标志；
    - 频率:至少产出频率(THz)且有干净页脚；
    - AIMD:OSZICAR 的 MD 步数达到期望且有干净页脚。

    缺文件时三者 (False, False, False)。静态/离子任务的第一位表示「收敛串在场」，
    必须与第二位 clean_exit 一并交给 diagnose 才可判 DONE；保留原始收敛位是为了
    把「旧收敛串 + 截断」精确标成 NEEDS_HUMAN，而不是自动续算。
    """
    if not remote:
        return False, False, False
    task = _canonical_vasp_task(task_type)
    if task == 'freq':
        mark, marker_file = _FREQ_MARK, 'OUTCAR'
    elif task == 'aimd':
        mark, marker_file = _AIMD_MARK, 'OSZICAR'
    elif task in _ELECTRONIC_TASKS:
        mark, marker_file = _ELEC_MARK, 'OUTCAR'
    else:
        # 未知旧清单沿用离子口径，避免无证据地把历史 relax 改成静态。
        mark, marker_file = _IONIC_MARK, 'OUTCAR'
    o = shlex.quote(remote + '/OUTCAR')
    marker_path = shlex.quote(remote + '/' + marker_file)
    out, _ = run_cmd(
        client,
        f'c=$(grep -c "{mark}" {marker_path} 2>/dev/null || true); '
        f'printf "%s\\n" "${{c:-0}}"; '
        f'c=$(grep -c "{_CLEAN_EXIT_MARK}" {o} 2>/dev/null || true); '
        f'printf "%s\\n" "${{c:-0}}"; '
        f'c=$(grep -c "{_STOP_MARK}" {o} 2>/dev/null || true); '
        f'printf "%s\\n" "${{c:-0}}"')
    nums = [_first_int(ln) for ln in out.splitlines() if ln.strip()]
    nums += [0] * (3 - len(nums))
    marker_count, clean_count, stop_count = nums[:3]
    clean_exit = clean_count > 0
    if task == 'freq':
        converged = marker_count > 0 and clean_exit
    elif task == 'aimd':
        try:
            need = max(int(expected_steps or 0), 0)
        except (TypeError, ValueError):
            need = 0
        converged = marker_count > 0 and clean_exit and (need == 0 or marker_count >= need)
    else:
        converged = marker_count > 0
    return converged, clean_exit, stop_count > 0


def _grep_converged(client, remote: str, task_type: str = 'relax') -> bool:
    """兼容入口:只要收敛位。"""
    return _grep_marks(client, remote, task_type)[0]


def _read_nelm(job_dir: str) -> int:
    """本地作业目录 INCAR 的 NELM(缺失/读不到 → VASP 默认 60)。"""
    try:
        from vcstudio.generate.incar_builder import parse_incar
        with open(os.path.join(job_dir, 'INCAR'), 'r', encoding='utf-8', errors='replace') as f:
            v = parse_incar(f.read()).get('NELM')
        return int(v) if isinstance(v, (int, float)) and int(v) > 0 else 60
    except (OSError, ValueError, TypeError):
        return 60


def _read_log(client, remote: str, job_id: str = ''):
    """取本作业的 stdout 日志:EXIT 标记(退出码)+ 尾部文本(供 diagnose.scan_log 扫签名)。

    脚本 run_block 尾部 echo "EXIT: $?";PBS -j oe 合并到 <name>.o<num>,Slurm 到
    slurm-<jid>.out。**按本作业号定位**:续算在同一目录重投,旧 attempt 的 .o<旧号>
    仍在,通配 *.o* + tail -1 会按字母序读到旧作业(o100 < o99),把新一轮误分类;
    故用 *.o<本号> / slurm-<本号>.out 精确锁定,再以每次覆盖的 vasp.out/log 兜底。
    → (exit_code|None, tail)。
    """
    if not remote:
        return None, ''
    num = str(job_id).split('.')[0].strip()
    if num:
        targets = f'*.o{num} slurm-{num}.out vasp.out log'
    else:                                        # 无作业号 → 退回宽通配(单作业目录仍准)
        targets = '*.o* slurm-*.out vasp.out log'
    out, _ = run_cmd(
        client,
        f"cd {shlex.quote(remote)} && "
        f"grep -h 'EXIT:' {targets} 2>/dev/null | tail -1; "
        f"echo '___VCSLOG___'; "
        f"tail -n 20 {targets} 2>/dev/null")
    marker, _sep, tail = out.partition('___VCSLOG___')
    mm = re.search(r'EXIT:\s*(-?\d+)', marker)
    exit_code = int(mm.group(1)) if mm else None
    return exit_code, tail


def _first_int(text: str) -> int:
    for tok in text.split():
        try:
            return int(tok)
        except ValueError:
            continue
    return 0


# ── 结果回收(S3):把关键输出拉回本地作业目录 ─────────────────────────────────
# FETCH_FILES 保留为兼容常量（手动指定/旧调用仍取传统三件套）；默认下载现在由
# fetch_files_for_manifest 按任务动态决定，电子结构产物不再静默留在集群。
FETCH_FILES = ('CONTCAR', 'OSZICAR', 'OUTCAR')
_VASP_FETCH_BY_TASK = {
    'static': ('vasprun.xml', 'CHGCAR'),
    'dos_pdos': ('DOSCAR', 'vasprun.xml'),
    'bands': ('EIGENVAL', 'PROCAR', 'vasprun.xml'),
    'bader': ('CHGCAR', 'AECCAR0', 'AECCAR2', 'ACF.dat'),
    'chgdiff': ('CHGCAR',),
    'elf': ('ELFCAR',),
    'workfunction': ('LOCPOT',),
    'aimd': ('XDATCAR',),
    'freq': ('vasprun.xml',),
}
_FETCH_EVIDENCE_KEYS = (
    'fetched', 'fetched_missing', 'missing', 'fetched_at', 'fetched_job_id',
    'fetched_remote_dir', 'fetch_requested', 'fetched_state',
    'fetched_attempt_token', 'fetched_sha256', 'fetched_sizes',
    'fetched_files', 'fetch_contract',
)


def _safe_manifest_names(raw) -> list[str]:
    """Return de-duplicated single-segment names from a user-editable manifest."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or '').strip()
        if (not name or os.path.isabs(name) or os.path.basename(name) != name
                or '/' in name or '\\' in name
                or name in (manifest_mod.MANIFEST_NAME, SCRIPT_NAME)):
            continue
        if name not in out:
            out.append(name)
    return out


def _gaussian_checkpoint_from_input(job_dir: str | None, input_files) -> str | None:
    """Return a safe local ``%chk`` basename, or ``None`` when disabled/unknown."""
    if not job_dir:
        return None
    for name in input_files or ():
        if not str(name).lower().endswith(('.gjf', '.com')):
            continue
        try:
            with open(os.path.join(job_dir, str(name)), 'r', encoding='utf-8',
                      errors='replace') as handle:
                match = re.search(r'^\s*%chk\s*=\s*(\S+)\s*$', handle.read(),
                                  flags=re.MULTILINE | re.IGNORECASE)
        except OSError:
            return None
        if not match:
            return None
        value = match.group(1).strip()
        if os.path.basename(value) != value or '/' in value or '\\' in value:
            return None
        return value
    return None


def fetch_files_for_manifest(m: dict | None, *, job_dir: str | None = None) -> tuple[str, ...]:
    """Resolve the default result bundle for one manifest.

    VASP tasks receive their scientifically relevant parser inputs.  For
    Gaussian/CP2K/CASTEP quick jobs, users may declare ``inputs.output_files``;
    otherwise the main output filename is derived from the primary input and
    the documented command convention.  Every name is a safe single path
    segment because it is later joined to the SSH work directory.
    """
    m = m or {}
    inputs = m.get('inputs') or {}
    if not isinstance(inputs, dict):
        inputs = {}
    engine = _job_engine(m)
    explicit = _safe_manifest_names(inputs.get('output_files'))
    if engine == 'vasp':
        if explicit:
            return tuple(explicit)
        task = _canonical_vasp_task(m)
        names = list(FETCH_FILES) + list(_VASP_FETCH_BY_TASK.get(task, ()))
    else:
        declared = _safe_manifest_names(inputs.get('files'))
        contract = get_run_contract(engine)
        derived = list(contract.result_files(declared, task=_engine_task(m, job_dir)))
        # Explicit output_files remain authoritative for imported/custom jobs.
        # engine_generate manifests use the shared contract so task-specific
        # CASTEP .phonon/.geom and restart evidence are not silently omitted.
        names = list(explicit)
        if not explicit or inputs.get('generator') == 'engine_generate':
            names.extend(derived)
        if engine == 'gaussian' and (not explicit or inputs.get('generator') == 'engine_generate'):
            checkpoint = _gaussian_checkpoint_from_input(job_dir, declared)
            names = [name for name in names if not name.lower().endswith('.chk')]
            if checkpoint:
                names.append(checkpoint)
    return tuple(dict.fromkeys(names))


def fetch_files_for_job(job_dir: str) -> tuple[str, ...]:
    """Load ``job.yaml`` and return that job's default download bundle."""
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法确定结果文件')
    return fetch_files_for_manifest(m, job_dir=job_dir)


def _clear_fetch_evidence(m: dict) -> None:
    """新一轮重投成功后清掉上一轮的下载完成证据（本地输出文件原样保留）。

    同一作业目录续算会复用 ``remote_dir``，本地也会保留旧 OUTCAR/OSZICAR 供审计；
    因此不能以文件仍存在或旧 ``fetched_at`` 判断新一轮结果已经回收。调用方只在取得
    新调度器作业号后调用本函数，重投失败时不会误清上一轮证据。
    """
    results = m.setdefault('results', {})
    for key in _FETCH_EVIDENCE_KEYS:
        results.pop(key, None)


def _atomic_sftp_get(sftp, remote_path: str, local_path: str) -> None:
    """Download beside the destination and atomically replace it on success.

    Paramiko writes directly to its target.  A dropped connection would
    otherwise truncate an older valid OUTCAR/vasprun.xml and leave no recovery
    path.  The temporary file lives in the same directory so ``os.replace`` is
    atomic on the destination filesystem.
    """
    parent = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f'.{os.path.basename(local_path)}.vcstudio-', suffix='.part', dir=parent)
    os.close(fd)
    try:
        sftp.get(remote_path, tmp_path)
        os.replace(tmp_path, local_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _file_sha256_size(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _same_fetch_generation(before: dict, after: dict | None) -> bool:
    """CAS predicate used after network I/O and immediately before evidence save."""
    if not after:
        return False
    return (
        str(after.get('state') or '') == str(before.get('state') or '')
        and str(after.get('scheduler_job_id') or '')
        == str(before.get('scheduler_job_id') or '')
        and str(after.get('remote_dir') or '') == str(before.get('remote_dir') or '')
        and current_attempt_token(after) == current_attempt_token(before)
        and (after.get('cluster_binding') or None)
        == (before.get('cluster_binding') or None)
    )


@_serialized_job_argument(2, '拉回结果')
def fetch_results(client, sftp, job_dir: str, files=None, *, profile=None,
                  preview: bool = False):
    """下载远程输出文件到本地作业目录。返回 (fetched, missing) 两个文件名列表。

    - 不覆盖输入语义:CONTCAR/OSZICAR/OUTCAR 与四件套不重名,直接落在 job_dir。
    - 单个文件缺失(如未跑出 CONTCAR)记入 missing,不中断其余下载。
    - 默认为最终回收，仅 DONE 可执行；``preview=True`` 是显式实时预览，
      允许下载但绝不写 fetched_at/哈希等最终证据。
    - 最终下载记录绑定 state/job/remote/attempt token，且在网络 I/O
      后重读 job.yaml 做 CAS；同目录并发续算时绝不覆盖新作业清单。
    """
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法定位远程目录')
    if profile is not None:
        assert_profile_binding(
            profile, job_dir, '拉回结果', manifest=m,
            allow_legacy_read=True)
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业尚未提交过(manifest 无 remote_dir)')
    if not preview and str(m.get('state') or '') != 'DONE':
        raise ValueError(
            f'最终结果只能在作业 DONE 后拉回；当前状态为 '
            f'{m.get("state") or "未知"}。运行中查看请使用显式预览模式')
    if files is None:
        requested = list(fetch_files_for_manifest(m, job_dir=job_dir))
    else:
        raw_requested = list(files)
        requested = _safe_manifest_names(raw_requested)
        raw_clean = list(dict.fromkeys(str(name or '').strip() for name in raw_requested
                                       if str(name or '').strip()))
        if requested != raw_clean:
            raise ValueError('结果文件名非法：只允许远程作业目录内的单个文件名')
    if _is_neb(m):
        fetched, missing = _fetch_neb_results(client, sftp, job_dir, remote, requested)
    else:
        fetched, missing = [], []
        for fname in requested:
            try:
                _atomic_sftp_get(
                    sftp, posixpath.join(remote, fname), os.path.join(job_dir, fname))
                fetched.append(fname)
            except (IOError, OSError):
                missing.append(fname)
    if preview:
        return fetched, missing

    # The manifest may have been continued/re-submitted while SFTP was busy.
    # Always update a freshly loaded copy and only when the exact generation is
    # unchanged; the downloaded files then remain untrusted local artefacts and
    # cannot unlock analysis because no final evidence is written.
    latest = manifest_mod.load_manifest(job_dir)
    if not _same_fetch_generation(m, latest):
        raise RuntimeError(
            '结果下载期间作业状态或提交代次已变化；已放弃写入最终下载证据，'
            '请刷新状态后重新拉取')
    file_evidence = {}
    for name in fetched:
        local_path = os.path.join(job_dir, *str(name).split('/'))
        try:
            digest, size = _file_sha256_size(local_path)
        except OSError as exc:
            raise RuntimeError(f'下载后无法校验文件 {name}:{exc}') from exc
        file_evidence[str(name)] = {'sha256': digest, 'size': size}
    # Hashing a very large vasprun/CHGCAR can take long enough for a restart to
    # happen, so repeat the generation CAS immediately before the manifest
    # update and use that newest copy instead of the pre-download snapshot.
    newest = manifest_mod.load_manifest(job_dir)
    if not _same_fetch_generation(m, newest):
        raise RuntimeError(
            '结果校验期间作业状态或提交代次已变化；已放弃写入最终下载证据，'
            '请刷新状态后重新拉取')
    m = newest
    attempt_token = current_attempt_token(m)
    res = m.setdefault('results', {})
    # ``missing`` 是旧版本曾用过的别名；本轮统一写 ``fetched_missing``，并清掉旧值，
    # 避免界面把上一轮缺失列表与当前下载结果混在一起。
    res.pop('missing', None)
    res['fetched'] = fetched
    res['fetched_missing'] = missing
    res['fetched_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    res['fetched_job_id'] = str(m.get('scheduler_job_id') or '')
    res['fetched_remote_dir'] = str(remote)
    res['fetch_requested'] = requested
    res['fetched_state'] = 'DONE'
    res['fetched_attempt_token'] = attempt_token
    res['fetched_files'] = file_evidence
    res['fetched_sha256'] = {
        name: evidence['sha256'] for name, evidence in file_evidence.items()
    }
    res['fetched_sizes'] = {
        name: evidence['size'] for name, evidence in file_evidence.items()
    }
    res['fetch_contract'] = {
        'schema': 1,
        'mode': 'final',
        'state': 'DONE',
        'scheduler_job_id': str(m.get('scheduler_job_id') or ''),
        'remote_dir': str(remote),
        'attempt_token': attempt_token,
        'requested': requested,
    }
    manifest_mod.save_manifest(job_dir, m)
    return fetched, missing


def _fetch_neb_results(client, sftp, job_dir: str, remote: str, files):
    """NEB 结果回收:逐 image 子目录(00..N+1)下载 files 到本地同名子目录。

    → (fetched, missing),名字带 image 前缀(如 '01/OSZICAR')。端点(00/N+1)常只有
    OUTCAR(读取初/末态能量),缺项记 missing 不中断其余。
    """
    fetched, missing = [], []
    for fr in _neb_local_frames(job_dir):
        local_sub = os.path.join(job_dir, fr)
        os.makedirs(local_sub, exist_ok=True)
        for fname in files:
            tag = f'{fr}/{fname}'
            try:
                _atomic_sftp_get(
                    sftp, posixpath.join(remote, fr, fname), os.path.join(local_sub, fname))
                fetched.append(tag)
            except (IOError, OSError):
                missing.append(tag)
    return fetched, missing


# ── 有界恢复:CONTCAR 续算(对齐论文 bounded recovery;人工触发,冻结 INCAR) ──────
CONTINUE_MAX_ROUNDS = 3
CONTINUE_TERMINAL_STATES = frozenset({
    'DONE', 'FAILED', 'UNCONVERGED', 'NEEDS_HUMAN',
})
# 续算前几何健全阈值(Å):周期最小原子间距低于此值判原子重叠(< 最短化学键 H-H 0.74)。
MIN_INTERATOMIC_OK = 0.7


def _read_remote_text(client, path: str) -> str:
    out, _ = run_cmd(client, f'cat {shlex.quote(path)} 2>/dev/null')
    return out


def _remote_sha256_guard(name: str, expected_sha256: str) -> str:
    """Shell precondition for one fixed-name remote input (fails if tool/file differs)."""
    digest = str(expected_sha256 or '').lower()
    if re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('远端输入 SHA-256 绑定无效')
    filename = str(name or '')
    if (filename not in {'INCAR', 'POSCAR', 'CONTCAR'}
            and re.fullmatch(r'[0-9]{2,4}/POSCAR', filename) is None):
        raise ValueError('远端输入文件名不在续算白名单')
    return (
        f"printf '%s  %s\\n' {shlex.quote(digest)} {shlex.quote(filename)} "
        '| sha256sum -c - >/dev/null')


def _contcar_min_distance(text: str):
    """CONTCAR → 周期最小间距；非有限数值/奇异晶胞/解析失败均返回 None。"""
    try:
        from vcstudio.generate.slab_builder import min_interatomic_distance
        from vcstudio.generate.structure_view import parse_positions

        parsed = parse_positions(text)
        cell = parsed['cell']
        coords = parsed['coords']
        values = [float(value) for row in (*cell, *coords) for value in row]
        if not values or not all(math.isfinite(value) for value in values):
            return None
        determinant = (
            cell[0][0] * (cell[1][1] * cell[2][2] - cell[1][2] * cell[2][1])
            - cell[0][1] * (cell[1][0] * cell[2][2] - cell[1][2] * cell[2][0])
            + cell[0][2] * (cell[1][0] * cell[2][1] - cell[1][1] * cell[2][0])
        )
        if not math.isfinite(determinant) or abs(determinant) <= 1e-12:
            return None
        distance = float(min_interatomic_distance(text))
        if math.isinf(distance) and len(coords) == 1:
            return distance
        return distance if math.isfinite(distance) else None
    except Exception:                                    # noqa: BLE001
        return None


def _require_continue_terminal_state(manifest: dict, action: str) -> None:
    state = str(manifest.get('state') or '')
    if state in CONTINUE_TERMINAL_STATES:
        return
    if state in {'UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING'}:
        raise ValueError(
            f'该作业仍在队列/运行中(状态 {state}),不能{action};'
            '请先查询状态确认已结束')
    raise ValueError(
        f'该作业状态 {state or "<缺失>"} 不是可{action}的终态；'
        '仅 DONE/FAILED/UNCONVERGED/NEEDS_HUMAN 可由用户明确重投')


def valid_scheduler_job_id(value) -> bool:
    """Return whether one persisted source scheduler generation is usable."""
    raw = str(value or '')
    normalized = raw.strip()
    return (raw == normalized
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', normalized)
            is not None)


def _require_source_scheduler_job_id(manifest: dict, action: str) -> str:
    job_id = str(manifest.get('scheduler_job_id') or '')
    if not valid_scheduler_job_id(job_id):
        raise ValueError(
            f'缺少可核验的源 scheduler_job_id，不能{action}；'
            '请先刷新或认领旧调度器作业，避免与未知在途代次并发')
    return job_id


def _valid_sha256(value) -> str:
    digest = str(value or '').strip().lower()
    return digest if re.fullmatch(r'[0-9a-f]{64}', digest) else ''


def _current_incar_authority(manifest: dict, source_job_id: str) -> str:
    """Resolve the INCAR digest authorized for the current scheduler generation."""
    authority = manifest.get('execution_authority')
    if isinstance(authority, dict):
        authority_job_id = str(authority.get('scheduler_job_id') or '')
        authority_digest = _valid_sha256(authority.get('current_incar_sha256'))
        if authority_job_id != source_job_id or not authority_digest:
            raise ValueError(
                'job.yaml 的执行代次 INCAR 权威绑定无效；请人工恢复或重新准备作业')
        matching_attempt = next((
            item for item in reversed(manifest.get('attempts') or [])
            if isinstance(item, dict)
            and str(item.get('job_id') or '') == source_job_id
        ), None)
        attempt_digest = _valid_sha256(
            (matching_attempt or {}).get('incar_sha256'))
        if attempt_digest != authority_digest:
            raise ValueError(
                'job.yaml 的执行代次与 attempt INCAR 摘要不一致；不能续算')
        if (matching_attempt or {}).get('action') in {
                'contcar_restart', 'incar_tuned_restart'}:
            authority_transaction = str(authority.get('transaction_id') or '')
            attempt_transaction = str(
                (matching_attempt or {}).get('operation_transaction_id') or '')
            if (not authority_transaction
                    or authority_transaction != attempt_transaction):
                raise ValueError(
                    'job.yaml 的执行代次与 attempt 事务绑定不一致；不能续算')
        return authority_digest

    # A legacy baseline is reconstructable only before any continuation changed
    # INCAR: the preparation-time managed-input digest remains authoritative.
    attempts = [item for item in manifest.get('attempts') or []
                if isinstance(item, dict)]
    if any(item.get('action') in {'contcar_restart', 'incar_tuned_restart'}
           for item in attempts):
        raise ValueError(
            '旧作业已有续算历史但缺执行代次 INCAR 权威摘要；不能从现场文件重建，请人工恢复')
    prepared = _valid_sha256(
        ((manifest.get('inputs') or {}).get('sha256') or {}).get('INCAR'))
    if not prepared:
        raise ValueError(
            '旧作业缺可重建的准备期 INCAR 摘要；请重新准备或人工迁移后再续算')
    return prepared


def _set_incar_authority(manifest: dict, *, scheduler_job_id: str,
                         incar_sha256: str, transaction_id: str,
                         neb_image_poscar_sha256: dict | None = None) -> None:
    digest = _valid_sha256(incar_sha256)
    if not valid_scheduler_job_id(scheduler_job_id) or not digest:
        raise ValueError('无法持久化无效的执行代次 INCAR 权威绑定')
    manifest['execution_authority'] = {
        'schema': 'vcstudio.execution-input-authority/v1',
        'scheduler_job_id': str(scheduler_job_id),
        'current_incar_sha256': digest,
        'transaction_id': str(transaction_id or ''),
    }
    if neb_image_poscar_sha256 is not None:
        canonical = {
            str(key): _valid_sha256(value)
            for key, value in sorted(neb_image_poscar_sha256.items())
        }
        if (len(canonical) < 3
                or any(re.fullmatch(r'[0-9]{2,4}', key) is None or not digest
                       for key, digest in canonical.items())
                or list(canonical) != [
                    f'{index:02d}' for index in range(len(canonical))]):
            raise ValueError('无法持久化无效的 NEB image POSCAR 权威摘要')
        manifest['execution_authority'][
            'neb_image_poscar_sha256'] = canonical


def _restart_cleanup_command(m: dict, job_dir: str | None = None) -> str:
    """Return safe wavefunction-history cleanup for one VASP restart.

    Non-self-consistent bands (ICHARG=11) hard-require the parent CHGCAR, so
    deleting it turns every otherwise valid restart into an immediate crash.
    Other VASP tasks retain the established clean-charge restart policy.
    """
    needs_chgcar = (_canonical_vasp_task(m) == 'bands'
                    or (job_dir is not None and _local_vasp_icharg(job_dir) in (1, 11)))
    return ('rm -f WAVECAR' if needs_chgcar
            else 'rm -f WAVECAR CHGCAR')


def _restart_archive_command(round_number: int, *,
                             backup_inputs: tuple[str, ...] = (),
                             promote_contcar: bool = False) -> tuple[str, str]:
    """Atomically remove prior-round outputs from the live remote directory.

    The files remain available under ``.vcstudio_history`` for audit, but can
    no longer masquerade as evidence from the newly submitted scheduler job.
    """
    archive = f'.vcstudio_history/round_{int(round_number):02d}'
    names = ('OUTCAR', 'OSZICAR', 'vasprun.xml', 'CONTCAR', 'XDATCAR')
    moves = ' '.join(shlex.quote(name) for name in names)
    quoted_archive = shlex.quote(archive)
    commands = [f'mkdir -p {quoted_archive}']
    for name in backup_inputs:
        quoted_name = shlex.quote(name)
        previous = shlex.quote(posixpath.join(archive, f'.vcstudio_previous_{name}'))
        missing = shlex.quote(posixpath.join(archive, f'.vcstudio_missing_{name}'))
        commands.append(
            f'if [ -e {quoted_name} ]; then cp -p {quoted_name} {previous}; '
            f'else : > {missing}; fi')
    if promote_contcar:
        # Must happen before CONTCAR is moved into the round archive.
        commands.append('cp CONTCAR POSCAR')
    commands.append(
        f'for f in {moves}; do '
        f'if [ -e "$f" ]; then mv "$f" {quoted_archive}/; fi; done')
    command = ' && '.join(commands)
    return command, archive


def _restart_restore_command(archive: str, *,
                             restore_inputs: tuple[str, ...] = ()) -> str:
    """Restore archived outputs and input snapshots after submission failure."""
    quoted = shlex.quote(archive)
    names = ('OUTCAR', 'OSZICAR', 'vasprun.xml', 'CONTCAR', 'XDATCAR')
    commands = [f'if [ -d {quoted} ]; then']
    for name in names:
        archived = shlex.quote(posixpath.join(archive, name))
        commands.append(f'if [ -e {archived} ]; then mv -f {archived} .; fi;')
    for name in restore_inputs:
        quoted_name = shlex.quote(name)
        previous = shlex.quote(posixpath.join(archive, f'.vcstudio_previous_{name}'))
        missing = shlex.quote(posixpath.join(archive, f'.vcstudio_missing_{name}'))
        commands.append(
            f'if [ -e {previous} ]; then mv -f {previous} {quoted_name}; '
            f'elif [ -e {missing} ]; then rm -f {quoted_name} {missing}; fi;')
    commands.append(f'rmdir {quoted} 2>/dev/null || true; fi')
    return ' '.join(commands)


def _restore_local_restart_file(path: str, round_number: int,
                                existed_before: bool) -> None:
    """Restore a locally mutated restart input while retaining its audit backup."""
    backup = f'{path}.bak{round_number}'
    if existed_before and os.path.isfile(backup):
        shutil.copyfile(backup, path)
    elif not existed_before and os.path.exists(path):
        os.remove(path)


@_serialized_job_argument(2, '续算')
def continue_from_contcar(client, profile, job_dir: str,
                          max_rounds: int | None = CONTINUE_MAX_ROUNDS, *,
                          idempotency_key: str | None = None) -> dict:
    """把一个可续算作业从 CONTCAR 接着跑(cp CONTCAR POSCAR + 冻结 INCAR 重投同一脚本)。

    有界恢复(论文核心 + 交接三不变式):
    - 只对 diagnose 标 restartable 的分类(未收敛/墙钟/ZBRENT)出手,否则拒绝;
    - CONTCAR 必须通过 valid_poscar 校验(防拿半个结构续出垃圾);
    - **INCAR 逐字冻结**(方法学主权,无可比性护栏前的安全默认);
    - 自动恢复的 continue_rounds 硬上限为 3,到顶停机交人工(防死循环);
    - ``max_rounds=None`` 只供服务端已确认的人工入口使用,仅放宽轮次门;
    - 清远端 WAVECAR/CHGCAR 去混合历史；bands 因 ICHARG=11 必须保留 CHGCAR；
      记 prev_job_id 溯源。
    失败抛 ValueError/RuntimeError(中文)。成功返回更新后的 manifest(state=SUBMITTED)。
    """
    operation_key = _validate_job_action_key(idempotency_key)
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法续算')
    if _job_engine(m) != 'vasp':
        contract = get_run_contract(_job_engine(m))
        raise ValueError(
            f'{_ENGINE_LABELS.get(_job_engine(m), _job_engine(m))} 不能走 VASP CONTCAR 续算；'
            f'{contract.restart_note}')
    assert_profile_binding(profile, job_dir, '续算', manifest=m)
    source_job_id = _require_source_scheduler_job_id(m, '续算')
    authorized_incar_sha256 = _current_incar_authority(m, source_job_id)
    intent = {
        'restart_from_contcar': True,
        'incar_policy': 'frozen',
        'round_policy': ('manual-unbounded' if max_rounds is None
                         else f'bounded-{int(max_rounds)}'),
    }
    intent_sha256 = _job_action_request_sha256(intent)
    replay = _reconcile_job_action(
        job_dir, m, 'continue', operation_key,
        intent_sha256=intent_sha256)
    if replay is not None:
        return replay
    local_incar = os.path.join(job_dir, 'INCAR')
    if not os.path.isfile(local_incar):
        raise ValueError('本地作业目录缺 INCAR，无法证明冻结输入，不能续算')
    incar_source_sha256 = manifest_mod.sha256_file(local_incar)
    if incar_source_sha256 != authorized_incar_sha256:
        raise ValueError(
            '本地 INCAR 已偏离上一调度器代次的权威摘要；'
            '普通续算不能建立新方法基线，请使用受控改参流程或重新准备')
    if _is_neb(m):
        raise ValueError(
            'NEB 不能使用通用 CONTCAR 续算：NEB 根目录没有单一 CONTCAR，'
            '必须保留各 image 并使用 NEB 专用重提流程；本次未修改远端文件')
    # 状态门(严重 bug 防护):仍在队列/运行中的作业绝不续算——否则会往活作业目录里
    # cp CONTCAR POSCAR + 重投第二个实例,两个 VASP 同写 OUTCAR 冲垮结果,且旧作业号被
    # 覆盖成孤儿。restartable 诊断是上一轮终态留下的,重投后必须消费掉(见函数尾)。
    _require_continue_terminal_state(m, '续算')
    diag = (m.get('results') or {}).get('diagnosis') or {}
    if not diag.get('restartable'):
        raise ValueError(
            f"该作业不可自动续算(分类 {diag.get('failure_class', '?')});"
            f'仅 未收敛/墙钟/ZBRENT 等可从 CONTCAR 续算,硬崩/缺输出需人工')
    rounds = int((m.get('results') or {}).get('continue_rounds', 0))
    if max_rounds is not None and rounds >= max_rounds:
        raise RuntimeError(f'已续算 {rounds} 次达上限 {max_rounds},停机交人工(防死循环)')
    manual_round_override = max_rounds is None and rounds >= CONTINUE_MAX_ROUNDS
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业无 remote_dir(未提交过),无法续算')

    contcar = _read_remote_text(client, posixpath.join(remote, 'CONTCAR'))
    if not diagnose.valid_poscar(contcar):
        raise RuntimeError('远端 CONTCAR 缺失或不完整,不能续算(防半个结构续出垃圾),请人工检查')

    # 几何健全:CONTCAR 原子重叠(周期最小间距 < 阈值)→ 不续算,转人工。病态几何续算只会
    # 反复崩(ZPOTRF/发散),盲目重投浪费机时;显式转 NEEDS_HUMAN 让人先修结构。
    min_d = _contcar_min_distance(contcar)
    if min_d is None:
        raise RuntimeError(
            'CONTCAR 晶格或坐标不是有限、非奇异的可核验几何，不能续算；请人工检查')
    if min_d < MIN_INTERATOMIC_OK:
        msg = f'CONTCAR 存在原子重叠(最小间距 {min_d:.2f} Å),疑似几何病态,请人工检查'
        manifest_mod.set_state(m, 'NEEDS_HUMAN', note=msg)
        manifest_mod.save_manifest(job_dir, m)
        raise RuntimeError(msg)

    # 续算沉降基线:重投前记下上一轮 OUTCAR 的 mtime(此刻新作业尚未启动,仍是旧文件)
    _o0, _z0, _base_outcar_mtime = _stat_outcar_full(client, remote)

    request = {
        'schema': 'vcstudio.continuation-request/v1',
        'manifest_job_id': str(m.get('job_id') or ''),
        'source_scheduler_job_id': source_job_id,
        'source_attempt_token': current_attempt_token(m),
        'source_authority_transaction_id': str(
            (m.get('execution_authority') or {}).get('transaction_id') or ''),
        'source_state': str(m.get('state') or ''),
        'source_round': rounds,
        'profile_fingerprint': str(profile_binding(profile)['fingerprint']),
        'remote_dir_sha256': hashlib.sha256(
            str(remote).encode('utf-8')).hexdigest(),
        'intent': intent,
        'round_limit_override': (
            'manual-explicit' if manual_round_override else None),
        'contcar_source_sha256': hashlib.sha256(
            contcar.encode('utf-8')).hexdigest(),
        'incar_source_sha256': incar_source_sha256,
        'authorized_incar_sha256': authorized_incar_sha256,
    }
    request_sha256 = _job_action_request_sha256(request)

    # 从第一个本地/远端变更开始，持久 journal 必须先落盘。若进程在重投响应前
    # 退出，下一实例只能 fail closed，绝不能再发第二次 qsub/sbatch。
    record = _start_job_action(
        job_dir, 'continue', operation_key,
        source_job_id, request=request, request_sha256=request_sha256,
        intent_sha256=intent_sha256)

    # 本地也留证:备份旧 POSCAR,用 CONTCAR 覆盖(保持本地目录与远端一致)
    local_poscar = os.path.join(job_dir, 'POSCAR')
    local_poscar_existed = os.path.isfile(local_poscar)
    try:
        if local_poscar_existed:
            shutil.copyfile(local_poscar, f'{local_poscar}.bak{rounds + 1}')
        with open(local_poscar, 'w', encoding='utf-8', newline='') as f:
            f.write(contcar)

        # 远端:CONTCAR→POSCAR + 清混合历史,再重投同一脚本(INCAR 不动)
        cleanup = _restart_cleanup_command(m, job_dir)
        archive_command, archive_dir = _restart_archive_command(
            rounds + 1, backup_inputs=('POSCAR',), promote_contcar=True)
        dialect = get_dialect(profile.scheduler)
        submit_command = dialect.submit_cmd(
            posixpath.join(remote, SCRIPT_NAME),
            getattr(profile, 'scheduler_bin', ''))
        contcar_sha256 = request['contcar_source_sha256']
        command = ' && '.join((
            f'cd {shlex.quote(remote)}',
            _remote_sha256_guard('INCAR', incar_source_sha256),
            _remote_sha256_guard('CONTCAR', contcar_sha256),
            archive_command,
            cleanup,
            _remote_sha256_guard('INCAR', incar_source_sha256),
            _remote_sha256_guard('POSCAR', contcar_sha256),
            submit_command,
        ))
        out, err = run_cmd(client, command, check=True)
    except Exception as exc:  # noqa: BLE001 - remote mutation may be partial
        _mark_job_action_unknown(job_dir, record, str(exc))
        raise UnknownRemoteJobOperation(
            '续算远端事务中断，远端目录或调度器结果未知；已禁止自动重试，请人工核对。',
            action='continue', recovery_status=record.get('status') or 'prepared',
            scheduler_job_id=str(m.get('scheduler_job_id') or '')) from exc

    job_id = dialect.parse_job_id(out)
    if not job_id:
        detail = (out or err).strip()[:300]
        _mark_job_action_unknown(job_dir, record, detail)
        raise UnknownRemoteJobOperation(
            f'续算重投未返回可核验作业号（{dialect.name}：{detail}）；'
            '远端是否受理未知，已禁止自动重试，请人工核对。',
            action='continue', recovery_status=record.get('status') or 'prepared')

    try:
        _update_job_action(
            job_dir, record, 'remote_accepted', result_job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - prepared record still blocks retry
        raise UnknownRemoteJobOperation(
            '续算已返回新作业号，但 journal 未能推进；已禁止自动重试，请人工核对。',
            action='continue', recovery_status='prepared',
            scheduler_job_id=str(job_id)) from exc

    prev = m.get('scheduler_job_id')
    m['scheduler_job_id'] = job_id
    _clear_fetch_evidence(m)
    # 消费掉上一轮的终态诊断:新作业尚未诊断,restartable=True 不能被下一次误用
    m.setdefault('results', {}).pop('diagnosis', None)
    m.setdefault('results', {})['continue_rounds'] = rounds + 1
    _set_continue_baseline(
        m, _base_outcar_mtime, outputs_archived=True)
    m.setdefault('results', {})['continue_archive'] = {
        'remote_dir': posixpath.join(remote, archive_dir),
        'round': rounds + 1,
        'files': ['OUTCAR', 'OSZICAR', 'vasprun.xml', 'CONTCAR', 'XDATCAR'],
    }
    attempt = {
        'n': len(m.get('attempts') or []) + 1,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': 'continued',
        'action': 'contcar_restart',
        'prev_job_id': prev,
        'job_id': job_id,
        'round': rounds + 1,
        'operation_transaction_id': record['transaction_id'],
        'operation_request_sha256': request_sha256,
        'operation_intent_sha256': intent_sha256,
        'incar_sha256': authorized_incar_sha256,
        'source_attempt_token': request['source_attempt_token'],
        'source_authority_transaction_id': request[
            'source_authority_transaction_id'],
        'profile_fingerprint': request['profile_fingerprint'],
        'remote_dir_sha256': request['remote_dir_sha256'],
    }
    if manual_round_override:
        attempt['round_limit_override'] = 'manual-explicit'
    if operation_key:
        attempt['idempotency_key'] = operation_key
    _set_incar_authority(
        m, scheduler_job_id=str(job_id),
        incar_sha256=authorized_incar_sha256,
        transaction_id=record['transaction_id'])
    token_manifest = dict(m)
    token_manifest['attempts'] = list(m.get('attempts') or []) + [attempt]
    attempt['attempt_token'] = current_attempt_token(token_manifest)
    m.setdefault('attempts', []).append(attempt)
    override_note = ',人工确认超出自动上限' if manual_round_override else ''
    manifest_mod.set_state(
        m, 'SUBMITTED',
        note=(f'CONTCAR 续算 第{rounds + 1}轮(prev {prev} → {job_id},'
              f'INCAR 冻结{override_note})'))
    try:
        manifest_mod.save_manifest(job_dir, m)
    except Exception as exc:  # noqa: BLE001 - remote_accepted journal is authoritative gate
        raise UnknownRemoteJobOperation(
            '续算已被调度器受理，但本地 job.yaml 未能安全持久化；'
            '已禁止自动重试，请人工恢复。',
            action='continue', recovery_status='remote_accepted',
            scheduler_job_id=str(job_id)) from exc
    try:
        _update_job_action(
            job_dir, record, 'succeeded', result_job_id=str(job_id), message='')
    except OSError:
        # job.yaml carries the exact transaction id and can reconcile this
        # remote_accepted record after restart without another scheduler call.
        pass
    return m


# ── 改参续算(S6 修复引擎):白名单键受控修改 INCAR 后重投 ─────────────────────
# 只放非方法学"数值旋钮":收敛算法/展宽/混合/步长/迭代上限/能带数。ENCUT/泛函/
# IVDW/ISPIN/赝势这类**决定可比性**的键绝不进白名单(方法学主权不破)。
INCAR_TUNE_WHITELIST = frozenset({
    'ALGO', 'ISMEAR', 'SIGMA', 'AMIX', 'BMIX', 'AMIX_MAG', 'BMIX_MAG', 'IMIX',
    'POTIM', 'IBRION', 'NELM', 'NELMIN', 'NSW', 'NBANDS', 'LREAL', 'ISYM',
    'SYMPREC', 'AMIN', 'MAXMIX', 'NCORE', 'KPAR', 'EDIFF',
})
_TUNE_BANNER = '# --- vcstudio 改参续算 第{round}轮 {at} ---'


@_serialized_job_argument(3, '改参续算')
def continue_with_incar_changes(client, sftp, profile, job_dir: str,
                                changes: dict,
                                max_rounds: int | None = CONTINUE_MAX_ROUNDS,
                                restart_from_contcar: bool = True, *,
                                idempotency_key: str | None = None) -> dict:
    """诊断建议 → 受控改参重投:白名单键追加覆盖到 INCAR 文末(原文一字不删),
    可选 CONTCAR→POSCAR,清 WAVECAR/CHGCAR（bands 保留 CHGCAR）,重投同一脚本。

    与冻结续算共用状态门；默认调用仍有三轮上限，而服务端确认的人工入口可传
    ``max_rounds=None`` 仅放宽轮次。其余差异:
    - changes 仅允许 INCAR_TUNE_WHITELIST 键(违例 ValueError 点名,绝不静默丢弃);
    - 放宽 restartable 限制:SCF_SLOSHING/EDDDAV 等 NEEDS_HUMAN 类正是改参对象,
      故只要求终态(不在队/不在跑),不要求 diagnose.restartable;
    - INCAR 修改以"追加覆盖块"落地(VASP 取同键末次出现值;原文保留可审计),
      同步上传远端;attempts 记录完整 changes。
    """
    operation_key = _validate_job_action_key(idempotency_key)
    if not changes:
        raise ValueError('未提供任何 INCAR 修改项')
    canonical_changes: dict[str, str] = {}
    for key, value in changes.items():
        canonical_key = str(key).upper()
        if canonical_key in canonical_changes:
            raise ValueError(f'重复的 INCAR 修改键:{canonical_key}')
        canonical_value = str(value)
        if (not canonical_value.strip() or len(canonical_value) > 256
                or any(marker in canonical_value
                       for marker in ('\r', '\n', '\x00', ';', '#', '!'))):
            raise ValueError(
                f'INCAR 修改值 {canonical_key} 必须是非空单值文本、不得含标签分隔符或注释符，'
                '且不超过 256 字符')
        canonical_changes[canonical_key] = canonical_value
    bad = [key for key in canonical_changes if key not in INCAR_TUNE_WHITELIST]
    if bad:
        raise ValueError(
            f'以下键不在改参白名单,拒绝修改:{", ".join(bad)}。'
            f'白名单(非方法学旋钮):{", ".join(sorted(INCAR_TUNE_WHITELIST))}')
    m = manifest_mod.load_manifest(job_dir)
    if m is None:
        raise ValueError('作业目录缺 job.yaml,无法改参续算')
    if _job_engine(m) != 'vasp':
        contract = get_run_contract(_job_engine(m))
        raise ValueError(
            f'{_ENGINE_LABELS.get(_job_engine(m), _job_engine(m))} 不能使用 INCAR 改参续算；'
            f'{contract.restart_note}')
    assert_profile_binding(profile, job_dir, '改参续算', manifest=m)
    if _is_neb(m):
        raise ValueError(
            'NEB 不能使用通用改参/CONTCAR 续算；请使用 NEB 专用 image 级重提流程')
    rounds = int((m.get('results') or {}).get('continue_rounds', 0))
    source_job_id = _require_source_scheduler_job_id(m, '改参重投')
    authorized_incar_sha256 = _current_incar_authority(m, source_job_id)
    manual_round_override = max_rounds is None and rounds >= CONTINUE_MAX_ROUNDS

    # The request is path-free but binds every user decision and the exact source
    # scheduler generation.  It is fsynced before INCAR/POSCAR or remote state is
    # changed, so a restarted client cannot reuse the operation id for a different
    # set of tuning choices.
    intent = {
        'changes': dict(sorted(canonical_changes.items())),
        'restart_from_contcar': bool(restart_from_contcar),
        'round_policy': ('manual-unbounded' if max_rounds is None
                         else f'bounded-{int(max_rounds)}'),
    }
    intent_sha256 = _job_action_request_sha256(intent)
    request = {
        'schema': 'vcstudio.tune-continuation-request/v1',
        'manifest_job_id': str(m.get('job_id') or ''),
        'source_scheduler_job_id': source_job_id,
        'source_state': str(m.get('state') or ''),
        'source_round': rounds,
        'profile_fingerprint': str(profile_binding(profile)['fingerprint']),
        'intent': intent,
        'round_limit_override': (
            'manual-explicit' if manual_round_override else None),
    }
    replay = _reconcile_job_action(
        job_dir, m, 'tune_continue', operation_key,
        intent_sha256=intent_sha256)
    if replay is not None:
        return replay

    _require_continue_terminal_state(m, '改参重投')
    if max_rounds is not None and rounds >= max_rounds:
        raise RuntimeError(f'已续算 {rounds} 次达上限 {max_rounds},停机交人工(防死循环)')
    remote = m.get('remote_dir')
    if not remote:
        raise ValueError('该作业无 remote_dir(未提交过),无法改参续算')
    request['source_attempt_token'] = current_attempt_token(m)
    request['source_authority_transaction_id'] = str(
        (m.get('execution_authority') or {}).get('transaction_id') or '')
    request['remote_dir_sha256'] = hashlib.sha256(
        str(remote).encode('utf-8')).hexdigest()

    # Requested CONTCAR promotion is mandatory, not best-effort.  Validate both
    # syntax and geometry before the durable transaction or any local/remote file
    # is changed; otherwise an audit row could falsely claim a CONTCAR restart.
    contcar = ''
    promoted_contcar = False
    if restart_from_contcar:
        contcar = _read_remote_text(client, posixpath.join(remote, 'CONTCAR'))
        if not diagnose.valid_poscar(contcar):
            raise RuntimeError(
                '远端 CONTCAR 缺失或不完整,不能按请求改参续算；'
                '如需保留原 POSCAR，请取消“从 CONTCAR 续算结构”后重新确认')
        min_d = _contcar_min_distance(contcar)
        if min_d is None:
            raise RuntimeError('远端 CONTCAR 几何无法可靠解析,不能改参续算')
        if min_d < MIN_INTERATOMIC_OK:
            raise RuntimeError(
                f'CONTCAR 存在原子重叠(最小间距 {min_d:.2f} Å),'
                '疑似几何病态,请人工检查')
        promoted_contcar = True

    # Read every local source before the fsynced transaction, but do not mutate it.
    local_incar = os.path.join(job_dir, 'INCAR')
    local_incar_existed = os.path.isfile(local_incar)
    if not local_incar_existed:
        raise ValueError('本地作业目录缺 INCAR')
    with open(local_incar, 'r', encoding='utf-8', errors='replace') as f:
        incar_text = f.read()
    local_incar_sha256 = manifest_mod.sha256_file(local_incar)
    if local_incar_sha256 != authorized_incar_sha256:
        raise ValueError(
            '本地 INCAR 已偏离上一调度器代次的权威摘要；'
            '不能在未知方法基线上改参续算，请先恢复或重新准备')
    at = time.strftime('%Y-%m-%dT%H:%M:%S')
    block = '\n' + _TUNE_BANNER.format(round=rounds + 1, at=at) + '\n'
    block += ''.join(f'{key} = {value}\n'
                     for key, value in canonical_changes.items())
    new_text = (incar_text if incar_text.endswith('\n') else incar_text + '\n') + block
    local_poscar = os.path.join(job_dir, 'POSCAR')
    local_poscar_existed = os.path.isfile(local_poscar)
    if not promoted_contcar and not local_poscar_existed:
        raise ValueError(
            '本地作业目录缺 POSCAR，无法绑定未提升结构的实际输入，不能改参续算')

    incar_target_sha256 = hashlib.sha256(
        new_text.encode('utf-8')).hexdigest()
    poscar_target_sha256 = (
        hashlib.sha256(contcar.encode('utf-8')).hexdigest()
        if promoted_contcar else manifest_mod.sha256_file(local_poscar))

    # 续算沉降基线:重投前记下上一轮 OUTCAR 的 mtime(此刻新作业尚未启动,仍是旧文件)
    _o0, _z0, _base_outcar_mtime = _stat_outcar_full(client, remote)

    request['incar_source_sha256'] = local_incar_sha256
    request['authorized_incar_sha256'] = authorized_incar_sha256
    request['incar_target_sha256'] = incar_target_sha256
    request['poscar_target_sha256'] = poscar_target_sha256
    if promoted_contcar:
        request['contcar_source_sha256'] = hashlib.sha256(
            contcar.encode('utf-8')).hexdigest()
    request_sha256 = _job_action_request_sha256(request)
    record = _start_job_action(
        job_dir, 'tune_continue', operation_key,
        source_job_id, request=request,
        request_sha256=request_sha256, intent_sha256=intent_sha256)

    try:
        shutil.copyfile(local_incar, f'{local_incar}.bak{rounds + 1}')
        with open(local_incar, 'w', encoding='utf-8', newline='') as f:
            f.write(new_text)
        if promoted_contcar:
            if local_poscar_existed:
                shutil.copyfile(local_poscar, f'{local_poscar}.bak{rounds + 1}')
            with open(local_poscar, 'w', encoding='utf-8', newline='') as f:
                f.write(contcar)

        archive_command, archive_dir = _restart_archive_command(
            rounds + 1, backup_inputs=('INCAR', 'POSCAR'),
            promote_contcar=promoted_contcar)
        pre_archive_guard = _remote_sha256_guard(
            'CONTCAR' if promoted_contcar else 'POSCAR',
            request.get('contcar_source_sha256') if promoted_contcar
            else poscar_target_sha256)
        run_cmd(
            client,
            f'cd {shlex.quote(remote)} && {pre_archive_guard} && '
            f'{archive_command} && {_restart_cleanup_command(m, job_dir)}',
            check=True)
        sftp.put(local_incar, posixpath.join(remote, 'INCAR'))
        dialect = get_dialect(profile.scheduler)
        submit_command = dialect.submit_cmd(
            posixpath.join(remote, SCRIPT_NAME),
            getattr(profile, 'scheduler_bin', ''))
        out, err = run_cmd(
            client, ' && '.join((
                f'cd {shlex.quote(remote)}',
                _remote_sha256_guard('INCAR', incar_target_sha256),
                _remote_sha256_guard('POSCAR', poscar_target_sha256),
                submit_command,
            )), check=True)
    except Exception as exc:  # noqa: BLE001 - remote mutation may be partial
        _mark_job_action_unknown(job_dir, record, str(exc))
        raise UnknownRemoteJobOperation(
            '改参续算远端事务中断，输入目录或调度器结果未知；'
            '已禁止自动重试，请人工核对。',
            action='tune_continue',
            recovery_status=record.get('status') or 'prepared',
            scheduler_job_id=str(m.get('scheduler_job_id') or '')) from exc

    job_id = dialect.parse_job_id(out)
    if not job_id:
        detail = (out or err).strip()[:300]
        _mark_job_action_unknown(job_dir, record, detail)
        raise UnknownRemoteJobOperation(
            f'改参续算未返回可核验作业号（{dialect.name}：{detail}）；'
            '远端是否受理未知，已禁止自动重试，请人工核对。',
            action='tune_continue',
            recovery_status=record.get('status') or 'prepared')

    try:
        _update_job_action(
            job_dir, record, 'remote_accepted', result_job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - prepared record still blocks retry
        raise UnknownRemoteJobOperation(
            '改参续算已返回新作业号，但 journal 未能推进；'
            '已禁止自动重试，请人工核对。',
            action='tune_continue', recovery_status='prepared',
            scheduler_job_id=str(job_id)) from exc

    prev = m.get('scheduler_job_id')
    m['scheduler_job_id'] = job_id
    _clear_fetch_evidence(m)
    m.setdefault('results', {}).pop('diagnosis', None)
    m.setdefault('results', {})['continue_rounds'] = rounds + 1
    _set_continue_baseline(
        m, _base_outcar_mtime, outputs_archived=True)
    m.setdefault('results', {})['continue_archive'] = {
        'remote_dir': posixpath.join(remote, archive_dir),
        'round': rounds + 1,
        'files': ['OUTCAR', 'OSZICAR', 'vasprun.xml', 'CONTCAR', 'XDATCAR'],
    }
    attempt = {
        'n': len(m.get('attempts') or []) + 1,
        'at': at,
        'result': 'continued',
        'action': 'incar_tuned_restart',
        'incar_changes': canonical_changes,
        'from_contcar_requested': bool(restart_from_contcar),
        'from_contcar': promoted_contcar,
        'prev_job_id': prev,
        'job_id': job_id,
        'round': rounds + 1,
        'operation_transaction_id': record['transaction_id'],
        'operation_request_sha256': request_sha256,
        'operation_intent_sha256': intent_sha256,
        'incar_sha256': incar_target_sha256,
        'source_attempt_token': request['source_attempt_token'],
        'source_authority_transaction_id': request[
            'source_authority_transaction_id'],
        'profile_fingerprint': request['profile_fingerprint'],
        'remote_dir_sha256': request['remote_dir_sha256'],
    }
    if manual_round_override:
        attempt['round_limit_override'] = 'manual-explicit'
    if operation_key:
        attempt['idempotency_key'] = operation_key
    _set_incar_authority(
        m, scheduler_job_id=str(job_id),
        incar_sha256=incar_target_sha256,
        transaction_id=record['transaction_id'])
    token_manifest = dict(m)
    token_manifest['attempts'] = list(m.get('attempts') or []) + [attempt]
    attempt['attempt_token'] = current_attempt_token(token_manifest)
    m.setdefault('attempts', []).append(attempt)
    override_note = ',人工确认超出自动上限' if manual_round_override else ''
    manifest_mod.set_state(
        m, 'SUBMITTED',
        note=f'改参续算 第{rounds + 1}轮({", ".join(f"{k}={v}" for k, v in canonical_changes.items())};'
             f'prev {prev} → {job_id}{override_note})')
    try:
        manifest_mod.save_manifest(job_dir, m)
    except Exception as exc:  # noqa: BLE001 - remote_accepted journal blocks retry
        raise UnknownRemoteJobOperation(
            '改参续算已被调度器受理，但本地 job.yaml 未能安全持久化；'
            '已禁止自动重试，请人工恢复。',
            action='tune_continue', recovery_status='remote_accepted',
            scheduler_job_id=str(job_id)) from exc
    try:
        _update_job_action(
            job_dir, record, 'succeeded', result_job_id=str(job_id), message='')
    except OSError:
        pass
    return m


def _read_oszicar(client, remote_dir: str):
    """OSZICAR 尾部一次取数 → (E0|None, tail_text)。

    tail -150 覆盖最后一个离子步的完整 SCF 块(供 diagnose 震荡扫描)且必含末行
    E0(取最后一个匹配)。拿不到 E0 → None(绝不编数)。"""
    if not remote_dir:
        return None, ''
    out, _ = run_cmd(client, f'tail -n 150 {shlex.quote(remote_dir + "/OSZICAR")} 2>/dev/null')
    m = None
    for m in _E0_RE.finditer(out):
        pass                                   # 取最后一个匹配
    if m is None:
        return None, out
    try:
        return float(m.group(1)), out
    except ValueError:
        return None, out


def _read_e0(client, remote_dir: str):
    """兼容入口:只要 E0。"""
    return _read_oszicar(client, remote_dir)[0]
