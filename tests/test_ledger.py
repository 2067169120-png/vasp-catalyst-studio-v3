"""任务登记表测试:登记/去重/移除/载入(路径注入 tmp,不碰真实 %APPDATA%)。"""
from vcstudio.cluster import ledger
from vcstudio.shared import manifest


def test_register_dedup_unregister_roundtrip(tmp_path):
    lp = tmp_path / 'jobs.json'
    d1, d2 = tmp_path / 'j1', tmp_path / 'j2'
    d1.mkdir(); d2.mkdir()

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
