"""光学化学结构识别 OCSR(图片 → SMILES)——对标 starpivot-DFT 的"图片识别"入口。

用户把文献插图/手绘/截图里的分子拍下来,本模块用 DECIMER(深度学习 OCSR 模型)识别成
SMILES,再交 smiles3d 建 3D。DECIMER 为可选重依赖(含 TensorFlow 模型,体积大、首次
运行需联网下模型):模块级 ``_lazy_decimer()`` 延迟 import,缺失时 probe/识别均返回结构化
中文指引(绝不 ImportError 崩)。2D 键线式预览图用 RDKit 生成 SVG,缺 RDKit 时降级提示。

``_lazy_decimer`` / ``_lazy_rdkit`` 独立成函数,便于测试 monkeypatch 注入假模块。
纯函数(异常统一转中文 error)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import time

_DECIMER_MISSING = ('未安装 DECIMER:pip install decimer'
                    '(约需下载深度学习模型,体积大,首次运行较慢)')
_RDKIT_MISSING = ('未安装 rdkit(pip install rdkit):无法生成 2D 键线式结构预览图')


def _lazy_decimer():
    """延迟 import DECIMER(pip 包名 decimer),返回模块;缺失 → None。便于测试注入。"""
    try:
        import DECIMER                                         # 官方 import 名为大写
        return DECIMER
    except ImportError:
        pass
    try:
        import decimer                                         # 兼容小写别名
        return decimer
    except ImportError:
        return None


def _lazy_rdkit():
    """延迟 import RDKit 绘图件,返回 ``(Chem, rdMolDraw2D)``;缺失 → None。便于测试注入。"""
    try:
        from rdkit import Chem
        from rdkit.Chem.Draw import rdMolDraw2D
    except ImportError:
        return None
    return Chem, rdMolDraw2D


def probe() -> dict:
    """探测 DECIMER 可用性。返回 ``{'available','detail'}``(缺失时 detail 为安装指引)。"""
    if _lazy_decimer() is None:
        return {'available': False, 'detail': _DECIMER_MISSING}
    return {'available': True,
            'detail': 'DECIMER 可用:可将分子结构图片识别为 SMILES'}


def image_to_smiles(image_path) -> dict:
    """图片 → SMILES(调 DECIMER.predict_SMILES)。

    返回 ``{'ok','smiles','elapsed_ms','error'}``:缺 DECIMER / 文件不存在 / 识别异常 /
    返回空串 → ok=False + 中文 error(不抛)。elapsed_ms 为识别耗时(毫秒)。
    """
    decimer = _lazy_decimer()
    if decimer is None:
        return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0, 'error': _DECIMER_MISSING}
    if not os.path.isfile(image_path):
        return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0,
                'error': f'图片文件不存在:{image_path}'}
    t0 = time.perf_counter()
    try:
        smiles = decimer.predict_SMILES(str(image_path))
    except Exception as e:                                     # noqa: BLE001 DECIMER/TF 异常繁杂,统一转中文不抛
        return {'ok': False, 'smiles': '', 'elapsed_ms': _ms_since(t0),
                'error': f'DECIMER 识别失败:{e}'}
    elapsed = _ms_since(t0)
    smiles = (smiles or '').strip()
    if not smiles:
        return {'ok': False, 'smiles': '', 'elapsed_ms': elapsed,
                'error': 'DECIMER 返回空 SMILES(图片可能非化学结构或过于模糊)'}
    return {'ok': True, 'smiles': smiles, 'elapsed_ms': elapsed, 'error': ''}


def smiles_svg(smiles, *, width: int = 400, height: int = 300) -> dict:
    """SMILES → 2D 键线式 SVG(RDKit rdMolDraw2D,默认 400×300)。

    返回 ``{'ok','svg','error'}``:缺 RDKit / SMILES 非法 / 绘图异常 → ok=False + 中文
    error(不抛)。
    """
    lazy = _lazy_rdkit()
    if lazy is None:
        return {'ok': False, 'svg': '', 'error': _RDKIT_MISSING}
    chem, rd_draw = lazy
    mol = chem.MolFromSmiles(smiles)
    if mol is None:
        return {'ok': False, 'svg': '',
                'error': f'SMILES 解析失败:{smiles!r}(非法或含 RDKit 不支持的记法)'}
    try:
        drawer = rd_draw.MolDraw2DSVG(int(width), int(height))
        drawer.DrawMolecule(mol)
        drawer.FinishDrawing()
        svg = drawer.GetDrawingText()
    except Exception as e:                                     # noqa: BLE001 RDKit C++ 绘图异常统一转中文
        return {'ok': False, 'svg': '', 'error': f'RDKit 绘图失败:{e}'}
    return {'ok': True, 'svg': svg, 'error': ''}


def _ms_since(t0: float) -> float:
    """自 t0(perf_counter)起的毫秒数,保留 1 位小数。"""
    return round((time.perf_counter() - t0) * 1000.0, 1)
