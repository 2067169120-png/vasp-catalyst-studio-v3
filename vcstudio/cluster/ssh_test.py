"""集群连接测试:paramiko 真连 → whoami + 调度器探测。M2 提交前的"配好没"校验。

主机指纹策略(防 MITM):默认加载 known_hosts,遇未知主机不静默信任,返回
needs_trust=True 交界面确认;确认后 trust_new=True 再连并落 known_hosts。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import paramiko

from vcstudio.shared.config import user_config_dir

# 调度器 → 探测命令关键字(存在即判定)
_SCHED_PROBES = [('Slurm', 'sbatch'), ('PBS', 'qsub'), ('LSF', 'bsub')]


@dataclass
class ConnectionResult:
    ok: bool
    whoami: str = ''
    scheduler: str = ''
    message: str = ''
    needs_trust: bool = False


def default_known_hosts_path() -> Path:
    return user_config_dir() / 'known_hosts'


def _detect_scheduler(client, scheduler_bin: str = '') -> str:
    """跑一条 command -v 探测,返回 Slurm/PBS/LSF/Shell。

    profile 填了 scheduler_bin 时补查该目录(1w 实情:torque 在 /opt/torque-6.1.2/bin,
    不在默认 PATH,只靠 command -v 会误报 Shell,把用户配置往错里带)。
    """
    probe = 'command -v sbatch qsub bsub 2>/dev/null'
    if scheduler_bin:
        b = scheduler_bin.rstrip('/')
        probe += f'; ls {b}/sbatch {b}/qsub {b}/bsub 2>/dev/null'
    _in, out, _err = client.exec_command(probe)
    found = out.read().decode(errors='replace')
    for name, exe in _SCHED_PROBES:
        if exe in found:
            return name
    return 'Shell'


def check_connection(profile, password: str | None = None, *,
                    trust_new: bool = False,
                    known_hosts_path: str | os.PathLike | None = None,
                    client_factory=None) -> ConnectionResult:
    """连一次集群并回报结果。client_factory 缺省 paramiko.SSHClient(测试可注入)。"""
    factory = client_factory or paramiko.SSHClient
    kh = Path(known_hosts_path) if known_hosts_path is not None else default_known_hosts_path()

    client = factory()
    jump = None
    try:
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

        # 超时口径与 connection.open_client 一致(1w 跳板 sshd 慢,15s 不够)
        connect_kwargs = dict(hostname=profile.hostname, port=int(profile.port),
                              username=profile.username, timeout=30,
                              banner_timeout=45, auth_timeout=30,
                              allow_agent=False, look_for_keys=False)
        if profile.auth == 'key':
            connect_kwargs['key_filename'] = profile.key_path
        else:
            connect_kwargs['password'] = password

        if profile.use_jump and profile.jump_host:
            connect_kwargs['sock'], jump = _open_jump_channel(
                profile, password, factory, trust_new=trust_new, known_hosts_path=kh)

        try:
            client.connect(**connect_kwargs)
        except paramiko.AuthenticationException:
            return ConnectionResult(ok=False, message='认证失败:用户名/密钥/密码不正确')
        except paramiko.ssh_exception.SSHException as e:
            # 未知主机指纹或 SSH 协议层错误 → 请界面确认信任
            return ConnectionResult(ok=False, needs_trust=not trust_new,
                              message=f'无法确认主机指纹或 SSH 错误:{e};确认后可信任重试')
        except (OSError, EOFError) as e:  # 网络不可达/超时等
            return ConnectionResult(ok=False, message=f'连接失败:{e}')

        _in, out, _err = client.exec_command('whoami')
        who = out.read().decode(errors='replace').strip()
        sched = _detect_scheduler(client, getattr(profile, 'scheduler_bin', ''))

        if trust_new:
            try:
                kh.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(kh))
            except Exception:
                pass

        return ConnectionResult(ok=True, whoami=who, scheduler=sched,
                          message=f'已连上 {profile.hostname},whoami={who},检测到 {sched}')
    finally:
        for c in (client, jump):
            if c is None:
                continue
            try:
                c.close()
            except Exception:
                pass


def _open_jump_channel(profile, password, factory, *,
                       trust_new: bool = False,
                       known_hosts_path: str | os.PathLike | None = None):
    """经跳板机开 direct-tcpip 通道。返回 (sock, jump_client),jump 由调用方负责 close。

    跳板机(bastion)同样按 trust_new 门控主机指纹:未知指纹默认拒绝(防 MITM),
    与主连接路径一致,不因经跳板机而放宽。
    """
    jump = factory()
    try:
        try:
            jump.load_system_host_keys()
        except Exception:
            pass
        if known_hosts_path is not None and Path(known_hosts_path).is_file():
            try:
                jump.load_host_keys(str(known_hosts_path))
            except Exception:
                pass
        jump.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if trust_new else paramiko.RejectPolicy())
        jkwargs = dict(hostname=profile.jump_host, port=int(profile.jump_port),
                       username=profile.jump_user or profile.username, timeout=30,
                       banner_timeout=45, auth_timeout=30,
                       allow_agent=False, look_for_keys=False)
        if profile.auth == 'key':
            jkwargs['key_filename'] = profile.key_path
        else:
            jkwargs['password'] = password
        jump.connect(**jkwargs)
        transport = jump.get_transport()
        sock = transport.open_channel(
            'direct-tcpip', (profile.hostname, int(profile.port)), ('127.0.0.1', 0))
        return sock, jump
    except BaseException:
        try:
            jump.close()
        except Exception:
            pass
        raise
