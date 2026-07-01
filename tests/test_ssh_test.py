from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster.ssh_test import check_connection, ConnectionResult


class FakeChannelFile:
    def __init__(self, text): self._t = text
    def read(self): return self._t.encode()


class FakeTransport:
    """记录 open_channel 参数,返回哨兵 sock。"""
    def __init__(self):
        self.open_channel_args = None
    def open_channel(self, kind, dest, src):
        self.open_channel_args = (kind, dest, src)
        return 'JUMPSOCK'


class FakeClient:
    """记录 connect 参数,按脚本回应 exec_command。"""
    def __init__(self, exec_map, raise_on_connect=None):
        self.exec_map = exec_map
        self.raise_on_connect = raise_on_connect
        self.connect_kwargs = None
        self.policy = None
        self.transport = FakeTransport()
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
    def get_transport(self): return self.transport
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


def test_trusted_host_ssh_error_does_not_ask_trust():
    import paramiko
    fake = FakeClient({}, raise_on_connect=paramiko.ssh_exception.SSHException('proto'))
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    res = check_connection(prof, client_factory=lambda: fake, trust_new=True)
    assert res.ok is False and res.needs_trust is False


def test_jump_channel_used_as_sock_and_gated():
    target = FakeClient({'whoami': 'alice\n', 'command -v': '/usr/bin/sbatch\n'})
    jump = FakeClient({})
    clients = iter([target, jump])  # 第一次 factory() → 目标,第二次 → 跳板机
    prof = ClusterProfile(name='c', hostname='h', port=22, username='alice',
                          auth='key', key_path='/k/id_rsa',
                          use_jump=True, jump_host='bastion', jump_user='j',
                          jump_port=22)
    res = check_connection(prof, client_factory=lambda: next(clients), trust_new=True)
    assert res.ok is True
    # 跳板通道被当作目标连接的 sock
    assert target.connect_kwargs['sock'] == 'JUMPSOCK'
    # 跳板机自身发起了连接(连到 bastion)
    assert jump.connect_kwargs is not None
    assert jump.connect_kwargs['hostname'] == 'bastion'
    assert jump.connect_kwargs['username'] == 'j'
    # direct-tcpip 目标为最终主机:(hostname, port)
    kind, dest, _src = jump.transport.open_channel_args
    assert kind == 'direct-tcpip'
    assert dest == ('h', 22)
    # 跳板机同样按 trust_new 门控:trust_new=True → AutoAddPolicy
    assert jump.policy.__class__.__name__ == 'AutoAddPolicy'
