"""集群连接测试:paramiko 真连 → whoami + 调度器探测。M2 提交前的"配好没"校验。

主机指纹策略(防 MITM):未知主机时从 Paramiko 的 missing_host_key
回调捕获真实公钥,返回 OpenSSH 格式 SHA256 指纹。第二次连接只接受
``{'host': <上次返回的 host>, 'fingerprint': 'SHA256:...'}``这样的精确 pin;
历史布尔 ``True`` 绝不会触发 AutoAdd。跳板机与目标机各自校验并落盘。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Mapping
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
    fingerprint: str = ''
    algorithm: str = ''
    host: str = ''


def host_key_fingerprint(key) -> str:
    """返回 OpenSSH 显示的 SHA256:<base64> 指纹(不带 base64 填充)。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')


class UnknownHostKey(paramiko.SSHException):
    """missing_host_key 回调捕获到的真实未知公钥。"""

    def __init__(self, host: str, key):
        self.host = str(host)
        self.algorithm = str(key.get_name())
        self.fingerprint = host_key_fingerprint(key)
        super().__init__(
            f'首次连接主机 {self.host};请核对 {self.algorithm} '
            f'指纹 {self.fingerprint} 后再确认')


class HostKeyPinMismatch(paramiko.SSHException):
    """本次公钥与用户明确确认的 host/指纹不一致;不可绕过。"""

    def __init__(self, host: str, fingerprint: str, algorithm: str, reason: str):
        self.host = str(host)
        self.fingerprint = str(fingerprint)
        self.algorithm = str(algorithm)
        super().__init__(f'主机密钥校验失败:{reason}')


def _normalise_pin(trust_new) -> dict | None:
    """只把完整映射视为 pin;布尔 True 与 False 一样不授权信任。"""
    if not isinstance(trust_new, Mapping):
        return None
    host = str(trust_new.get('host') or '').strip()
    fingerprint = str(trust_new.get('fingerprint') or '').strip()
    algorithm = str(trust_new.get('algorithm') or '').strip()
    if not host or not fingerprint:
        raise HostKeyPinMismatch('', '', '', '确认信息缺少 host 或 SHA256 指纹')
    if not fingerprint.startswith('SHA256:'):
        raise HostKeyPinMismatch(host, fingerprint, algorithm, '仅接受 SHA256 指纹')
    return {'host': host, 'fingerprint': fingerprint, 'algorithm': algorithm}


class HostKeyTrust:
    """一次连接链(跳板+目标)共享的 pin,最多只能消费一次。"""

    def __init__(self, trust_new=False):
        self.pin = _normalise_pin(trust_new)
        self.consumed = False


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """捕获未知 key;仅 pin 与本次回调的 host+SHA256 完全一致时加入内存。"""

    def __init__(self, trust_new=False):
        self.trust = trust_new if isinstance(trust_new, HostKeyTrust) \
            else HostKeyTrust(trust_new)
        self.pin = self.trust.pin
        self.accepted = False

    def missing_host_key(self, client, hostname, key):
        host = str(hostname)
        algorithm = str(key.get_name())
        fingerprint = host_key_fingerprint(key)
        # 一个用户确认只授权一台未知主机。若跳板机刚消费 pin,
        # 目标机仍未知,必须把目标的真实指纹再返回界面确认一次。
        if self.pin is None or self.trust.consumed:
            raise UnknownHostKey(host, key)
        if self.pin['host'] != host:
            raise HostKeyPinMismatch(
                host, fingerprint, algorithm,
                f"确认的主机 {self.pin['host']} 与本次主机 {host} 不一致")
        if self.pin['fingerprint'] != fingerprint:
            raise HostKeyPinMismatch(
                host, fingerprint, algorithm,
                f"公钥指纹已变化(确认值 {self.pin['fingerprint']},本次 {fingerprint})")
        if self.pin['algorithm'] and self.pin['algorithm'] != algorithm:
            raise HostKeyPinMismatch(
                host, fingerprint, algorithm,
                f"公钥算法不一致(确认值 {self.pin['algorithm']},本次 {algorithm})")
        client.get_host_keys().add(host, algorithm, key)
        self.accepted = True
        self.trust.consumed = True


def _persist_accepted_key(client, policy: PinnedHostKeyPolicy, kh: Path) -> None:
    """只持久化本次精确 pin 验证通过的 key;写盘失败不得伪报成功。"""
    if not policy.accepted:
        return
    try:
        kh.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(kh))
        try:
            os.chmod(kh, 0o600)
        except OSError:
            pass
    except Exception as exc:
        raise paramiko.SSHException(f'已验证指纹,但无法写入 known_hosts:{exc}') from exc


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
                    trust_new=False,
                    known_hosts_path: str | os.PathLike | None = None,
                    client_factory=None) -> ConnectionResult:
    """连一次集群并回报结果。

    trust_new 只接受首次结果原样回传的
    ``{'host', 'fingerprint', 'algorithm'?}``;client_factory 供测试注入。
    """
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
        # 超时口径与 connection.open_client 一致(1w 跳板 sshd 慢,15s 不够)
        connect_kwargs = dict(hostname=profile.hostname, port=int(profile.port),
                              username=profile.username, timeout=30,
                              banner_timeout=45, auth_timeout=30,
                              allow_agent=False, look_for_keys=False)
        if profile.auth == 'key':
            connect_kwargs['key_filename'] = profile.key_path
        else:
            connect_kwargs['password'] = password

        try:
            trust = HostKeyTrust(trust_new)
            policy = PinnedHostKeyPolicy(trust)
            client.set_missing_host_key_policy(policy)
            if profile.use_jump and profile.jump_host:
                connect_kwargs['sock'], jump = _open_jump_channel(
                    profile, password, factory, known_hosts_path=kh,
                    _trust=trust)
            client.connect(**connect_kwargs)
            _persist_accepted_key(client, policy, kh)
        except UnknownHostKey as e:
            return ConnectionResult(
                ok=False, needs_trust=True, message=str(e),
                fingerprint=e.fingerprint, algorithm=e.algorithm, host=e.host)
        except HostKeyPinMismatch as e:
            return ConnectionResult(ok=False, message=str(e))
        except paramiko.AuthenticationException:
            return ConnectionResult(ok=False, message='认证失败:用户名/密钥/密码不正确')
        except paramiko.BadHostKeyException as e:
            return ConnectionResult(
                ok=False, message=f'主机密钥已变化,已阻止连接:{e}')
        except paramiko.ssh_exception.SSHException as e:
            # 只有 UnknownHostKey 能 needs_trust;协议错误绝不能伪装成可信任。
            return ConnectionResult(ok=False, message=f'SSH 协议错误:{e}')
        except (OSError, EOFError) as e:  # 网络不可达/超时等
            return ConnectionResult(ok=False, message=f'连接失败:{e}')

        _in, out, _err = client.exec_command('whoami')
        who = out.read().decode(errors='replace').strip()
        sched = _detect_scheduler(client, getattr(profile, 'scheduler_bin', ''))

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
                       trust_new=False,
                       known_hosts_path: str | os.PathLike | None = None,
                       _trust: HostKeyTrust | None = None):
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
        trust = _trust or HostKeyTrust(trust_new)
        policy = PinnedHostKeyPolicy(trust)
        jump.set_missing_host_key_policy(policy)
        jkwargs = dict(hostname=profile.jump_host, port=int(profile.jump_port),
                       username=profile.jump_user or profile.username, timeout=30,
                       banner_timeout=45, auth_timeout=30,
                       allow_agent=False, look_for_keys=False)
        if profile.auth == 'key':
            jkwargs['key_filename'] = profile.key_path
        else:
            jkwargs['password'] = password
        jump.connect(**jkwargs)
        kh = Path(known_hosts_path) if known_hosts_path is not None \
            else default_known_hosts_path()
        _persist_accepted_key(jump, policy, kh)
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
