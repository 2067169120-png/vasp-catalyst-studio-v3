"""molbuild 分子建模引擎链——图片识别 → SMILES → 3D 建模(对标 starpivot-DFT)。

四段流水线,可选重依赖全部延迟 import、缺失降级为中文指引(绝不 ImportError 崩):
    - ocsr:            图片 → SMILES(DECIMER)+ SMILES → 2D SVG(RDKit)。
    - smiles3d:        SMILES → 3D(RDKit ETKDGv3 + MMFF/UFF)+ xyz/mol/POSCAR 导出。
    - molinfo:         化学式 / 电子数 / 分子量 / 多重度初猜(纯 python,零依赖)。
    - external_editor: 导出→外部编辑器(GaussView/Avogadro)手改→回读 的往返闭环。

顶层仅 import 各子模块(其内 numpy/rdkit/decimer 均延迟),故 import vcstudio.molbuild
零重依赖。中文注释允许,英文标识符。
"""
from __future__ import annotations

from vcstudio.molbuild import external_editor, molinfo, ocsr, smiles3d

__all__ = ['ocsr', 'smiles3d', 'molinfo', 'external_editor']
