"""campaign.schema 测试:构造/校验/环依赖/原子写/round-trip。"""
import pytest

from vcstudio.campaign import schema


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
