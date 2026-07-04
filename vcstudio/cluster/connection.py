"""可复用 SSH 连接:提交/查状态用的活连接(区别于 ssh_test 的一次性测连)。

安全语义与 ssh_test 完全一致(同一套 known_hosts 门控,跳板机复用
ssh_test._open_jump_channel,不重复实现 MITM 防线)。paramiko 延迟导入,
纯逻辑层(schedulers/script_builder/ledger)在无 paramiko 环境下仍可测试。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from pathlib import Path


class ConnectError(Exception):
    """连接失败。needs_trust=True 表示未知主机指纹,界面确认后可 trust_new 重试。"""

    def __init__(self, message: str, needs_trust: bool = False):
        super().__init__(message)
        self.needs_trust = needs_trust


def open_client(profile, password: str | None = None, *,
                trust_new: bool = False,
                known_hosts_path: str | os.PathLike | None = None,
                client_factory=None):
    """连上集群,返回 (client, jump_client|None)。调用方负责两个都 close。

    失败抛 ConnectError(中文文案;未知指纹时 needs_trust=True)。
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
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy() if trust_new else paramiko.RejectPolicy())

    kwargs = dict(hostname=profile.hostname, port=int(profile.port),
                  username=profile.username, timeout=15,
                  allow_agent=False, look_for_keys=False)
    if profile.auth == 'key':
        kwargs['key_filename'] = profile.key_path
    else:
        kwargs['password'] = password

    jump = None
    try:
        if profile.use_jump and profile.jump_host:
            kwargs['sock'], jump = ssh_test._open_jump_channel(
                profile, password, factory, trust_new=trust_new, known_hosts_path=kh)
        client.connect(**kwargs)
    except paramiko.AuthenticationException:
        close_quiet(client, jump)
        raise ConnectError('认证失败:用户名/密钥/密码不正确') from None
    except paramiko.ssh_exception.SSHException as e:
        close_quiet(client, jump)
        raise ConnectError(f'无法确认主机指纹或 SSH 错误:{e};确认后可信任重试',
                           needs_trust=not trust_new) from None
    except (OSError, EOFError) as e:
        close_quiet(client, jump)
        raise ConnectError(f'连接失败:{e}') from None

    if trust_new:
        try:
            kh.parent.mkdir(parents=True, exist_ok=True)
            client.save_host_keys(str(kh))
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
