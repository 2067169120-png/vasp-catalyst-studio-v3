"""GUI 纯逻辑 helpers:字段解析/校验、表单↔profile 映射。**不 import tkinter**,可单测。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os

from vcstudio.cluster.profiles import ClusterProfile


def parse_kpoints_field(text: str):
    """KPOINTS 输入框 → [kx,ky,kz] 或 None(自动)。非法抛 ValueError(中文文案)。"""
    t = (text or '').strip()
    if t == '' or t.lower() in ('auto', '自动'):
        return None
    parts = t.split()
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        raise ValueError('KPOINTS 需 3 个整数,如 "5 5 1"')
    if len(nums) != 3:
        raise ValueError('KPOINTS 需恰好 3 个整数,如 "5 5 1"')
    return nums


def validate_generate_inputs(poscar: str, incar: str, out_dir: str, lib_root: str) -> list:
    """生成前预检,返回错误文案列表(空=通过)。"""
    errs = []
    if not lib_root:
        errs.append('未设置赝势库(POTCAR lib)路径')
    if not poscar or not os.path.isfile(poscar):
        errs.append('POSCAR 文件不存在或未选择')
    if not incar or not os.path.isfile(incar):
        errs.append('INCAR 文件不存在或未选择')
    if not out_dir:
        errs.append('未设置输出目录')
    return errs


def profile_from_form(name: str, fields: dict) -> ClusterProfile:
    """把界面字段(字符串为主)映射成 ClusterProfile。port 容错为 int。"""
    def _int(v, default):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default
    return ClusterProfile(
        name=name,
        hostname=(fields.get('hostname') or '').strip(),
        port=_int(fields.get('port'), 22),
        username=(fields.get('username') or '').strip(),
        auth=fields.get('auth') or 'key',
        key_path=(fields.get('key_path') or '').strip(),
        use_jump=bool(fields.get('use_jump')),
        jump_host=(fields.get('jump_host') or '').strip(),
        jump_user=(fields.get('jump_user') or '').strip(),
        jump_port=_int(fields.get('jump_port'), 22),
        remote_root=(fields.get('remote_root') or '').strip(),
        scheduler=fields.get('scheduler') or 'Slurm',
    )


def validate_cluster_inputs(profile: ClusterProfile, has_password: bool) -> list:
    """集群配置预检(测连接/保存前)。has_password:是否已有可用密码。"""
    errs = []
    if not profile.name:
        errs.append('未填集群名称')
    if not profile.hostname:
        errs.append('未填主机名')
    if not profile.username:
        errs.append('未填用户名')
    if profile.auth == 'key':
        # 只校验"是否填了密钥路径";路径存在性交由实际连接时报错
        # (与测试契约一致:key_path 非空即视为已选择)。
        if not profile.key_path:
            errs.append('SSH 密钥文件未选择')
    else:
        if not has_password:
            errs.append('密码认证但未提供密码')
    if profile.use_jump and not profile.jump_host:
        errs.append('勾选了跳板机但未填跳板主机')
    return errs
