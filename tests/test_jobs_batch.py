"""jobs_tab 后台批量函数测试(不建 Tk 窗口,只测线程体):连接关闭责任与单作业失败隔离。"""
from vcstudio.gui import jobs_tab
from vcstudio.shared import manifest as mm


def _mk_job(tmp_path, name, state, *, restartable=None, rounds=0):
    d = tmp_path / name
    d.mkdir()
    m = mm.new_manifest(job_id=name, system='s', task_type='relax', calc_type='slab', inputs={})
    if state != 'CREATED':
        mm.set_state(m, state)
    res = {}
    if restartable is not None:
        res['diagnosis'] = {'failure_class': 'NONCONVERGED', 'restartable': restartable}
    if rounds:
        res['continue_rounds'] = rounds
    m['results'] = res
    mm.save_manifest(d, m)
    return str(d)


def test_filter_continuable_partitions_selection(tmp_path):
    ok = _mk_job(tmp_path, 'ok', 'UNCONVERGED', restartable=True)
    running = _mk_job(tmp_path, 'run', 'RUNNING', restartable=True)      # 仍在跑 → 跳过
    hardfail = _mk_job(tmp_path, 'seg', 'FAILED', restartable=False)     # 不可续算 → 跳过
    capped = _mk_job(tmp_path, 'cap', 'UNCONVERGED', restartable=True, rounds=3)  # 到顶 → 跳过
    nodiag = _mk_job(tmp_path, 'done', 'DONE')                           # 无诊断 → 跳过
    eligible, skipped = jobs_tab._filter_continuable([ok, running, hardfail, capped, nodiag])
    assert eligible == [ok] and skipped == 4


class FakeSFTP:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self):
        self.closed = False
        self.sftp = FakeSFTP()

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


def _patch_open(monkeypatch):
    client, jump = FakeConn(), FakeConn()
    monkeypatch.setattr(jobs_tab, 'open_client',
                        lambda prof, pw, trust_new: (client, jump))
    return client, jump


def test_submit_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(jobs_tab.submitter, 'submit_job',
                        lambda c, s, p, d: {'scheduler_job_id': '1'})
    payload = jobs_tab._submit_batch(object(), None, ['d1'], False)
    assert payload['results'] == [('d1', True, '已提交,作业号 1')]
    assert client.closed and jump.closed          # 跳板连接同样必须关


def test_refresh_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(jobs_tab.submitter, 'query_states', lambda c, p: {})
    monkeypatch.setattr(jobs_tab.submitter, 'refresh_job',
                        lambda c, p, d, live_states: {'state': 'DONE', 'results': {}})
    payload = jobs_tab._refresh_batch(object(), None, ['d1'], False)
    assert payload['results'] == [('d1', 'DONE')]
    assert client.closed and jump.closed


def test_fetch_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(jobs_tab.submitter, 'fetch_results',
                        lambda c, s, d: (['CONTCAR'], []))
    payload = jobs_tab._fetch_batch(object(), None, ['d1'], False)
    assert payload['results'][0][1] is True
    assert client.closed and jump.closed


# ── 单作业失败隔离:一条连接抖动(SSHException)不许打断整批 ──
def test_submit_batch_survives_ssh_exception(monkeypatch):
    from paramiko.ssh_exception import SSHException
    client, jump = _patch_open(monkeypatch)

    def flaky(c, s, p, d):
        if d == 'bad':
            raise SSHException('channel closed')
        return {'scheduler_job_id': '2'}

    monkeypatch.setattr(jobs_tab.submitter, 'submit_job', flaky)
    payload = jobs_tab._submit_batch(object(), None, ['bad', 'good'], False)
    assert [r[1] for r in payload['results']] == [False, True]   # bad 失败,good 照常
    assert 'channel closed' in payload['results'][0][2]
    assert client.closed and jump.closed


def test_refresh_batch_survives_ssh_exception(monkeypatch):
    from paramiko.ssh_exception import SSHException
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(jobs_tab.submitter, 'query_states', lambda c, p: {})

    def flaky(c, p, d, live_states):
        if d == 'bad':
            raise SSHException('boom')
        return {'state': 'DONE', 'results': {}}

    monkeypatch.setattr(jobs_tab.submitter, 'refresh_job', flaky)
    payload = jobs_tab._refresh_batch(object(), None, ['bad', 'good'], False)
    assert '查询失败' in payload['results'][0][1]
    assert payload['results'][1][1] == 'DONE'
    assert client.closed and jump.closed


def test_fetch_batch_survives_ssh_exception(monkeypatch):
    from paramiko.ssh_exception import SSHException
    client, jump = _patch_open(monkeypatch)

    def flaky(c, s, d):
        if d == 'bad':
            raise SSHException('boom')
        return (['CONTCAR'], [])

    monkeypatch.setattr(jobs_tab.submitter, 'fetch_results', flaky)
    payload = jobs_tab._fetch_batch(object(), None, ['bad', 'good'], False)
    assert [r[1] for r in payload['results']] == [False, True]
    assert client.closed and jump.closed


# ── 续算批量:关闭责任 + 单作业失败隔离(不可续算的自失败不打断整批) ──
def test_continue_batch_closes_client_and_jump(monkeypatch):
    client, jump = _patch_open(monkeypatch)
    monkeypatch.setattr(jobs_tab.submitter, 'continue_from_contcar',
                        lambda c, p, d: {'scheduler_job_id': '9'})
    payload = jobs_tab._continue_batch(object(), None, ['d1'], False)
    assert payload['results'] == [('d1', True, '已续算重投,新作业号 9')]
    assert client.closed and jump.closed


def test_continue_batch_survives_error(monkeypatch):
    client, jump = _patch_open(monkeypatch)

    def flaky(c, p, d):
        if d == 'bad':
            raise RuntimeError('不可自动续算')
        return {'scheduler_job_id': '9'}

    monkeypatch.setattr(jobs_tab.submitter, 'continue_from_contcar', flaky)
    payload = jobs_tab._continue_batch(object(), None, ['bad', 'good'], False)
    assert [r[1] for r in payload['results']] == [False, True]
    assert client.closed and jump.closed
