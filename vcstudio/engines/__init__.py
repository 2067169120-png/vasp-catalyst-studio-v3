"""多引擎适配层——VASP 主 + CP2K/Gaussian/Materials Studio(CASTEP) 文件级适配。

用户裁决:VASP 为主引擎(参照实现),其余三引擎做**文件级适配**(生成输入 + 解析
输出),软件本体一律用户自备。架构(采纳蓝图 E22):
- 科学层(CHE/ZPE/参考态)只产引擎无关的 CalcSpec,与引擎适配解耦;
- 跨引擎方法**不等价映射**必须显式弹清单逐项确认(NONEQUIV_MAP / nonequivalence_report),
  绝不静默翻译;
- 每引擎独立 checker(check_inputs);
- 混引擎比较有先行闸(equivalence.mixing_gate + reference_consistency_check)。

统一入口:get_backend(name) → Backend 实例(generate_inputs / parse_energy / check_inputs)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

from vcstudio.engines.calcspec import (
    HARTREE_TO_EV,
    ENGINE_RUN_CONTRACTS,
    EngineRunContract,
    KNOWN_ENGINES,
    NONEQUIV_MAP,
    VALID_TASKS,
    CalcSpec,
    EngineBackend,
    nonequivalence_report,
    parse_structure,
    get_run_contract,
    validate,
)
from vcstudio.engines.castep import CastepBackend
from vcstudio.engines.cp2k import Cp2kBackend
from vcstudio.engines.equivalence import mixing_gate, reference_consistency_check
from vcstudio.engines.gaussian import GaussianBackend
from vcstudio.engines.vasp import VaspBackend

# 注册表:引擎名(小写)→ Backend 类。别名收敛到规范名。
_BACKENDS = {
    'vasp': VaspBackend,
    'cp2k': Cp2kBackend,
    'gaussian': GaussianBackend,
    'castep': CastepBackend,
}
_ALIASES = {
    'g16': 'gaussian', 'g09': 'gaussian', 'gaussian16': 'gaussian',
    'materials_studio': 'castep', 'materials studio': 'castep',
    'ms': 'castep', 'castep/ms': 'castep',
}


def get_backend(name: str) -> EngineBackend:
    """引擎名 → Backend 实例。未知名 → ValueError(列出已注册引擎,绝不静默)。"""
    key = str(name).strip().lower()
    key = _ALIASES.get(key, key)
    if key not in _BACKENDS:
        raise ValueError(
            f'未知引擎 {name!r};已注册:{", ".join(sorted(_BACKENDS))}'
            f'(别名:{", ".join(sorted(_ALIASES))})。')
    return _BACKENDS[key]()


def available_engines() -> list:
    """已注册引擎规范名列表(稳定顺序)。"""
    return list(_BACKENDS.keys())


__all__ = [
    'CalcSpec', 'EngineBackend', 'validate', 'parse_structure',
    'NONEQUIV_MAP', 'nonequivalence_report', 'HARTREE_TO_EV',
    'ENGINE_RUN_CONTRACTS', 'EngineRunContract', 'get_run_contract',
    'VALID_TASKS', 'KNOWN_ENGINES',
    'get_backend', 'available_engines',
    'VaspBackend', 'Cp2kBackend', 'GaussianBackend', 'CastepBackend',
    'reference_consistency_check', 'mixing_gate',
]
