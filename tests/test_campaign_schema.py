"""campaign.schema 测试:构造/校验/环依赖/原子写/revision CAS/round-trip。"""
import copy
import multiprocessing
import threading

import pytest
import yaml

from vcstudio.campaign import schema


def _process_create_task(campaign_dir, writer, start, output):
    """Spawn-safe create-only contender used by the cross-process CAS regression."""
    try:
        if not start.wait(10):
            raise TimeoutError('create race start timed out')
        task = schema.new_task('raced-create', 'relax')
        task['writer'] = writer
        schema.save_task(campaign_dir, task)
        output.put(('won', writer))
    except schema.RevisionConflictError as exc:
        output.put(('conflict', exc.expected_revision, str(exc)))
    except BaseException as exc:  # pragma: no cover - reported to parent assertion
        output.put(('error', type(exc).__name__, str(exc)))


def _process_init_campaign(base, writer, start, output):
    """Spawn-safe same-ID campaign initializer used by the process CAS regression."""
    try:
        if not start.wait(10):
            raise TimeoutError('campaign race start timed out')
        campaign = schema.init_campaign(
            base, 'raced-campaign', title=writer, status='active',
            tasks=[schema.new_task(f'task-{writer}', 'relax')])
        output.put(('won', writer, campaign['meta']['title']))
    except schema.CampaignConflictError as exc:
        output.put(('conflict', exc.campaign_id, str(exc)))
    except BaseException as exc:  # pragma: no cover - reported to parent assertion
        output.put(('error', type(exc).__name__, str(exc)))


def _two_tasks():
    return [schema.new_task('a', 'relax'),
            schema.new_task('b', 'static', depends_on=['a'])]


def test_init_and_load_roundtrip(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=_two_tasks(),
                                title='演示', budget_core_hours=500.0)
    assert (tmp_path / '.vcstudio' / 'campaign' / 'demo' / 'campaign.yaml').is_file()
    loaded = schema.load_campaign(camp['dir'])
    assert loaded is not None
    assert loaded['meta']['id'] == 'demo'
    assert loaded['meta']['budget_core_hours'] == 500.0
    assert [t['id'] for t in loaded['tasks']] == ['a', 'b']
    schema.validate_campaign(loaded)


def test_init_campaign_is_create_only_and_preserves_trusted_existing_bytes(tmp_path):
    trusted = schema.init_campaign(
        str(tmp_path), 'demo', title='trusted', status='active',
        tasks=[schema.new_task('trusted-task', 'relax')])
    path = tmp_path / '.vcstudio' / 'campaign' / 'demo' / 'campaign.yaml'
    before = path.read_bytes()

    with pytest.raises(schema.CampaignConflictError, match='创建冲突.*create-only'):
        schema.init_campaign(
            str(tmp_path), 'demo', title='attacker-controlled', status='draft',
            tasks=[schema.new_task('attacker-task', 'relax')])

    assert path.read_bytes() == before
    loaded = schema.load_campaign(trusted['dir'])
    assert loaded['meta']['title'] == 'trusted'
    assert loaded['meta']['status'] == 'active'
    assert [task['id'] for task in loaded['tasks']] == ['trusted-task']


def test_init_campaign_never_overwrites_corrupt_existing_campaign_yaml(tmp_path):
    cdir = schema.campaign_dir(str(tmp_path), 'demo')
    cdir.mkdir(parents=True)
    path = cdir / schema.CAMPAIGN_YAML
    corrupt = b'\xff\xfe\x00attacker-controlled'
    path.write_bytes(corrupt)

    with pytest.raises(schema.CampaignConflictError, match='已存在'):
        schema.init_campaign(str(tmp_path), 'demo', title='replacement')

    assert path.read_bytes() == corrupt
    assert schema.load_campaign(cdir) is None


def test_init_campaign_publish_failure_cleans_unique_temp_and_allows_retry(
        tmp_path, monkeypatch):
    def fail_link(_source, _destination):
        raise OSError('simulated no-replace publish failure')

    monkeypatch.setattr(schema.os, 'link', fail_link)
    with pytest.raises(OSError, match='no-replace publish failure'):
        schema.init_campaign(str(tmp_path), 'demo', title='first attempt')

    cdir = schema.campaign_dir(str(tmp_path), 'demo')
    assert not (cdir / schema.CAMPAIGN_YAML).exists()
    assert list(cdir.glob('.campaign.yaml.*.tmp')) == []
    assert (cdir / '.campaign.yaml.cas.lock').is_file()

    monkeypatch.undo()
    campaign = schema.init_campaign(str(tmp_path), 'demo', title='clean retry')
    assert schema.load_campaign(campaign['dir'])['meta']['title'] == 'clean retry'


def test_two_threads_init_same_campaign_have_one_winner_and_readable_yaml(tmp_path):
    start = threading.Barrier(2)
    outcomes = []

    def initialize(writer):
        start.wait()
        try:
            campaign = schema.init_campaign(
                str(tmp_path), 'raced-campaign', title=writer, status='active',
                tasks=[schema.new_task(f'task-{writer}', 'relax')])
            outcomes.append(('won', writer, campaign['meta']['title']))
        except schema.CampaignConflictError as exc:
            outcomes.append(('conflict', exc.campaign_id))

    threads = [threading.Thread(target=initialize, args=(f'thread-{index}',))
               for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(row[0] for row in outcomes) == ['conflict', 'won']
    winner = next(row[1] for row in outcomes if row[0] == 'won')
    cdir = schema.campaign_dir(str(tmp_path), 'raced-campaign')
    path = cdir / schema.CAMPAIGN_YAML
    parsed = yaml.safe_load(path.read_text(encoding='utf-8'))
    loaded = schema.load_campaign(cdir)
    assert parsed == loaded['meta']
    assert parsed['title'] == winner
    assert [task['id'] for task in loaded['tasks']] == [f'task-{winner}']
    assert (cdir / '.campaign.yaml.cas.lock').is_file()


def test_two_spawn_processes_init_same_campaign_have_one_winner(tmp_path):
    context = multiprocessing.get_context('spawn')
    start = context.Event()
    output = context.Queue()
    processes = [context.Process(
        target=_process_init_campaign,
        args=(str(tmp_path), f'process-{index}', start, output),
    ) for index in range(2)]
    for process in processes:
        process.start()
    start.set()
    outcomes = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        if process.is_alive():  # pragma: no cover - cleanup for failed synchronization
            process.terminate()
            process.join(5)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(row[0] for row in outcomes) == ['conflict', 'won'], outcomes
    winner = next(row[1] for row in outcomes if row[0] == 'won')
    cdir = schema.campaign_dir(str(tmp_path), 'raced-campaign')
    path = cdir / schema.CAMPAIGN_YAML
    parsed = yaml.safe_load(path.read_text(encoding='utf-8'))
    loaded = schema.load_campaign(cdir)
    assert parsed == loaded['meta']
    assert parsed['title'] == winner
    assert [task['id'] for task in loaded['tasks']] == [f'task-{winner}']


def test_new_campaign_rejects_empty_id():
    with pytest.raises(ValueError, match='campaign id 不能为空'):
        schema.new_campaign('')


def test_validate_missing_field():
    meta = schema.new_campaign('x')
    del meta['status']
    with pytest.raises(ValueError, match='缺少必需字段: status'):
        schema.validate_campaign_meta(meta)


def test_validate_task_illegal_kind_and_rung():
    with pytest.raises(ValueError, match='kind 非法'):
        schema.validate_task(schema.new_task('t', 'bogus'))
    bad = schema.new_task('t', 'relax')
    bad['rung'] = 'nope'
    with pytest.raises(ValueError, match='rung 非法'):
        schema.validate_task(bad)


def test_validate_graph_missing_dependency():
    tasks = [schema.new_task('a', 'relax', depends_on=['ghost'])]
    with pytest.raises(ValueError, match='依赖不存在的任务: ghost'):
        schema.validate_graph(tasks)


def test_validate_graph_cycle_detected():
    tasks = [schema.new_task('a', 'relax', depends_on=['c']),
             schema.new_task('b', 'static', depends_on=['a']),
             schema.new_task('c', 'static', depends_on=['b'])]
    with pytest.raises(ValueError, match='循环依赖'):
        schema.validate_graph(tasks)


def test_validate_graph_duplicate_id():
    tasks = [schema.new_task('a', 'relax'), schema.new_task('a', 'static')]
    with pytest.raises(ValueError, match='重复任务 id'):
        schema.validate_graph(tasks)


def test_add_task_dedup_and_persist(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[schema.new_task('a', 'relax')])
    schema.add_task(camp, schema.new_task('b', 'static', depends_on=['a']))
    assert [t['id'] for t in camp['tasks']] == ['a', 'b']
    # 追加即持久化:重新读盘可见新任务
    assert [t['id'] for t in schema.load_campaign(camp['dir'])['tasks']] == ['a', 'b']
    with pytest.raises(ValueError, match='任务 id 重复'):
        schema.add_task(camp, schema.new_task('a', 'relax'))
    # 追加一个依赖不存在的任务应被依赖图校验挡下
    with pytest.raises(ValueError, match='依赖不存在的任务: ghost'):
        schema.add_task(camp, schema.new_task('c', 'static', depends_on=['ghost']))


def test_init_campaign_rejects_cycle_before_write(tmp_path):
    tasks = [schema.new_task('a', 'relax', depends_on=['b']),
             schema.new_task('b', 'static', depends_on=['a'])]
    with pytest.raises(ValueError, match='循环依赖'):
        schema.init_campaign(str(tmp_path), 'bad', tasks=tasks)
    # 校验失败发生在落盘前:目录不该留下 campaign.yaml
    assert not (tmp_path / '.vcstudio' / 'campaign' / 'bad' / 'campaign.yaml').is_file()


def test_atomic_write_preserves_original_on_failure(tmp_path, monkeypatch):
    """写坏中途(os.replace 抛错)不损坏原文件:仍能读回旧内容(原子性证明)。"""
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax')], title='原始')
    cdir = camp['dir']
    assert schema.load_campaign(cdir)['meta']['title'] == '原始'

    def _boom(src, dst):
        raise OSError('模拟落盘中断')

    monkeypatch.setattr(schema.os, 'replace', _boom)
    new_meta = dict(camp['meta'])
    new_meta['title'] = '损坏写入'
    with pytest.raises(OSError, match='模拟落盘中断'):
        schema.save_campaign_meta(cdir, new_meta)

    monkeypatch.undo()
    # 原文件完好,标题仍是旧值,而非半个文件
    reloaded = schema.load_campaign(cdir)
    assert reloaded is not None
    assert reloaded['meta']['title'] == '原始'


def test_load_missing_returns_none(tmp_path):
    assert schema.load_campaign(str(tmp_path / 'nope')) is None


def test_fingerprint_file_roundtrip(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax')],
                                fingerprint={'functional': 'PBE', 'encut': 500.0})
    fp = schema.load_fingerprint(camp['dir'])
    assert fp['functional'] == 'PBE'


def test_task_revision_cas_rejects_stale_writer_without_clobber(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[schema.new_task('a', 'relax')])
    first = schema.load_task(camp['dir'], 'a')
    stale = copy.deepcopy(first)
    first['rung'] = 'running'
    schema.persist_task(camp['dir'], first, expected_revision=0)
    assert first['revision'] == 1

    stale['rung'] = 'failed'
    with pytest.raises(schema.RevisionConflictError, match='revision 冲突'):
        schema.persist_task(camp['dir'], stale, expected_revision=0)
    disk = schema.load_task(camp['dir'], 'a')
    assert disk['revision'] == 1 and disk['rung'] == 'running'


def test_existing_task_cannot_use_legacy_unconditional_save(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[schema.new_task('a', 'relax')])
    task = schema.load_task(camp['dir'], 'a')
    task['rung'] = 'running'
    with pytest.raises(ValueError, match='expected_revision'):
        schema.save_task(camp['dir'], task)
    assert schema.load_task(camp['dir'], 'a')['rung'] == 'pending'


def test_legacy_task_without_revision_migrates_as_revision_zero(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[schema.new_task('a', 'relax')])
    path = schema.task_path(camp['dir'], 'a')
    text = path.read_text(encoding='utf-8').replace('revision: 0\n', '')
    path.write_text(text, encoding='utf-8')
    legacy = schema.load_task(camp['dir'], 'a')
    assert legacy['revision'] == 0
    legacy['rung'] = 'running'
    schema.persist_task(camp['dir'], legacy, expected_revision=0)
    assert schema.load_task(camp['dir'], 'a')['revision'] == 1


def test_two_task_claims_have_exactly_one_winner_and_yaml_remains_readable(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[schema.new_task('a', 'relax')])
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(owner):
        barrier.wait()
        try:
            claimed = schema.claim_task(
                camp['dir'], 'a', expected_revision=0, owner=owner)
            outcomes.append(('won', claimed['revision'], claimed['claim']['owner']))
        except (schema.RevisionConflictError, schema.TaskClaimError) as exc:
            outcomes.append(('lost', type(exc).__name__))

    threads = [threading.Thread(target=worker, args=(f'runner-{i}',)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert [row[0] for row in outcomes].count('won') == 1
    assert [row[0] for row in outcomes].count('lost') == 1
    disk = schema.load_task(camp['dir'], 'a')
    assert disk is not None and disk['rung'] == 'running' and disk['revision'] == 1


def test_two_threads_create_same_task_have_one_cas_winner_and_readable_yaml(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo')
    start = threading.Barrier(2)
    outcomes = []

    def writer(name):
        task = schema.new_task('raced-create', 'relax')
        task['writer'] = name
        start.wait()
        try:
            schema.save_task(camp['dir'], task)
            outcomes.append(('won', name))
        except schema.RevisionConflictError as exc:
            outcomes.append(('conflict', exc.expected_revision))

    threads = [threading.Thread(target=writer, args=(f'thread-{index}',))
               for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(row[0] for row in outcomes) == ['conflict', 'won']
    assert next(row for row in outcomes if row[0] == 'conflict')[1] is None
    path = schema.task_path(camp['dir'], 'raced-create')
    parsed = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert parsed == schema.load_task(camp['dir'], 'raced-create')
    assert parsed['writer'] in {'thread-0', 'thread-1'}


def test_two_spawn_processes_create_same_task_have_one_cas_winner(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo')
    context = multiprocessing.get_context('spawn')
    start = context.Event()
    output = context.Queue()
    processes = [context.Process(
        target=_process_create_task,
        args=(camp['dir'], f'process-{index}', start, output),
    ) for index in range(2)]
    for process in processes:
        process.start()
    start.set()
    outcomes = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        if process.is_alive():  # pragma: no cover - cleanup for failed synchronization
            process.terminate()
            process.join(5)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(row[0] for row in outcomes) == ['conflict', 'won'], outcomes
    assert next(row for row in outcomes if row[0] == 'conflict')[1] is None
    path = schema.task_path(camp['dir'], 'raced-create')
    parsed = yaml.safe_load(path.read_text(encoding='utf-8'))
    assert parsed == schema.load_task(camp['dir'], 'raced-create')
    assert parsed['writer'] in {'process-0', 'process-1'}


def test_atomic_yaml_uses_unique_temporary_files_and_cleans_failure(tmp_path, monkeypatch):
    camp = schema.init_campaign(str(tmp_path), 'demo', tasks=[schema.new_task('a', 'relax')])
    task = schema.load_task(camp['dir'], 'a')

    def boom(src, dst):
        raise OSError('replace failed')

    monkeypatch.setattr(schema.os, 'replace', boom)
    task['rung'] = 'running'
    with pytest.raises(OSError, match='replace failed'):
        schema.persist_task(camp['dir'], task, expected_revision=0)
    task_dir = schema.task_path(camp['dir'], 'a').parent
    assert list(task_dir.glob('.a.yaml.*.tmp')) == []
    monkeypatch.undo()
    assert schema.load_task(camp['dir'], 'a')['rung'] == 'pending'
