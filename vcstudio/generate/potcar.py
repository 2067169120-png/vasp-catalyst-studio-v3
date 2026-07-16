"""PAW_PBE 赝势变体映射 + 本地拼接 + ENMAX≤ENCUT 自洽校验。

复用自 E: layer2_science/potcar_variant.py;唯一改动:去掉硬编码 DEFAULT_LIB_ROOT,
lib_root 缺省时从 vcstudio.shared.config 读本地库根。新增 max_enmax(供 INCAR 校验补 ENCUT)。
按 elements 物种顺序拼接,保证 POTCAR 与 POSCAR/MAGMOM 一致;ENMAX>ENCUT 抛错绝不静默。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re

from vcstudio.shared.config import get_potcar_lib_root

# 元素 → PAW_PBE 变体(经用户 potpaw54 库 ENMAX≤ENCUT 核对坐实)
POTCAR_VARIANT = {
    'C': 'C', 'N': 'N', 'Li': 'Li', 'S': 'S',
    'B': 'B', 'P': 'P',
    'V': 'V_sv', 'Nb': 'Nb_sv', 'Ta': 'Ta_pv',
    'Cr': 'Cr_pv', 'Mn': 'Mn_pv',
    'Fe': 'Fe', 'Co': 'Co', 'Ni': 'Ni', 'Zn': 'Zn',
    'Ti': 'Ti_pv', 'Cu': 'Cu', 'Mo': 'Mo_pv', 'Ru': 'Ru_pv',
    'Pd': 'Pd', 'W': 'W_pv', 'Pt': 'Pt',
    'Sc': 'Sc_sv', 'Y': 'Y_sv', 'Zr': 'Zr_sv', 'Hf': 'Hf_pv',
    'Tc': 'Tc_pv', 'Re': 'Re_pv', 'Os': 'Os_pv', 'Ir': 'Ir',
    'Rh': 'Rh_pv', 'Ag': 'Ag', 'Au': 'Au', 'Cd': 'Cd',
    # ── 2026-07-02 扩充:28 元素,逐一对用户 potpaw_PBE.54 全集核对 TITEL+ENMAX ──
    # 吸附/催化刚需(*H/*O/*OH/H2O、氧化物载体):
    'H': 'H',          # ENMAX 250.0
    'O': 'O',          # ENMAX 400.0(全表最大;补全规则 1.3×max 取整 → ENCUT 520)
    # 卤素:
    'F': 'F',          # 400.0
    'Cl': 'Cl',        # 262.5
    'Br': 'Br',        # 216.3
    'I': 'I',          # 175.6
    # 碱金属 / 碱土(锂硫/掺杂/载体常用):
    'Na': 'Na_pv',     # 259.6
    'K': 'K_sv',       # 259.3
    'Rb': 'Rb_sv',     # 220.1
    'Cs': 'Cs_sv',     # 220.3
    'Be': 'Be',        # 247.5
    'Mg': 'Mg',        # 126.1
    'Ca': 'Ca_sv',     # 266.6
    'Sr': 'Sr_sv',     # 229.4
    'Ba': 'Ba_sv',     # 187.2
    # 主族 p 区:
    'Al': 'Al',        # 240.3
    'Si': 'Si',        # 245.3
    'Ga': 'Ga_d',      # 282.7
    'Ge': 'Ge_d',      # 310.3
    'As': 'As',        # 208.7
    'Se': 'Se',        # 211.6
    'Sn': 'Sn_d',      # 241.1
    'Sb': 'Sb',        # 172.1
    'Te': 'Te',        # 175.0
    'Pb': 'Pb_d',      # 237.8
    'Bi': 'Bi_d',      # 242.8
    # 镧系(载体/助剂;Ce 做 CeO2 请自行在 INCAR 给 DFT+U,工具不代设):
    'La': 'La',        # 219.3
    'Ce': 'Ce',        # 273.0
}

_ENMAX_RE = re.compile(r'ENMAX\s*=\s*([0-9.]+)')


class PotcarError(Exception):
    """赝势变体缺失、库文件缺失或 ENMAX 超 ENCUT 时抛出(绝不静默)。"""


def _resolve_root(lib_root: str | None) -> str:
    root = lib_root if lib_root is not None else get_potcar_lib_root()
    if not root:
        raise PotcarError(
            '未配置 PAW_PBE 赝势库路径(potcar_lib_root 为空)。'
            '请复制 config.example.yaml 为 config.yaml 填写,'
            '或在「生成」页设置赝势库目录,或传入 --lib-root。')
    return root


def variant(element: str) -> str:
    """元素符号 → PAW_PBE 变体目录名。未登记元素抛 PotcarError。"""
    try:
        return POTCAR_VARIANT[element]
    except KeyError:
        raise PotcarError(
            f'元素 {element!r} 未在 POTCAR_VARIANT 登记;'
            f'新增金属请先复核其 ENMAX≤ENCUT 再入表'
        ) from None


def read_enmax(variant_name: str, lib_root: str | None = None) -> float:
    """从 ``{lib_root}/{variant_name}/POTCAR`` 读取 ENMAX(eV)。

    variant_name 是变体目录名(如 'Ta_pv'),不是元素符号。
    文件缺失或无 ENMAX 行 → PotcarError。lib_root 缺省从 config 读。
    """
    root = _resolve_root(lib_root)
    path = os.path.join(root, variant_name, 'POTCAR')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(4096)  # ENMAX 在文件头部
    except FileNotFoundError:
        raise PotcarError(f'POTCAR 不存在: {path}')
    m = _ENMAX_RE.search(head)
    if not m:
        raise PotcarError(f'POTCAR 缺 ENMAX 行: {path}')
    return float(m.group(1))


_TITEL_RE = re.compile(r'TITEL\s*=\s*(.+)')


def potcar_provenance(elements: list, lib_root: str | None = None,
                      _force_variant: dict | None = None) -> list:
    """赝势身份溯源:按物种顺序返回 [{'element','variant','titel','enmax'}]。

    发刊级 provenance(审查#2):方法学论文必须能回答"用的哪套 POTCAR"——
    TITEL 行(如 'PAW_PBE Ta_pv 07Sep2000')+ ENMAX 落入 job.yaml,结果永远可追。
    与 build_potcar 同源读库;文件缺失/缺行 → PotcarError(绝不静默)。
    """
    root = _resolve_root(lib_root)
    override = _force_variant or {}
    out = []
    for el in elements:
        v = override.get(el) or variant(el)
        path = os.path.join(root, v, 'POTCAR')
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                head = f.read(4096)
        except FileNotFoundError:
            raise PotcarError(f'POTCAR 不存在: {path}')
        tm = _TITEL_RE.search(head)
        em = _ENMAX_RE.search(head)
        if not em:
            raise PotcarError(f'POTCAR 缺 ENMAX 行: {path}')
        out.append({'element': el, 'variant': v,
                    'titel': tm.group(1).strip() if tm else '',
                    'enmax': float(em.group(1))})
    return out


def build_potcar(elements: list, encut: int = 400,
                 lib_root: str | None = None,
                 _force_variant: dict | None = None) -> str:
    """按 elements 物种顺序拼接本地 PAW_PBE POTCAR,返回拼接文本。

    - 变体由 POTCAR_VARIANT 决定(``_force_variant`` 仅供测试覆盖)。
    - 每个变体校验存在且 ENMAX ≤ encut;任一不满足 → PotcarError(绝不静默)。
    - elements 必须为 POSCAR 物种顺序。lib_root 缺省从 config 读。
    """
    root = _resolve_root(lib_root)
    override = _force_variant or {}
    chunks: list = []
    for el in elements:
        v = override.get(el) or variant(el)
        enmax = read_enmax(v, root)
        if enmax > encut:
            raise PotcarError(
                f'{el}({v}) ENMAX={enmax} 超过 ENCUT={encut};'
                f'换低价电子变体或提高 ENCUT'
            )
        path = os.path.join(root, v, 'POTCAR')
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            chunks.append(f.read())
    return ''.join(chunks)


def max_enmax(elements: list, lib_root: str | None = None) -> float:
    """各元素 ENMAX 的最大值(供 INCAR 缺 ENCUT 时按 1.3×max 补全)。

    elements 为空 → ValueError(调用方须保证非空)。
    """
    root = _resolve_root(lib_root)
    if not elements:
        raise ValueError('max_enmax: elements 为空')
    return max(read_enmax(variant(el), root) for el in elements)
