"""connection.open_client 测试:注入假 factory,验证跳板 client 的交还与失败路径关闭。"""
import paramiko
import pytest

from vcstudio.cluster.connection import open_client, ConnectError
from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster.ssh_test import host_key_fingerprint


_KEY = paramiko.RSAKey.generate(1024)
_OTHER_KEY = paramiko.RSAKey.generate(1024)


class _FakeTransport:
    def __init__(self):
        self.keepalive = None

    def open_channel(self, kind, dest, src):
        return 'JUMPSOCK'

    def set_keepalive(self, seconds):
        self.keepalive = seconds


class FakeClient:
    def __init__(self, raise_on_connect=None, server_key=None):
        self.raise_on_connect = raise_on_connect
        self.server_key = server_key
        self.connect_kwargs = None
        self.closed = False
        self.transport = _FakeTransport()
        self.policy = None
        self.host_keys = paramiko.HostKeys()

    def set_missing_host_key_policy(self, p):
        self.policy = p

    def load_system_host_keys(self, *a):
        pass

    def load_host_keys(self, path):
        self.host_keys.load(path)

    def get_host_keys(self):
        return self.host_keys

    def save_host_keys(self, path):
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

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True


def _prof(**kw):
    base = dict(name='c', hostname='h', username='u', auth='key', key_path='/k')
    base.update(kw)
    return ClusterProfile(**base)


def _jump_prof():
    return _prof(use_jump=True, jump_host='bastion', jump_user='j')


def test_no_jump_returns_none_jump(tmp_path):
    fake = FakeClient()
    client, jump = open_client(_prof(), client_factory=lambda: fake,
                               known_hosts_path=tmp_path / 'kh', trust_new=True)
    assert client is fake and jump is None


def test_jump_client_returned_for_closing(tmp_path):
    target, jump = FakeClient(), FakeClient()
    clients = iter([target, jump])
    client, j = open_client(_jump_prof(), client_factory=lambda: next(clients),
                            known_hosts_path=tmp_path / 'kh', trust_new=True)
    # 跳板 client 必须交还调用方,否则那条活连接永远关不掉(每次提交/查询漏一个)
    assert client is target and j is jump
    assert target.connect_kwargs['sock'] == 'JUMPSOCK'
    client.close()
    j.close()
    assert target.closed and jump.closed


def test_jump_closed_on_auth_failure(tmp_path):
    target = FakeClient(raise_on_connect=paramiko.AuthenticationException())
    jump = FakeClient()
    clients = iter([target, jump])
    with pytest.raises(ConnectError, match='认证'):
        open_client(_jump_prof(), client_factory=lambda: next(clients),
                    known_hosts_path=tmp_path / 'kh', trust_new=True)
    assert target.closed and jump.closed          # 失败路径不留活连接


def test_connect_kwargs_have_robust_timeouts(tmp_path):
    """1w 跳板 sshd 实测慢:banner 可超 15s。借 V2.0.0 口径:timeout≥30 + banner/auth 超时显式给足。"""
    fake = FakeClient()
    open_client(_prof(), client_factory=lambda: fake,
                known_hosts_path=tmp_path / 'kh', trust_new=True)
    kw = fake.connect_kwargs
    assert kw['timeout'] >= 30
    assert kw.get('banner_timeout', 0) >= 30
    assert kw.get('auth_timeout', 0) >= 30


def test_jump_connect_kwargs_have_robust_timeouts(tmp_path):
    target, jump = FakeClient(), FakeClient()
    clients = iter([target, jump])
    open_client(_jump_prof(), client_factory=lambda: next(clients),
                known_hosts_path=tmp_path / 'kh', trust_new=True)
    for c in (target, jump):
        assert c.connect_kwargs['timeout'] >= 30
        assert c.connect_kwargs.get('banner_timeout', 0) >= 30


def test_keepalive_set_on_transports(tmp_path):
    """借 V2.0.0 的 set_keepalive(30):长查询/慢集群下防 NAT 半路掐死连接。"""
    target, jump = FakeClient(), FakeClient()
    clients = iter([target, jump])
    open_client(_jump_prof(), client_factory=lambda: next(clients),
                known_hosts_path=tmp_path / 'kh', trust_new=True)
    assert target.transport.keepalive == 30
    assert jump.transport.keepalive == 30


def test_jump_closed_on_ssh_error(tmp_path):
    target = FakeClient(raise_on_connect=paramiko.ssh_exception.SSHException('x'))
    jump = FakeClient()
    clients = iter([target, jump])
    with pytest.raises(ConnectError):
        open_client(_jump_prof(), client_factory=lambda: next(clients),
                    known_hosts_path=tmp_path / 'kh', trust_new=True)
    assert target.closed and jump.closed


def test_unknown_host_error_contains_real_key_evidence(tmp_path):
    fake = FakeClient(server_key=_KEY)
    with pytest.raises(ConnectError) as caught:
        open_client(_prof(), client_factory=lambda: fake,
                    known_hosts_path=tmp_path / 'kh')
    err = caught.value
    assert err.needs_trust is True
    assert err.host == 'h'
    assert err.algorithm == _KEY.get_name()
    assert err.fingerprint == host_key_fingerprint(_KEY)


def test_generic_ssh_exception_never_needs_trust(tmp_path):
    fake = FakeClient(raise_on_connect=paramiko.SSHException('kex failed'))
    with pytest.raises(ConnectError) as caught:
        open_client(_prof(), client_factory=lambda: fake,
                    known_hosts_path=tmp_path / 'kh')
    assert caught.value.needs_trust is False
    assert '协议错误' in str(caught.value)


def test_boolean_true_does_not_authorize_unknown_key(tmp_path):
    fake = FakeClient(server_key=_KEY)
    with pytest.raises(ConnectError) as caught:
        open_client(_prof(), client_factory=lambda: fake, trust_new=True,
                    known_hosts_path=tmp_path / 'kh')
    assert caught.value.needs_trust is True
    assert not (tmp_path / 'kh').exists()


def test_exact_pin_connects_and_persists(tmp_path):
    kh = tmp_path / 'kh'
    pin = {'host': 'h', 'fingerprint': host_key_fingerprint(_KEY),
           'algorithm': _KEY.get_name()}
    fake = FakeClient(server_key=_KEY)
    client, jump = open_client(
        _prof(), client_factory=lambda: fake, trust_new=pin,
        known_hosts_path=kh)
    assert client is fake and jump is None
    assert paramiko.HostKeys(str(kh)).lookup('h') is not None


def test_wrong_pin_and_known_key_change_are_not_confirmable(tmp_path):
    kh = tmp_path / 'kh'
    wrong_pin = {'host': 'h', 'fingerprint': host_key_fingerprint(_OTHER_KEY)}
    with pytest.raises(ConnectError) as caught:
        open_client(_prof(), client_factory=lambda: FakeClient(server_key=_KEY),
                    trust_new=wrong_pin, known_hosts_path=kh)
    assert caught.value.needs_trust is False

    kh.write_text(f"h {_KEY.get_name()} {_KEY.get_base64()}\n", encoding='utf-8')
    changed_pin = {'host': 'h', 'fingerprint': host_key_fingerprint(_OTHER_KEY)}
    with pytest.raises(ConnectError) as changed:
        open_client(_prof(), client_factory=lambda: FakeClient(server_key=_OTHER_KEY),
                    trust_new=changed_pin, known_hosts_path=kh)
    assert changed.value.needs_trust is False
    assert '已变化' in str(changed.value)


def test_jump_then_target_require_separate_pins(tmp_path):
    kh = tmp_path / 'kh'

    def _open(pin=False):
        target = FakeClient(server_key=_KEY)
        jump = FakeClient(server_key=_OTHER_KEY)
        clients = iter([target, jump])
        return open_client(
            _jump_prof(), client_factory=lambda: next(clients),
            trust_new=pin, known_hosts_path=kh)

    with pytest.raises(ConnectError) as jump_unknown:
        _open()
    assert jump_unknown.value.host == 'bastion'
    with pytest.raises(ConnectError) as target_unknown:
        _open({'host': 'bastion',
               'fingerprint': jump_unknown.value.fingerprint})
    assert target_unknown.value.host == 'h'
    saved = paramiko.HostKeys(str(kh))
    assert saved.lookup('bastion') is not None and saved.lookup('h') is None
    client, jump = _open({'host': 'h',
                          'fingerprint': target_unknown.value.fingerprint})
    assert client is not None and jump is not None
