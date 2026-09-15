from vcstudio.cluster.profiles import ClusterProfile
import paramiko

from vcstudio.cluster.ssh_test import check_connection, host_key_fingerprint


_KEY = paramiko.RSAKey.generate(1024)
_OTHER_KEY = paramiko.RSAKey.generate(1024)


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
    def __init__(self, exec_map, raise_on_connect=None, server_key=None):
        self.exec_map = exec_map
        self.raise_on_connect = raise_on_connect
        self.server_key = server_key
        self.connect_kwargs = None
        self.policy = None
        self.closed = False
        self.transport = FakeTransport()
        self.host_keys = paramiko.HostKeys()
        self.saved_paths = []
    def set_missing_host_key_policy(self, p): self.policy = p
    def load_system_host_keys(self, *a): pass
    def load_host_keys(self, path): self.host_keys.load(path)
    def get_host_keys(self): return self.host_keys
    def save_host_keys(self, path):
        self.saved_paths.append(path)
        self.host_keys.save(path)
    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.raise_on_connect:
            raise self.raise_on_connect
        if self.server_key is not None:
            host = str(kwargs['hostname']) if int(kwargs.get('port', 22)) == 22 \
                else f"[{kwargs['hostname']}]:{kwargs['port']}"
            known = self.host_keys.lookup(host)
            expected = (known or {}).get(self.server_key.get_name())
            if expected is not None and expected.asbytes() != self.server_key.asbytes():
                raise paramiko.BadHostKeyException(host, self.server_key, expected)
            if expected is None:
                self.policy.missing_host_key(self, host, self.server_key)
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
    fake = FakeClient({}, server_key=_KEY)
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    res = check_connection(prof, client_factory=lambda: fake, trust_new=False)
    assert res.ok is False and res.needs_trust is True
    assert res.host == 'h'
    assert res.algorithm == _KEY.get_name()
    assert res.fingerprint == host_key_fingerprint(_KEY)


def test_generic_ssh_error_is_never_trustable():
    fake = FakeClient({}, raise_on_connect=paramiko.SSHException('protocol broke'))
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    res = check_connection(prof, client_factory=lambda: fake)
    assert res.ok is False and res.needs_trust is False
    assert res.fingerprint == '' and '协议错误' in res.message


def test_boolean_true_cannot_blindly_trust_unknown_host(tmp_path):
    fake = FakeClient({}, server_key=_KEY)
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    res = check_connection(
        prof, client_factory=lambda: fake, trust_new=True,
        known_hosts_path=tmp_path / 'known_hosts')
    assert res.ok is False and res.needs_trust is True
    assert not (tmp_path / 'known_hosts').exists()


def test_exact_host_and_sha256_pin_persists_key(tmp_path):
    kh = tmp_path / 'known_hosts'
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    first = check_connection(
        prof, client_factory=lambda: FakeClient({}, server_key=_KEY),
        known_hosts_path=kh)
    pin = {'host': first.host, 'fingerprint': first.fingerprint,
           'algorithm': first.algorithm}
    second = check_connection(
        prof, client_factory=lambda: FakeClient({'whoami': 'x\n'}, server_key=_KEY),
        trust_new=pin, known_hosts_path=kh)
    assert second.ok is True
    saved = paramiko.HostKeys(str(kh))
    assert saved.lookup('h')[_KEY.get_name()].asbytes() == _KEY.asbytes()


def test_wrong_host_or_fingerprint_pin_is_hard_failure(tmp_path):
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    for pin in (
        {'host': 'other', 'fingerprint': host_key_fingerprint(_KEY)},
        {'host': 'h', 'fingerprint': host_key_fingerprint(_OTHER_KEY)},
    ):
        res = check_connection(
            prof, client_factory=lambda: FakeClient({}, server_key=_KEY),
            trust_new=pin, known_hosts_path=tmp_path / 'kh')
        assert res.ok is False and res.needs_trust is False
        assert '校验失败' in res.message


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
    # 布尔 True 也只安装捕获策略,绝不启用 AutoAdd。
    assert jump.policy.__class__.__name__ == 'PinnedHostKeyPolicy'


def test_jump_and_target_are_confirmed_and_persisted_in_sequence(tmp_path):
    kh = tmp_path / 'known_hosts'
    prof = ClusterProfile(name='c', hostname='target', username='u', auth='key',
                          key_path='/k', use_jump=True, jump_host='bastion')

    def _run(pin=False):
        target = FakeClient({'whoami': 'u\n'}, server_key=_KEY)
        jump = FakeClient({}, server_key=_OTHER_KEY)
        clients = iter([target, jump])
        return check_connection(
            prof, client_factory=lambda: next(clients), trust_new=pin,
            known_hosts_path=kh)

    first = _run()
    assert first.needs_trust and first.host == 'bastion'
    second = _run({'host': first.host, 'fingerprint': first.fingerprint})
    assert second.needs_trust and second.host == 'target'
    # 跳板 key 在目标 key 待确认时已独立落盘。
    saved = paramiko.HostKeys(str(kh))
    assert saved.lookup('bastion') is not None
    assert saved.lookup('target') is None
    third = _run({'host': second.host, 'fingerprint': second.fingerprint})
    assert third.ok is True
    saved = paramiko.HostKeys(str(kh))
    assert saved.lookup('bastion') is not None
    assert saved.lookup('target') is not None


def test_known_host_key_change_is_hard_failure(tmp_path):
    kh = tmp_path / 'known_hosts'
    kh.write_text(
        f"h {_KEY.get_name()} {_KEY.get_base64()}\n", encoding='utf-8')
    prof = ClusterProfile(name='c', hostname='h', username='u', auth='key', key_path='/k')
    res = check_connection(
        prof, client_factory=lambda: FakeClient({}, server_key=_OTHER_KEY),
        trust_new={'host': 'h', 'fingerprint': host_key_fingerprint(_OTHER_KEY)},
        known_hosts_path=kh)
    assert res.ok is False and res.needs_trust is False
    assert '已变化' in res.message


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
