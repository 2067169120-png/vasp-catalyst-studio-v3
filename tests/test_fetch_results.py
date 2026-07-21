"""结果回收测试:fetch_results 下载/缺文件降级/未提交报错(假 sftp)。"""
import os

import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster import submitter
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.shared import manifest


class _FakeChannel:
    def recv_exit_status(self):
        return 0


class _FS:
    def __init__(self, data=b''):
        self._d = data
        self.channel = _FakeChannel()

    def read(self):
        return self._d


class FakeClient:
    def __init__(self, script=None):
        self.script = list(script or [])

    def exec_command(self, cmd, timeout=None):
        for needle, out in self.script:
            if needle in cmd:
                return _FS(), _FS(out.encode()), _FS()
        return _FS(), _FS(b''), _FS()


class FakeSFTP:
    """get:remote 在 available 里 → 写个本地占位文件;否则 IOError(模拟远端缺文件)。"""

    def __init__(self, available=('CONTCAR', 'OSZICAR', 'OUTCAR')):
        self.available = set(available)
        self.puts = {}
        self.gets = []

    def put(self, local, remote):
        self.puts[remote] = local

    def file(self, path, mode='w'):
        class _W:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def write(self_inner, text):
                pass
        return _W()

    def get(self, remote, local):
        self.gets.append(remote)
        name = remote.rsplit('/', 1)[-1]
        if name not in self.available:
            raise IOError(f'no such file: {remote}')
        with open(local, 'w', encoding='utf-8') as f:
            f.write(f'fake {name}\n')


def _job(tmp_path):
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   TITEL  = PAW_PBE C 08Apr2002\n'
        '   ENMAX  =  273.214; ENMIN = 200.000 eV\n', encoding='utf-8')
    poscar = tmp_path / 'POSCAR'
    poscar.write_text('C atom\n1.0\n10 0 0\n0 10 0\n0 0 10\nC\n1\nCartesian\n0 0 0\n',
                      encoding='utf-8')
    out = tmp_path / 'j'
    res = build_job_dir(str(poscar), 'ENCUT = 400\n', str(out),
                        calc_type='slab', lib_root=str(lib))
    manifest.create_from_build(str(out), res, poscar_path=str(poscar), validate=True)
    return str(out)


def _prof():
    return ClusterProfile(name='1w', hostname='h', username='u', remote_root='/w',
                          scheduler='PBS', queue='batch', ppn=12, vasp_cmd='mpirun vasp')


def _mark_done(job_dir):
    data = manifest.load_manifest(job_dir)
    manifest.set_state(data, 'DONE', note='test remote completion')
    manifest.save_manifest(job_dir, data)


def test_fetch_all_and_manifest_record(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '11.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)
    fetched, missing = submitter.fetch_results(FakeClient(), FakeSFTP(), d)
    assert fetched == ['CONTCAR', 'OSZICAR', 'OUTCAR']
    assert missing == ['vasprun.xml', 'CHGCAR']
    for f in fetched:
        assert os.path.isfile(os.path.join(d, f))
    m = manifest.load_manifest(d)
    assert m['results']['fetched'] == fetched and m['results']['fetched_at']


def test_fetch_partial_missing(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '12.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)
    fetched, missing = submitter.fetch_results(
        FakeClient(), FakeSFTP(available=('OSZICAR',)), d)
    assert fetched == ['OSZICAR']
    assert set(missing) == {'CONTCAR', 'OUTCAR', 'vasprun.xml', 'CHGCAR'}


def test_fetch_before_submit_raises(tmp_path):
    d = _job(tmp_path)
    with pytest.raises(ValueError, match='尚未提交'):
        submitter.fetch_results(FakeClient(), FakeSFTP(), d)


def test_interrupted_fetch_keeps_previous_file_and_cleans_temp(tmp_path):
    """SFTP 在半途断线时只能破坏 .part，不能截断上一轮完整 OUTCAR。"""
    d = _job(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '13.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)
    old_path = os.path.join(d, 'OUTCAR')
    with open(old_path, 'w', encoding='utf-8') as handle:
        handle.write('previous complete OUTCAR\n')

    class PartialFailure(FakeSFTP):
        def get(self, remote, local):
            name = remote.rsplit('/', 1)[-1]
            if name == 'OUTCAR':
                with open(local, 'w', encoding='utf-8') as handle:
                    handle.write('truncated new data')
                raise IOError('connection dropped')
            return super().get(remote, local)

    _fetched, missing = submitter.fetch_results(FakeClient(), PartialFailure(), d)
    assert 'OUTCAR' in missing
    with open(old_path, encoding='utf-8') as handle:
        assert handle.read() == 'previous complete OUTCAR\n'
    assert not [name for name in os.listdir(d)
                if name.startswith('.OUTCAR.vcstudio-') and name.endswith('.part')]


def test_fetch_profile_binding_rejects_wrong_server(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(FakeClient(script=[('qsub', '14.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)
    wrong = ClusterProfile(name='other', hostname='x')
    with pytest.raises(ValueError, match='属于服务器「1w」'):
        submitter.fetch_results(FakeClient(), FakeSFTP(), d, profile=wrong)


def test_final_fetch_requires_done_but_preview_writes_no_final_evidence(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(
        FakeClient(script=[('qsub', '15.cluster\n')]), FakeSFTP(), _prof(), d)
    sftp = FakeSFTP()

    with pytest.raises(ValueError, match='DONE'):
        submitter.fetch_results(FakeClient(), sftp, d)
    assert not sftp.gets

    fetched, _missing = submitter.fetch_results(
        FakeClient(), sftp, d, preview=True)
    assert fetched
    results = manifest.load_manifest(d).get('results') or {}
    assert 'fetched_at' not in results
    assert 'fetched_state' not in results
    assert 'fetch_contract' not in results


def test_final_fetch_records_attempt_contract_and_file_hashes(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(
        FakeClient(script=[('qsub', '16.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)

    fetched, _missing = submitter.fetch_results(FakeClient(), FakeSFTP(), d)

    data = manifest.load_manifest(d)
    results = data['results']
    assert results['fetched_state'] == 'DONE'
    assert results['fetched_attempt_token'] == submitter.current_attempt_token(data)
    assert results['fetch_contract'] == {
        'schema': 1,
        'mode': 'final',
        'state': 'DONE',
        'scheduler_job_id': '16',
        'remote_dir': data['remote_dir'],
        'attempt_token': results['fetched_attempt_token'],
        'requested': results['fetch_requested'],
    }
    for name in fetched:
        local = os.path.join(d, name)
        assert results['fetched_sizes'][name] == os.path.getsize(local)
        assert len(results['fetched_sha256'][name]) == 64
        assert results['fetched_files'][name] == {
            'sha256': results['fetched_sha256'][name],
            'size': results['fetched_sizes'][name],
        }


def test_final_fetch_cas_does_not_overwrite_concurrent_resubmit(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(
        FakeClient(script=[('qsub', '17.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)

    class ResubmittingSFTP(FakeSFTP):
        changed = False

        def get(self, remote, local):
            super().get(remote, local)
            if not self.changed:
                self.changed = True
                latest = manifest.load_manifest(d)
                latest['scheduler_job_id'] = '18'
                manifest.set_state(latest, 'SUBMITTED', note='concurrent restart')
                latest.setdefault('attempts', []).append({
                    'n': len(latest.get('attempts') or []) + 1,
                    'at': '2026-01-02T00:00:00',
                    'job_id': '18',
                    'action': 'concurrent-test',
                })
                for key in ('fetched_at', 'fetched_state', 'fetch_contract'):
                    latest.setdefault('results', {}).pop(key, None)
                manifest.save_manifest(d, latest)

    with pytest.raises(RuntimeError, match='提交代次已变化'):
        submitter.fetch_results(FakeClient(), ResubmittingSFTP(), d)

    latest = manifest.load_manifest(d)
    assert latest['scheduler_job_id'] == '18'
    assert latest['state'] == 'SUBMITTED'
    assert 'fetched_at' not in (latest.get('results') or {})


def test_same_name_profile_endpoint_change_is_blocked(tmp_path):
    d = _job(tmp_path)
    submitter.submit_job(
        FakeClient(script=[('qsub', '19.cluster\n')]), FakeSFTP(), _prof(), d)
    _mark_done(d)
    edited = ClusterProfile(
        name='1w', hostname='another-host', username='u', remote_root='/w',
        scheduler='PBS', queue='batch', ppn=12, vasp_cmd='mpirun vasp')
    with pytest.raises(ValueError, match='连接端点已与作业提交时不同'):
        submitter.fetch_results(FakeClient(), FakeSFTP(), d, profile=edited)


def test_legacy_manifest_without_endpoint_fingerprint_uses_name_fallback(tmp_path):
    d = _job(tmp_path)
    data = manifest.load_manifest(d)
    data.update({
        'cluster': 'legacy',
        'remote_dir': '/legacy/job',
        'scheduler_job_id': '20',
    })
    manifest.set_state(data, 'DONE')
    manifest.save_manifest(d, data)
    legacy = ClusterProfile(name='legacy', hostname='new-host')

    fetched, _missing = submitter.fetch_results(
        FakeClient(), FakeSFTP(), d, profile=legacy)

    assert fetched
