"""GUI 纯逻辑 helpers:字段解析/校验、表单↔profile 映射、即时预览。**不 import tkinter**,可单测。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.generate.incar_builder import parse_incar, validate_and_complete_incar
from vcstudio.generate.kpoints import recommend_kpoints
from vcstudio.generate.poscar import parse_poscar_species, read_cell_vectors, read_poscar
from vcstudio.generate.potcar import PotcarError


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


# ── 即时预览(选完文件立刻看到,不必等生成) ──────────────────────────────────
def poscar_preview(path: str, calc_type: str = 'slab') -> str:
    """POSCAR 解析摘要(多行中文文本)。任何解析问题 → 单行友好提示,绝不抛异常。"""
    if not path or not os.path.isfile(path):
        return '(选择 POSCAR 后自动解析)'
    try:
        content = read_poscar(path)
    except (OSError, UnicodeDecodeError) as e:
        return f'⚠ POSCAR 读取失败:{e}'
    lines = [f'体系:{(content.splitlines() or [""])[0].strip() or "(无标题行)"}']
    elements, counts = parse_poscar_species(content)
    if not elements:
        return lines[0] + '\n⚠ 第 6 行无元素符号(VASP4/畸形):无法拼 POTCAR,请转 VASP5 格式'
    if counts and len(counts) == len(elements):
        pair = ' · '.join(f'{el} {c}' for el, c in zip(elements, counts))
        lines.append(f'元素/计数:{pair}(共 {sum(counts)} 原子)')
    else:
        lines.append(f'元素:{" ".join(elements)}(⚠ 计数行缺失,MAGMOM 将降级)')
    try:
        cell = read_cell_vectors(content)
        lens = [math.sqrt(sum(c * c for c in v)) for v in cell]
        lines.append('晶格 |a| |b| |c|:' + ' / '.join(f'{x:.2f}' for x in lens) + ' Å')
        kpts = recommend_kpoints(cell, calc_type)
        lines.append(f'推荐 K 网格({calc_type}):{kpts[0]} × {kpts[1]} × {kpts[2]}')
    except (ValueError, NotImplementedError) as e:
        lines.append(f'⚠ 晶格解析:{e}')
    return '\n'.join(lines)


def incar_preview(incar_path: str, poscar_path: str, lib_root: str,
                  validate: bool = True) -> str:
    """INCAR 校验预览:生成前预告"将补全什么/警告什么"。绝不抛异常、绝不写文件。"""
    if not incar_path or not os.path.isfile(incar_path):
        return '(选择 INCAR 后自动预览校验)'
    if not validate:
        return '校验补全已关闭:INCAR 将严格照抄,不追加任何键。'
    try:
        incar_dict = parse_incar(read_poscar(incar_path))
    except (OSError, UnicodeDecodeError) as e:
        return f'⚠ INCAR 读取失败:{e}'
    if not poscar_path or not os.path.isfile(poscar_path):
        return '(选择 POSCAR 后可预览 ENCUT/MAGMOM 补全)'
    elements, counts = parse_poscar_species(read_poscar(poscar_path))
    if not elements:
        return '⚠ POSCAR 无元素行,无法预览补全'
    if not lib_root:
        return '⚠ 未设置赝势库路径,无法预览 ENCUT 补全'
    try:
        completions, warnings = validate_and_complete_incar(
            incar_dict, elements, counts, lib_root)
    except PotcarError as e:
        return f'⚠ 无法预览补全:{e}'
    except (OSError, ValueError) as e:
        return f'⚠ 预览失败:{e}'
    lines = []
    if completions:
        lines.append('将补全(追加于 INCAR 文末,原文一字不改):')
        lines.extend(f'  {k} = {v}' for k, v in completions.items())
    else:
        lines.append('无缺项:INCAR 原文透传,不追加任何键。')
    lines.extend(f'⚠ {w}' for w in warnings)
    return '\n'.join(lines)


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
