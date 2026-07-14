"""batch_ops 是 UI 无关线程体:两个 GUI 共用,绝不 import tkinter。"""
import os
import sys
import types

from vcstudio.cluster import batch_ops


def test_no_tkinter_dependency():
    assert 'tkinter' not in sys.modules or True  # 见下:检查模块源
    import inspect
    src = inspect.getsource(batch_ops)
    assert 'tkinter' not in src


def test_filter_continuable_empty():
    eligible, skipped = batch_ops.filter_continuable([])
    assert eligible == [] and skipped == 0


def test_public_surface():
    for name in ('submit_batch', 'fetch_batch', 'continue_batch',
                 'refresh_batch', 'queue_detail', 'tune_batch', 'adopt_scan'):
        assert callable(getattr(batch_ops, name))


# ── adopt_scan:一次连接完成 明细 → 补目录 → 落认领 ────────────────────────────
def test_adopt_scan_skips_known_adopts_and_flags_missing(tmp_path, monkeypatch):
    """3 作业:1 已纳管跳过、1 带 workdir 认领、1 无 workdir(query_workdir 也空)→ 需手填。

    断言 adopt_external_job 以 tmp_path 下 sanitize 后的目录被调用,且该目录被 makedirs。
    """
    # 假连接:open_client → (client, jump);close_quiet 无操作
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('CLIENT', 'JUMP'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)

    jobs = [
        {'job_id': '100', 'state': 'RUNNING', 'name': 'known', 'workdir': '/home/u/known'},
        {'job_id': '200', 'state': 'QUEUED', 'name': 'ads/job:2', 'workdir': '/home/u/w2'},
        {'job_id': '300', 'state': 'QUEUED', 'name': 'nowd', 'workdir': ''},
    ]
    wd_calls, adopted = [], []

    def _adopt(local_dir, prof, job_id, remote_dir, name=''):
        adopted.append({'local': local_dir, 'jid': job_id,
                        'remote': remote_dir, 'name': name})
        return {'state': 'SUBMITTED'}

    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail',
                        lambda client, prof: jobs)
    monkeypatch.setattr(batch_ops.submitter, 'query_workdir',
                        lambda client, prof, jid: wd_calls.append(jid) or '')
    monkeypatch.setattr(batch_ops.submitter, 'adopt_external_job', _adopt)

    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, {'100'}, str(tmp_path))

    assert out['needs_trust'] is False
    rows = {r[0]: r for r in out['results']}
    assert '100' not in rows                       # 已纳管:不入 results
    assert rows['200'][1] is True and '已认领' in rows['200'][2]
    assert rows['300'][1] is False and '查不到工作目录' in rows['300'][2]
    # 仅无 workdir 的 300 才落回 query_workdir(200 已带 workdir 短路)
    assert wd_calls == ['300']
    # 认领目录 = tmp_path/<sanitize('ads/job:2')>,且被 makedirs 建出
    expected = os.path.join(str(tmp_path), 'ads_job_2')
    assert adopted == [{'local': expected, 'jid': '200',
                        'remote': '/home/u/w2', 'name': 'ads/job:2'}]
    assert os.path.isdir(expected)


def test_adopt_scan_name_collision_appends_jid(tmp_path, monkeypatch):
    """两个未纳管作业同名 'Nb_S8'(qstat 截断易撞名):第二个本地目录撞名 → 追加 _<jid2>,
    两个都认领成功且落到不同目录(不再第二个失败还引用第一个的作业号)。"""
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('C', 'J'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)

    jobs = [
        {'job_id': '501', 'state': 'QUEUED', 'name': 'Nb_S8', 'workdir': '/home/u/a'},
        {'job_id': '502', 'state': 'QUEUED', 'name': 'Nb_S8', 'workdir': '/home/u/b'},
    ]
    adopted = []

    def _adopt(local_dir, prof, job_id, remote_dir, name=''):
        adopted.append(local_dir)
        return {'state': 'SUBMITTED'}

    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail',
                        lambda client, prof: jobs)
    monkeypatch.setattr(batch_ops.submitter, 'query_workdir',
                        lambda client, prof, jid: '')
    monkeypatch.setattr(batch_ops.submitter, 'adopt_external_job', _adopt)

    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, set(), str(tmp_path))

    rows = {r[0]: r for r in out['results']}
    assert rows['501'][1] is True and rows['502'][1] is True   # 两个都认领成功
    assert len(adopted) == 2 and adopted[0] != adopted[1]      # 落到不同目录
    assert adopted[0] == os.path.join(str(tmp_path), 'Nb_S8')
    assert adopted[1] == os.path.join(str(tmp_path), 'Nb_S8_502')
    assert os.path.isdir(adopted[1])


def test_adopt_scan_needs_trust_branch(monkeypatch):
    """首次未知主机指纹:open_client 抛 ConnectError(needs_trust)→ 返回 needs_trust。"""
    from vcstudio.cluster.connection import ConnectError

    def _boom(prof, pw, trust_new=False):
        raise ConnectError('未知主机', needs_trust=True)

    monkeypatch.setattr(batch_ops, 'open_client', _boom)
    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, set(), '/root')
    assert out['needs_trust'] is True and out['results'] == []


def test_adopt_scan_adopt_failure_is_per_job(tmp_path, monkeypatch):
    """单作业 adopt_external_job 抛错只失败该条,msg 透传异常文案。"""
    monkeypatch.setattr(batch_ops, 'open_client',
                        lambda prof, pw, trust_new=False: ('C', 'J'))
    monkeypatch.setattr(batch_ops, 'close_quiet', lambda c, j: None)
    monkeypatch.setattr(batch_ops.submitter, 'query_queue_detail',
                        lambda client, prof: [
                            {'job_id': '9', 'name': 'x', 'workdir': '/home/u/x'}])
    monkeypatch.setattr(batch_ops.submitter, 'query_workdir',
                        lambda client, prof, jid: '')
    monkeypatch.setattr(batch_ops.submitter, 'adopt_external_job',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('已关联作业号')))
    prof = types.SimpleNamespace(name='c1')
    out = batch_ops.adopt_scan(prof, 'pw', False, set(), str(tmp_path))
    assert out['results'] == [['9', False, '已关联作业号']]
