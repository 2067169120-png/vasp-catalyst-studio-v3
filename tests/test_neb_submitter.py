"""NEB 提交/监控测试(cluster.submitter + diagnose.classify_neb):假 SSH 下的目录树递归
上传、终态收敛判定、逐 image 取证与 NEB 专属诊断签名。照 test_submitter.py 假件风格。"""
import os
import posixpath

import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster import submitter, diagnose
from vcstudio.generate import neb_builder as nb
from vcstudio.shared import manifest


# ── 假件(同 test_submitter,FakeSFTP 增 get 供 NEB 逐 image 回收) ──
class _FakeChannel:
    def __init__(self, status=0):
        self._status = status

    def recv_exit_status(self):
        return self._status


class _FakeStream:
    def __init__(self, data=b'', status=0):
        self._data = data
        self.channel = _FakeChannel(status)

    def read(self):
        return self._data


class FakeClient:
    def __init__(self, script=None, exit_codes=None):
        self.script = list(script or [])
        self.exit_codes = dict(exit_codes or {})
        self.commands = []

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        status = 0
        for needle, code in self.exit_codes.items():
            if needle in cmd:
                status = code
                break
        for needle, out in self.script:
            if needle in cmd:
                return _FakeStream(), _FakeStream(out.encode(), status), _FakeStream()
        return _FakeStream(), _FakeStream(b'', status), _FakeStream()


class _FakeRemoteFile:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, text):
        self.store[self.path] = text


class FakeSFTP:
    def __init__(self, fail_on=()):
        self.uploaded = {}
        self.written = {}
        self.gotten = []
        self.fail_on = tuple(fail_on)

    def put(self, local, remote):
        self.uploaded[remote] = local

    def file(self, path, mode='w'):
        return _FakeRemoteFile(self.written, path)

    def get(self, remote, local):
        self.gotten.append(remote)
        if any(s in remote for s in self.fail_on):
            raise IOError('远端缺文件')
        os.makedirs(os.path.dirname(local) or '.', exist_ok=True)
        with open(local, 'w', encoding='utf-8') as f:
            f.write('x')


def _profile(**kw):
    base = dict(name='1w', hostname='h', username='sk2067', auth='key', key_path='k',
                remote_root='/work/sk2067/jobs', scheduler='PBS',
                scheduler_bin='/opt/torque/bin', queue='batch', nodes=1, ppn=12,
                walltime='24:00:00', env_lines=['source /opt/intel.sh'],
                vasp_cmd='mpirun -np 12 vasp_std > log 2>&1', script_mode='auto')
    base.update(kw)
    return ClusterProfile(**base)


_INI = 'NEB\n1.0\n6 0 0\n0 6 0\n0 0 6\nH\n2\nDirect\n0.20 0.5 0.5\n0.80 0.5 0.5\n'
_FIN = 'NEB\n1.0\n6 0 0\n0 6 0\n0 0 6\nH\n2\nDirect\n0.30 0.5 0.5\n0.70 0.5 0.5\n'
_INCAR = 'ENCUT = 400\nISYM = 0\nNSW = 300\n'


def _fake_potcar(elements):
    return ''.join(f'  PAW_PBE {e}\n   TITEL  = PAW_PBE {e}\n   ENMAX  = 250.0\n' for e in elements)


def _neb_job(tmp_path, n_images=3):
    jd = os.path.join(str(tmp_path), 'neb')
    nb.build_neb_dir(jd, _INI, _FIN, _INCAR, n_images=n_images,
                     kpoints=[3, 3, 1], potcar_fn=_fake_potcar)
    return jd


def _osz(e0):
    return f'DAV:   5  {e0:.4E}  -0.1E-04\n   1 F= {e0:.6E} E0= {e0:.6E}  d E =-.1E-05\n'


_SLOSH = ('DAV:  85  -0.50000E+03  -0.50000E+00  0.1E+03  99\n'
          '   1 F= -.5E+01 E0= -.5E+01  d E =-.1E-05\n')


def _submit(tmp_path, jid='900.c\n', n_images=3):
    jd = _neb_job(tmp_path, n_images)
    submitter.submit_job(FakeClient(script=[('qsub', jid)]), FakeSFTP(), _profile(), jd)
    return jd


# ── preflight ──
def test_neb_preflight_passes(tmp_path):
    jd = _neb_job(tmp_path)
    assert submitter.preflight(_profile(), jd) == []


def test_neb_preflight_missing_image_poscar(tmp_path):
    jd = _neb_job(tmp_path)
    os.remove(os.path.join(jd, '02', 'POSCAR'))
    errs = submitter.preflight(_profile(), jd)
    assert any('02 缺 POSCAR' in e for e in errs)


def test_neb_preflight_images_count_mismatch(tmp_path):
    jd = _neb_job(tmp_path)
    m = manifest.load_manifest(jd)
    m['inputs']['n_images'] = 9                      # 与实际 3 中间 image 不符
    manifest.save_manifest(jd, m)
    errs = submitter.preflight(_profile(), jd)
    assert any('n_images=9' in e for e in errs)


# ── 递归上传 ──
def test_neb_submit_uploads_tree(tmp_path):
    jd = _neb_job(tmp_path)
    client = FakeClient(script=[('qsub', '900.cluster\n')])
    sftp = FakeSFTP()
    m = submitter.submit_job(client, sftp, _profile(), jd)

    remote = m['remote_dir']
    assert remote.startswith('/work/sk2067/jobs/neb--')
    for fr in ('00', '01', '02', '03', '04'):
        assert posixpath.join(remote, fr, 'POSCAR') in sftp.uploaded
        assert any(f'mkdir -p {posixpath.join(remote, fr)}' in c for c in client.commands)
    for f in ('INCAR', 'POTCAR', 'KPOINTS'):
        assert posixpath.join(remote, f) in sftp.uploaded
    assert posixpath.join(remote, 'job.yaml') not in sftp.uploaded   # 台账不上传
    assert posixpath.join(remote, 'vcs_job.sh') in sftp.written
    assert m['state'] == 'SUBMITTED' and m['scheduler_job_id'] == '900'


# ── 刷新:排队/运行/收敛/未收敛 ──
def test_neb_refresh_queued(tmp_path):
    jd = _submit(tmp_path)
    m = submitter.refresh_job(FakeClient(), _profile(), jd, live_states={'900': 'QUEUED'})
    assert m['state'] == 'QUEUED'


def test_neb_refresh_running_records_progress(tmp_path):
    jd = _submit(tmp_path)
    client = FakeClient(script=[('OSZICAR', _osz(-9.5))])   # 各 image 当前 E0
    m = submitter.refresh_job(client, _profile(), jd, live_states={'900': 'RUNNING'})
    assert m['state'] == 'RUNNING'
    assert m['results']['neb_energies'] == [pytest.approx(-9.5)] * 3


def test_neb_refresh_converged_is_done(tmp_path):
    jd = _submit(tmp_path)
    client = FakeClient(script=[
        ('reached required accuracy', 'reached required accuracy - stopping\n'),
        ('___VCSLOG___', 'EXIT: 0\n___VCSLOG___\nnormal end\n'),
        ('01/OSZICAR', _osz(-9.6)),
        ('02/OSZICAR', _osz(-9.3)),
        ('03/OSZICAR', _osz(-9.7)),
        ('ls -1d', '00\n01\n02\n03\n04\n'),
    ])
    m = submitter.refresh_job(client, _profile(), jd, live_states={})
    assert m['state'] == 'DONE'
    assert m['results']['diagnosis']['failure_class'] == 'CONVERGED'
    assert m['results']['neb_energies'] == [pytest.approx(v) for v in (-9.6, -9.3, -9.7)]


def test_neb_refresh_nonconverged_restartable(tmp_path):
    jd = _submit(tmp_path)
    client = FakeClient(script=[('OSZICAR', _osz(-9.5))])   # 有输出、无收敛串、无 ls
    m = submitter.refresh_job(client, _profile(), jd, live_states={})
    assert m['state'] == 'UNCONVERGED'
    assert m['results']['diagnosis']['failure_class'] == 'NONCONVERGED'
    assert m['results']['diagnosis']['restartable'] is True


def test_neb_refresh_image_missing_is_needs_human(tmp_path):
    jd = _submit(tmp_path)
    client = FakeClient(script=[
        ('02/OSZICAR', ''),                            # image02 无输出
        ('01/OSZICAR', _osz(-9.6)),
        ('03/OSZICAR', _osz(-9.7)),
    ])
    m = submitter.refresh_job(client, _profile(), jd, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    d = m['results']['diagnosis']
    assert d['failure_class'] == 'NEB_IMAGE_MISSING' and '02' in d['evidence']


def test_neb_refresh_images_mismatch(tmp_path):
    jd = _submit(tmp_path)
    client = FakeClient(script=[('ls -1d', '00\n01\n02\n03\n')])   # 只 2 中间 image,INCAR IMAGES=3
    m = submitter.refresh_job(client, _profile(), jd, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    assert m['results']['diagnosis']['failure_class'] == 'NEB_IMAGES_MISMATCH'


def test_neb_refresh_image_scf_crash_named(tmp_path):
    jd = _submit(tmp_path)
    client = FakeClient(script=[
        ('01/OSZICAR', _SLOSH),                        # image01 SCF 震荡
        ('02/OSZICAR', _osz(-9.3)),
        ('03/OSZICAR', _osz(-9.7)),
    ])
    m = submitter.refresh_job(client, _profile(), jd, live_states={})
    assert m['state'] == 'NEEDS_HUMAN'
    d = m['results']['diagnosis']
    assert d['failure_class'] == 'NEB_IMAGE_SCF' and '01' in d['evidence']


# ── 逐 image 回收 ──
def test_neb_fetch_per_image(tmp_path):
    jd = _submit(tmp_path)
    sftp = FakeSFTP()
    fetched, missing = submitter.fetch_results(FakeClient(), sftp, jd)
    assert '01/OSZICAR' in fetched and '00/OUTCAR' in fetched and '04/OUTCAR' in fetched
    assert os.path.isfile(os.path.join(jd, '02', 'OSZICAR'))
    assert missing == []


def test_neb_fetch_missing_recorded(tmp_path):
    jd = _submit(tmp_path)
    sftp = FakeSFTP(fail_on=('CONTCAR',))              # 远端各 image 无 CONTCAR
    fetched, missing = submitter.fetch_results(FakeClient(), sftp, jd)
    assert any('CONTCAR' in t for t in missing)
    assert any('OSZICAR' in t for t in fetched)


# ── diagnose.classify_neb 纯函数分支 ──
def test_classify_neb_walltime_restartable():
    d = diagnose.classify_neb(images_expected=3, images_found=3,
                              image_status=[{'index': 1, 'empty': False, 'scf_fail': False}],
                              converged=False, scheduler_reason=diagnose.R_TIMEOUT)
    assert d.failure_class == diagnose.WALLTIME and d.restartable


def test_classify_neb_npar_divide_signature():
    """NEB 专属签名:总核数不能整除 IMAGES → M_divide → NEB_NPAR_DIVIDE(交人工)。"""
    hit = diagnose.scan_vasp_error('M_divide: can not subdivide 40 groups')
    assert hit is not None and hit[0] == 'NEB_NPAR_DIVIDE'
    d = diagnose.classify_neb(images_expected=3, images_found=3, image_status=[],
                              converged=False, log_tail='M_divide: can not subdivide')
    assert d.failure_class == 'NEB_NPAR_DIVIDE' and d.state == 'NEEDS_HUMAN'


def test_classify_neb_converged_done():
    d = diagnose.classify_neb(images_expected=3, images_found=3, image_status=[],
                              converged=True)
    assert d.failure_class == diagnose.CONVERGED and d.state == 'DONE'
