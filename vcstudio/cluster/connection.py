"""可复用 SSH 连接:提交/查状态用的活连接(区别于 ssh_test 的一次性测连)。

安全语义与 ssh_test 完全一致:仅 missing_host_key 捕获的真实
SHA256 指纹可进入确认流程,且后续必须传 host+指纹精确 pin。跳板机复用
ssh_test._open_jump_channel,不重复实现 MITM 防线。paramiko 延迟导入,
纯逻辑层(schedulers/script_builder/ledger)在无 paramiko 环境下仍可测试。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from pathlib import Path


class ConnectError(Exception):
    """连接失败。needs_trust=True 表示未知主机指纹,界面确认后可 trust_new 重试。"""

    def __init__(self, message: str, needs_trust: bool = False, *,
                 fingerprint: str = '', algorithm: str = '', host: str = ''):
        super().__init__(message)
        self.needs_trust = needs_trust
        self.fingerprint = fingerprint
        self.algorithm = algorithm
        self.host = host


def open_client(profile, password: str | None = None, *,
                trust_new=False,
                known_hosts_path: str | os.PathLike | None = None,
                client_factory=None):
    """连上集群,返回 (client, jump_client|None)。调用方负责两个都 close。

    失败抛 ConnectError(中文文案;未知指纹时 needs_trust=True 且
    附 fingerprint/algorithm/host)。trust_new 必须是这些字段的原样 pin 映射。
    """
    import paramiko                                    # 延迟导入
    from vcstudio.cluster import ssh_test              # 复用其 known_hosts/跳板逻辑

    factory = client_factory or paramiko.SSHClient
    kh = Path(known_hosts_path) if known_hosts_path is not None \
        else ssh_test.default_known_hosts_path()

    client = factory()
    try:
        client.load_system_host_keys()
    except Exception:
        pass
    if kh.is_file():
        try:
            client.load_host_keys(str(kh))
        except Exception:
            pass
    # 超时口径借自 V2.0.0 layer1_hpc(1w 跳板 sshd 实测慢,banner 可拖过 15s):
    # timeout=30 + banner/auth 显式给足,防"能 ping 通但握手超时"的假性连接失败
    kwargs = dict(hostname=profile.hostname, port=int(profile.port),
                  username=profile.username, timeout=30,
                  banner_timeout=45, auth_timeout=30,
                  allow_agent=False, look_for_keys=False)
    if profile.auth == 'key':
        kwargs['key_filename'] = profile.key_path
    else:
        kwargs['password'] = password

    jump = None
    try:
        trust = ssh_test.HostKeyTrust(trust_new)
        policy = ssh_test.PinnedHostKeyPolicy(trust)
        client.set_missing_host_key_policy(policy)
        if profile.use_jump and profile.jump_host:
            kwargs['sock'], jump = ssh_test._open_jump_channel(
                profile, password, factory, known_hosts_path=kh, _trust=trust)
        client.connect(**kwargs)
        ssh_test._persist_accepted_key(client, policy, kh)
    except ssh_test.UnknownHostKey as e:
        close_quiet(client, jump)
        raise ConnectError(
            str(e), needs_trust=True, fingerprint=e.fingerprint,
            algorithm=e.algorithm, host=e.host) from None
    except ssh_test.HostKeyPinMismatch as e:
        close_quiet(client, jump)
        raise ConnectError(str(e)) from None
    except paramiko.AuthenticationException:
        close_quiet(client, jump)
        raise ConnectError('认证失败:用户名/密钥/密码不正确') from None
    except paramiko.BadHostKeyException as e:
        close_quiet(client, jump)
        raise ConnectError(f'主机密钥已变化,已阻止连接:{e}') from None
    except paramiko.ssh_exception.SSHException as e:
        close_quiet(client, jump)
        raise ConnectError(f'SSH 协议错误:{e}') from None
    except (OSError, EOFError) as e:
        close_quiet(client, jump)
        raise ConnectError(f'连接失败:{e}') from None

    # keepalive 借自 V2.0.0(set_keepalive(30)):批量查询/慢集群下防 NAT 半路掐死空闲连接
    for c in (client, jump):
        if c is None:
            continue
        try:
            t = c.get_transport()
            if t is not None:
                t.set_keepalive(30)
        except Exception:
            pass

    return client, jump


def close_quiet(*clients):
    """静默关闭若干 client(None 项跳过)。GUI 层批量操作的 finally 复用。"""
    for c in clients:
        if c is None:
            continue
        try:
            c.close()
        except Exception:
            pass
