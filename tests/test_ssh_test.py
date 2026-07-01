import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster.ssh_test import check_connection, ConnectionResult


class FakeChannelFile:
    def __init__(self, text): self._t = text
    def read(self): return self._t.encode()


class FakeClient:
    """记录 connect 参数,按脚本回应 exec_command。"""
    def __init__(self, exec_map, raise_on_connect=None):
        self.exec_map = exec_map
        self.raise_on_connect = raise_on_connect
        self.connect_kwargs = None
        self.policy = None
    def set_missing_host_key_policy(self, p): self.policy = p
    def load_system_host_keys(self, *a): pass
    def load_host_keys(self, *a): pass
    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.raise_on_connect:
            raise self.raise_on_connect
    def exec_command(self, cmd):
        for key, val in self.exec_map.items():
            if key in cmd:
                return (None, FakeChannelFile(val), FakeChannelFile(''))
        return (None, FakeChannelFile(''), FakeChannelFile(''))
    def close(self): pass


def test_successful_key_auth_reports_whoami_and_scheduler():
    fake = FakeClient({'whoami': 'alice\n', 'command -v': '/usr/bin/sbatch\n'})
    prof = ClusterProfile(name='c', hostname='h', username='alice',
                          auth='key', key_path='/k/id_rsa')
    res = check_connection(prof, client_factory=lambda: fake, trust_new=True)
    assert res.ok is True
    assert res.whoami == 'alice'
    assert res.scheduler == 'Slurm'
    assert fake.connect_kwargs['key_filename'] == '/k/id_rsa'
    assert fake.connect_kwargs['hostname'] == 'h'


def test_password_auth_passes_password():
    fake = FakeClient({'whoami': 'bob\n', 'command -v': '/bin/qsub\n'})
    prof = ClusterProfile(name='c', hostname='h', username='bob', auth='password')
    res = check_connection(prof, password='pw', client_factory=lambda: fake, trust_new=True)
    assert res.ok is True and res.scheduler == 'PBS'
    assert fake.connect_kwargs['password'] == 'pw'


def test_auth_failure_is_friendly():
    import paramiko
    fake = FakeClient({}, raise_on_connect=paramiko.AuthenticationException())
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='password')
    res = check_connection(prof, password='bad', client_factory=lambda: fake, trust_new=True)
    assert res.ok is False and '认证' in res.message


def test_unknown_host_needs_trust():
    import paramiko
    fake = FakeClient({}, raise_on_connect=paramiko.ssh_exception.SSHException('unknown'))
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    res = check_connection(prof, client_factory=lambda: fake, trust_new=False)
    assert res.ok is False and res.needs_trust is True
