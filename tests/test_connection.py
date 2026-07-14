"""connection.open_client 测试:注入假 factory,验证跳板 client 的交还与失败路径关闭。"""
import paramiko
import pytest

from vcstudio.cluster.connection import open_client, ConnectError
from vcstudio.cluster.profiles import ClusterProfile


class _FakeTransport:
    def __init__(self):
        self.keepalive = None

    def open_channel(self, kind, dest, src):
        return 'JUMPSOCK'

    def set_keepalive(self, seconds):
        self.keepalive = seconds


class FakeClient:
    def __init__(self, raise_on_connect=None):
        self.raise_on_connect = raise_on_connect
        self.connect_kwargs = None
        self.closed = False
        self.transport = _FakeTransport()

    def set_missing_host_key_policy(self, p):
        pass

    def load_system_host_keys(self, *a):
        pass

    def load_host_keys(self, *a):
        pass

    def save_host_keys(self, *a):
        pass

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.raise_on_connect:
            raise self.raise_on_connect

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
