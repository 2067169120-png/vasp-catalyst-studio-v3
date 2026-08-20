"""任务登记表测试:登记/去重/移除/载入(路径注入 tmp,不碰真实 %APPDATA%)。"""
import json
import multiprocessing

import pytest

from vcstudio.cluster import ledger
from vcstudio.shared import manifest


def _slow_owned_register(ledger_path, job_dir, transaction_id, entered, release, results):
    original = ledger._save_state

    def slow_save(path, dirs, owners):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release ledger writer")
        return original(path, dirs, owners)

    ledger._save_state = slow_save
    results.put(("method", ledger.register_owned(
        job_dir, transaction_id, path=ledger_path)))


def _ordinary_register(ledger_path, job_dir, ready, results):
    ready.set()
    results.put(("ordinary", ledger.register(job_dir, path=ledger_path)))


def test_register_dedup_unregister_roundtrip(tmp_path):
    lp = tmp_path / 'jobs.json'
    d1, d2 = tmp_path / 'j1', tmp_path / 'j2'
    d1.mkdir()
    d2.mkdir()

    assert ledger.register(d1, path=lp) is True
    assert ledger.register(d1, path=lp) is False          # 去重
    assert ledger.register(d2, path=lp) is True
    assert ledger.list_dirs(path=lp) == [str(d1.resolve()), str(d2.resolve())]

    assert ledger.unregister(d1, path=lp) is True
    assert ledger.unregister(d1, path=lp) is False
    assert ledger.list_dirs(path=lp) == [str(d2.resolve())]


def test_load_all_reads_manifest_or_none(tmp_path):
    lp = tmp_path / 'jobs.json'
    d1 = tmp_path / 'with_manifest'
    d1.mkdir()
    m = manifest.new_manifest(job_id='x', system='s', task_type='relax',
                              calc_type='slab', inputs={})
    manifest.save_manifest(d1, m)
    d2 = tmp_path / 'no_manifest'
    d2.mkdir()
    ledger.register(d1, path=lp)
    ledger.register(d2, path=lp)
    ledger.register(tmp_path / 'gone_dir', path=lp)        # 目录不存在

    rows = ledger.load_all(path=lp)
    assert len(rows) == 3
    assert rows[0][1]['job_id'] == 'x'
    assert rows[1][1] is None and rows[2][1] is None


def test_corrupt_ledger_degrades_to_empty(tmp_path):
    lp = tmp_path / 'jobs.json'
    lp.write_text('{not json', encoding='utf-8')
    assert ledger.list_dirs(path=lp) == []
    assert ledger.register(tmp_path, path=lp) is True      # 可自愈重建


def test_transaction_owner_never_removes_preexisting_entry(tmp_path):
    lp = tmp_path / 'jobs.json'
    job = tmp_path / 'preexisting'
    transaction_id = 'a' * 64

    assert ledger.register(job, path=lp) is True
    assert ledger.register_owned(job, transaction_id, path=lp) == {
        'added': False, 'preexisting': True, 'owned': False,
    }
    assert ledger.unregister_owned(job, transaction_id, path=lp) is False
    assert ledger.list_dirs(path=lp) == [str(job.resolve())]


def test_owned_entry_can_be_withdrawn_or_released_without_touching_others(tmp_path):
    lp = tmp_path / 'jobs.json'
    first, second = tmp_path / 'first', tmp_path / 'second'
    first_id, second_id = 'b' * 64, 'c' * 64

    assert ledger.register_owned(first, first_id, path=lp)['added'] is True
    assert ledger.register_owned(second, second_id, path=lp)['added'] is True
    assert ledger.unregister_owned(first, first_id, path=lp) is True
    assert ledger.release_registration_owner(second, second_id, path=lp) is True
    assert ledger.unregister_owned(second, second_id, path=lp) is False
    assert ledger.list_dirs(path=lp) == [str(second.resolve())]


def test_method_and_ordinary_writers_in_two_processes_preserve_exactly_two_entries(tmp_path):
    context = multiprocessing.get_context('spawn')
    lp = tmp_path / 'jobs.json'
    method_job, ordinary_job = tmp_path / 'method-job', tmp_path / 'ordinary-job'
    transaction_id = 'd' * 64
    entered = context.Event()
    release = context.Event()
    ordinary_ready = context.Event()
    results = context.Queue()

    method_process = context.Process(
        target=_slow_owned_register,
        args=(str(lp), str(method_job), transaction_id, entered, release, results),
    )
    ordinary_process = context.Process(
        target=_ordinary_register,
        args=(str(lp), str(ordinary_job), ordinary_ready, results),
    )
    method_process.start()
    assert entered.wait(10)
    ordinary_process.start()
    assert ordinary_ready.wait(10)
    release.set()
    method_process.join(15)
    ordinary_process.join(15)

    assert method_process.exitcode == ordinary_process.exitcode == 0
    outcomes = dict(results.get(timeout=2) for _ in range(2))
    assert outcomes['method']['added'] is True
    assert outcomes['ordinary'] is True
    assert ledger.list_dirs(path=lp) == [
        str(method_job.resolve()), str(ordinary_job.resolve()),
    ]
    payload = json.loads(lp.read_text(encoding='utf-8'))
    assert payload['_transaction_owners'] == {str(method_job.resolve()): transaction_id}
    assert not list(tmp_path.glob('.jobs.json.*.tmp'))


def test_owned_registration_only_its_transaction_can_rollback(tmp_path):
    lp = tmp_path / 'jobs.json'
    job = tmp_path / 'owned-job'
    job.mkdir()
    owner = 'a' * 32

    first = ledger.register_owned(job, owner, path=lp)
    assert first == {'added': True, 'preexisting': False, 'owned': True}
    assert ledger.register_owned(job, owner, path=lp) == first
    payload = json.loads(lp.read_text(encoding='utf-8'))
    stored_owner = payload['_transaction_owners'][str(job.resolve())]
    assert len(stored_owner) == 64 and stored_owner != owner
    with pytest.raises(RuntimeError, match='another registration transaction'):
        ledger.register_owned(job, 'b' * 32, path=lp)
    assert ledger.unregister_owned(job, 'b' * 32, path=lp) is False
    assert ledger.list_dirs(path=lp) == [str(job.resolve())]
    assert ledger.unregister_owned(job, owner, path=lp) is True
    assert ledger.list_dirs(path=lp) == []


def test_release_owner_is_durable_and_never_removes_registered_job(tmp_path):
    lp = tmp_path / 'jobs.json'
    job = tmp_path / 'released-job'
    job.mkdir()
    owner = 'c' * 32

    ledger.register_owned(job, owner, path=lp)
    assert ledger.release_registration_owner(job, owner, path=lp) is True
    assert ledger.release_registration_owner(job, owner, path=lp) is False
    assert ledger.unregister_owned(job, owner, path=lp) is False
    assert ledger.list_dirs(path=lp) == [str(job.resolve())]
    payload = json.loads(lp.read_text(encoding='utf-8'))
    assert payload == {'job_dirs': [str(job.resolve())]}


def test_ensure_registration_released_is_atomic_and_rejects_another_owner(tmp_path):
    lp = tmp_path / 'jobs.json'
    job = tmp_path / 'candidate'
    job.mkdir()
    owner = 'd' * 32

    assert ledger.register_owned(job, owner, path=lp)['owned'] is True
    assert ledger.ensure_registration_released(job, owner, path=lp) == {
        'present': True, 'owned': False, 'added': False,
        'preexisting': False, 'released': True,
    }
    assert ledger.ensure_registration_released(job, owner, path=lp) == {
        'present': True, 'owned': False, 'added': False,
        'preexisting': True, 'released': False,
    }

    other = tmp_path / 'other-candidate'
    other.mkdir()
    ledger.register_owned(other, 'e' * 64, path=lp)
    with pytest.raises(RuntimeError, match='another registration transaction'):
        ledger.ensure_registration_released(other, 'f' * 32, path=lp)
    assert ledger.registration_state(other, 'e' * 64, path=lp)['owned'] is True
