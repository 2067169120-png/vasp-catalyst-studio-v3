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
        self.closed = False
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
    def close(self): self.closed = True


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


def test_jump_client_closed_after_check():
    """一次性测连不许留下活的跳板连接(每测一次漏一条的回归防线)。"""
    target = FakeClient({'whoami': 'alice\n'})
    jump = FakeClient({})
    clients = iter([target, jump])
    prof = ClusterProfile(name='c', hostname='h', username='alice',
                          auth='key', key_path='/k',
                          use_jump=True, jump_host='bastion')
    res = check_connection(prof, client_factory=lambda: next(clients), trust_new=True)
    assert res.ok is True
    assert target.closed and jump.closed


def test_detect_scheduler_honors_scheduler_bin():
    """1w 实情:torque 不在默认 PATH(command -v 探不到),但 profile 填了
    scheduler_bin=/opt/torque-6.1.2/bin → 探测必须补查该目录,报 PBS 而非 Shell。
    否则用户被"检测到 Shell"误导,查任务永远报调度器不支持(本次 exe 看不到任务的根源之一)。"""
    # 探测是一条合并命令(command -v …; ls <bin>/…):只有探测命令里真的带上了
    # scheduler_bin 的 ls,这个假件才会命中并回出 qsub 路径 → 判定 PBS
    fake = FakeClient({'ls /opt/torque-6.1.2/bin': '/opt/torque-6.1.2/bin/qsub\n'})
    prof = ClusterProfile(name='c', hostname='h', username='u', auth='password',
                          scheduler_bin='/opt/torque-6.1.2/bin')
    res = check_connection(prof, password='pw', client_factory=lambda: fake, trust_new=True)
    assert res.ok is True
    assert res.scheduler == 'PBS'


def test_connect_kwargs_have_robust_timeouts():
    fake = FakeClient({'whoami': 'u\n'})
    prof = ClusterProfile(name='c', hostname='h', username='u', auth='password')
    check_connection(prof, password='pw', client_factory=lambda: fake, trust_new=True)
    kw = fake.connect_kwargs
    assert kw['timeout'] >= 30
    assert kw.get('banner_timeout', 0) >= 30
    assert kw.get('auth_timeout', 0) >= 30


def test_jump_client_closed_on_target_auth_failure():
    import paramiko
    target = FakeClient({}, raise_on_connect=paramiko.AuthenticationException())
    jump = FakeClient({})
    clients = iter([target, jump])
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='password',
                          use_jump=True, jump_host='bastion')
    res = check_connection(prof, password='pw',
                           client_factory=lambda: next(clients), trust_new=True)
    assert res.ok is False
    assert target.closed and jump.closed

