"""金属 slab 建模(F2 前置):fcc/bcc/hcp 低指数晶面的确定性 N 层 slab 构造 + 可再生配方。

解决层厚收敛的"入口"问题(v3.2.1 Backlog #2):层厚收敛系列必须**重建**不同层数的
slab,而裸 CONTCAR 不含体相晶胞/米勒面/终止面信息,无法再生——所以入口必须在建模
流程:本模块建 slab 时同步产出**配方(recipe)**,写入 job.yaml;之后任何时刻都能由
``slab_builder_from_recipe(recipe)`` 还原出 ``fn(n_layers)->POSCAR``,喂给
``conv_scan.build_slab_thickness_series`` 真正生成层厚系列(不再诚实报错)。

几何为教科书级确定性构造(纯 numpy,不引入 pymatgen/ASE;ASE 仅在 dev 测试里作交叉
验证参照),覆盖催化最常用低指数面:

- fcc(111):六方表面胞 a_s=a/√2,层距 a/√3,ABC 堆垛(逐层面内平移 (a1+a2)/3);
- fcc(100):正方胞 a_s=a/√2,层距 a/2,AB 堆垛(平移 (1/2,1/2));
- fcc(110):矩形胞 a/√2 × a,层距 a/(2√2),AB 堆垛(平移 (1/2,1/2));
- bcc(100):正方胞 a,层距 a/2,AB 堆垛(平移 (1/2,1/2));
- bcc(110):矩形胞 a × a√2(每层含心,2 原子/表面胞),层距 a/√2,AB 堆垛(平移 (1/2,0));
- hcp(0001):六方胞 a(60° 约定,同 sac_builder 石墨烯),层距 c/2,AB 堆垛(平移 (a1+a2)/3)。

科学正确红线:
- 内置晶格常数为**实验值初猜**(LATTICE_GUESS),发表口径必须用同泛函 EOS/晶胞优化
  重新确定并显式传入——build 结果始终附中文 warning 提醒,绝不静默。
- 结构/晶面组合不在支持表(_MILLERS)内 → ValueError 中文报错,不编造几何。
- 每次构造后自检:层数(slab_builder.count_layers)与真空(vacuum_thickness)必须
  与请求一致,最近邻距不塌缩(< 0.8×理论 NN 视为内部错误)——防"看着成功"的错构型。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import math

import numpy as np

from vcstudio.generate.sac_builder import cart_to_frac, frac_to_cart, write_poscar
from vcstudio.generate.slab_builder import (count_layers, fix_bottom_layers,
                                            min_interatomic_distance,
                                            vacuum_thickness)

DEFAULT_VACUUM = 15.0    # Å slab 真空层缺省(层厚/真空可另做收敛扫描)
_IDEAL_C_OVER_A = math.sqrt(8.0 / 3.0)   # hcp 理想轴比(缺 c 时的推算基准,附 warning)

# 实验晶格常数初猜表(Å,室温常用值;**非发表口径**,须同泛函 EOS/晶胞优化定终值)。
# fcc/bcc: {'a': ...};hcp: {'a': ..., 'c': ...}。
LATTICE_GUESS = {
    'fcc': {'Al': 4.050, 'Ni': 3.524, 'Cu': 3.615, 'Rh': 3.803, 'Pd': 3.891,
            'Ag': 4.086, 'Ir': 3.839, 'Pt': 3.924, 'Au': 4.078},
    'bcc': {'Fe': 2.866, 'Cr': 2.885, 'Mo': 3.147, 'W': 3.165, 'Ta': 3.306,
            'Nb': 3.301, 'V': 3.024},
    'hcp': {'Co': {'a': 2.507, 'c': 4.069}, 'Ru': {'a': 2.706, 'c': 4.282},
            'Ti': {'a': 2.951, 'c': 4.684}, 'Zn': {'a': 2.665, 'c': 4.947},
            'Mg': {'a': 3.209, 'c': 5.211}, 'Zr': {'a': 3.232, 'c': 5.147},
            'Re': {'a': 2.761, 'c': 4.456}},
}

# 支持的(结构, 晶面)组合:值 = (每表面胞每层原子数, 堆垛周期)。不在表内 → 显式拒绝。
_MILLERS = {
    ('fcc', '111'): (1, 3),
    ('fcc', '100'): (1, 2),
    ('fcc', '110'): (1, 2),
    ('bcc', '100'): (1, 2),
    ('bcc', '110'): (2, 2),
    ('hcp', '0001'): (1, 2),
}


def supported_surfaces() -> list:
    """支持的 (structure, miller) 列表(前端下拉数据源),按结构分组稳定排序。"""
    return [{'structure': s, 'miller': m} for (s, m) in _MILLERS]


def lattice_guess(element: str, structure: str):
    """查内置实验晶格常数初猜 → {'a':...}(fcc/bcc)或 {'a':...,'c':...}(hcp)|None。"""
    table = LATTICE_GUESS.get(str(structure))
    if not table:
        return None
    val = table.get(str(element))
    if val is None:
        return None
    return dict(val) if isinstance(val, dict) else {'a': float(val)}


def _surface_mesh(structure: str, miller: str, a: float, c: float | None):
    """(结构, 晶面) → (面内二维基矢 a1/a2, 每层原子二维基, 层距 h, 逐层面内偏移序列)。

    返回 (a1, a2, basis2d, h, stack_offsets):第 k 层原子 = basis2d 各点 +
    stack_offsets[k % 周期](二维笛卡尔 Å),z = k·h。偏移用**显式序列**而非累积
    平移:fcc(111) ABC 是 [0, s, 2s](s=(a1+a2)/3),hcp(0001) ABAB 是 [0, s]——
    hcp 若写成累积 k·s 会得到错误的 ABC 堆垛(那是 fcc 而非 hcp)。
    """
    key = (str(structure), str(miller))
    if key not in _MILLERS:
        supported = '、'.join(f'{s}({m})' for (s, m) in _MILLERS)
        raise ValueError(f'不支持的结构/晶面组合 {structure}({miller});'
                         f'当前支持:{supported}。其余晶面待结构工坊二期(pymatgen)。')
    a = float(a)
    if a <= 0:
        raise ValueError(f'晶格常数 a 须为正数(Å),收到 {a!r}')
    zero = np.zeros(2)
    if key == ('fcc', '111'):
        d = a / math.sqrt(2.0)                       # 面内最近邻
        a1 = np.array([d, 0.0])
        a2 = np.array([d * 0.5, d * math.sqrt(3.0) / 2.0])
        s = (a1 + a2) / 3.0
        return a1, a2, [zero], a / math.sqrt(3.0), [zero, s, 2.0 * s]     # ABC
    if key == ('fcc', '100'):
        d = a / math.sqrt(2.0)
        a1 = np.array([d, 0.0])
        a2 = np.array([0.0, d])
        return a1, a2, [zero], a / 2.0, [zero, (a1 + a2) / 2.0]           # AB
    if key == ('fcc', '110'):
        d = a / math.sqrt(2.0)                       # 沿 [1-10]
        a1 = np.array([d, 0.0])
        a2 = np.array([0.0, a])                      # 沿 [001]
        return a1, a2, [zero], a / (2.0 * math.sqrt(2.0)), [zero, (a1 + a2) / 2.0]
    if key == ('bcc', '100'):
        a1 = np.array([a, 0.0])
        a2 = np.array([0.0, a])
        return a1, a2, [zero], a / 2.0, [zero, (a1 + a2) / 2.0]           # AB
    if key == ('bcc', '110'):
        a1 = np.array([a, 0.0])                      # 沿 [001]
        a2 = np.array([0.0, a * math.sqrt(2.0)])     # 沿 [1-10]
        basis = [zero, (a1 + a2) / 2.0]              # 含心矩形:角 + 心
        return a1, a2, basis, a / math.sqrt(2.0), [zero, a1 / 2.0]        # AB
    # hcp(0001):60° 六方胞(同 sac_builder 石墨烯约定),ABAB 堆垛
    if c is None or float(c) <= 0:
        raise ValueError('hcp(0001) 需正的 c 轴长度(Å);未知时可用 lattice_guess 或理想轴比')
    a1 = np.array([a, 0.0])
    a2 = np.array([a * 0.5, a * math.sqrt(3.0) / 2.0])
    return a1, a2, [zero], float(c) / 2.0, [zero, (a1 + a2) / 3.0]        # ABAB


def _theoretical_nn(structure: str, a: float, c: float | None) -> float:
    """体相理论最近邻距(Å):fcc a/√2;bcc √3a/2;hcp min(a, √(a²/3+c²/4))。"""
    a = float(a)
    if structure == 'fcc':
        return a / math.sqrt(2.0)
    if structure == 'bcc':
        return a * math.sqrt(3.0) / 2.0
    return min(a, math.sqrt(a * a / 3.0 + float(c) * float(c) / 4.0))


def build_metal_slab(element, structure='fcc', miller='111', layers=4, *,
                     a=None, c=None, nx=3, ny=3, vacuum=DEFAULT_VACUUM,
                     fix_bottom=0, orthogonal_note=True) -> dict:
    """构造金属 slab → {'poscar','recipe','description','warnings','natoms'}。

    Args:
        element: 元素符号(如 'Pt');structure: fcc/bcc/hcp;miller: 晶面(见 _MILLERS)。
        layers: 原子层数(≥1);nx/ny: 面内超胞;vacuum: 真空层 Å(z 居中)。
        a/c: 晶格常数 Å;缺省查 LATTICE_GUESS(查不到 → ValueError,不编造);
            hcp 缺 c 且初猜表无该元素 → 按理想轴比 √(8/3)·a 推算并附 warning。
        fix_bottom: 冻结最底 n 层(Selective dynamics;0=不冻结;n≥layers → ValueError)。

    Returns:
        poscar: VASP5 POSCAR 文本;recipe: 可再生配方(含全部构造参数,写 job.yaml 用);
        warnings: 中文提醒(晶格常数口径/轴比推算/非正交胞说明)。
    """
    element = str(element or '').strip()
    structure = str(structure or '').strip().lower()
    miller = str(miller or '').strip()
    layers = int(layers)
    nx, ny = int(nx), int(ny)
    if not element or not element[0].isupper() or not element.isalpha() or len(element) > 2:
        raise ValueError(f'元素符号非法:{element!r}(应如 Pt / Fe / Ru)')
    if layers < 1:
        raise ValueError(f'层数须 ≥1,收到 {layers}')
    if nx < 1 or ny < 1:
        raise ValueError(f'超胞尺寸须为正整数,收到 nx={nx}, ny={ny}')
    if float(vacuum) < 5.0:
        raise ValueError(f'真空层须 ≥5 Å(收到 {vacuum!r}):过薄真空使周期镜像 slab 相互作用,'
                         '能量不可信;催化 slab 常用 ≥15 Å 并做真空收敛。')
    warnings: list[str] = []
    if float(vacuum) < 10.0:
        warnings.append(f'真空层 {float(vacuum):g} Å 偏薄(<10 Å):建议 ≥15 Å 并做真空收敛扫描。')
    guess = lattice_guess(element, structure)
    if a is None:
        if guess is None:
            raise ValueError(
                f'{element} 不在 {structure} 初猜表(LATTICE_GUESS),请显式给晶格常数 a'
                '(建议来自同泛函 EOS/晶胞优化);不猜测未知元素的晶格常数。')
        a = guess['a']
        warnings.append(f'晶格常数 a={a:g} Å 取自实验值初猜表:发表口径须用同泛函 '
                        'EOS/晶胞优化重新确定后显式传入。')
    a = float(a)
    if structure == 'hcp' and c is None:
        if guess is not None and 'c' in guess:
            c = guess['c']
            warnings.append(f'c={c:g} Å 取自实验值初猜表:发表口径须用同泛函晶胞优化确定。')
        else:
            c = _IDEAL_C_OVER_A * a
            warnings.append(f'未给 c,按理想轴比 c/a=√(8/3) 推算 c={c:.4f} Å:'
                            '真实 hcp 金属轴比偏离理想值,发表口径须晶胞优化确定。')
    a1, a2, basis2d, h, stack_offsets = _surface_mesh(structure, miller, a, c)

    coords: list = []
    z0 = float(vacuum) / 2.0                          # z 居中(同 sac_builder 口径)
    for k in range(layers):
        off = stack_offsets[k % len(stack_offsets)]
        for b in basis2d:
            for i in range(nx):
                for j in range(ny):
                    xy = b + off + i * a1 + j * a2
                    coords.append([xy[0], xy[1], z0 + k * h])
    span = (layers - 1) * h
    cell = np.array([[nx * a1[0], nx * a1[1], 0.0],
                     [ny * a2[0], ny * a2[1], 0.0],
                     [0.0, 0.0, span + float(vacuum)]], dtype=float)
    coords = np.array(coords, dtype=float)
    # 面内折回原胞([0,1) 分数区间;z 不动,保持真空居中)——坐标整洁,便于人眼核查
    frac = cart_to_frac(coords, cell)
    frac[:, :2] -= np.floor(frac[:, :2])
    coords = frac_to_cart(frac, cell)
    natoms = len(coords)
    text = write_poscar(
        f'{element} {structure}({miller}) slab {layers}L {nx}x{ny} (a={a:g} A'
        + (f', c={float(c):g} A' if structure == 'hcp' else '') + f', vac={float(vacuum):g} A)',
        cell, [element] * natoms, np.array(coords), mode='Cartesian')

    # ── 构造后自检(防"看着成功"的错构型;失败=内部错误,直接抛) ──
    got_layers = count_layers(text)
    if got_layers != layers:
        raise ValueError(f'内部自检失败:构造层数 {got_layers} ≠ 请求 {layers},请报 bug')
    got_vac = vacuum_thickness(text)
    if abs(got_vac - float(vacuum)) > 1e-6:
        raise ValueError(f'内部自检失败:真空层 {got_vac:.4f} ≠ 请求 {float(vacuum):g} Å,请报 bug')
    nn_theory = _theoretical_nn(structure, a, c)
    if natoms >= 2:
        nn = min_interatomic_distance(text)
        if nn < 0.8 * nn_theory:
            raise ValueError(f'内部自检失败:最近邻 {nn:.3f} Å 塌缩(理论 {nn_theory:.3f} Å),请报 bug')

    if fix_bottom:
        text = fix_bottom_layers(text, int(fix_bottom))   # 非法层数由其抛中文 ValueError

    if orthogonal_note and structure in ('fcc', 'hcp') and miller in ('111', '0001'):
        warnings.append('六方表面胞(面内基矢 60°,非正交):VASP 正常支持;'
                        'k 网格按面内两方向等密度取。')
    recipe = {'kind': 'metal_slab', 'element': element, 'structure': structure,
              'miller': miller, 'layers': layers, 'nx': nx, 'ny': ny,
              'vacuum': float(vacuum), 'a': a, 'fix_bottom': int(fix_bottom or 0)}
    if structure == 'hcp':
        recipe['c'] = float(c)
    desc = (f'{element} {structure}({miller}) slab:{layers} 层 × {nx}×{ny} 超胞,'
            f'a={a:g} Å' + (f'、c={float(c):g} Å' if structure == 'hcp' else '')
            + f',真空 {float(vacuum):g} Å,层距 {h:.4f} Å,{natoms} 原子'
            + (f',冻结底部 {int(fix_bottom)} 层' if fix_bottom else ''))
    return {'poscar': text, 'recipe': recipe, 'description': desc,
            'warnings': warnings, 'natoms': natoms}


def slab_builder_from_recipe(recipe) -> 'callable':
    """配方 → ``fn(n_layers) -> POSCAR 文本``(层厚收敛系列的再生器)。

    只改层数,其余参数(元素/晶面/超胞/真空/晶格常数/冻结底层)与配方一致——保证
    系列各作业"只改单一目标参数"(conv_scan 设计原则)。配方非 metal_slab / 缺字段 →
    ValueError 中文报错(如 SAC 石墨烯单层无层厚概念,见 api 层针对性提示)。
    """
    r = dict(recipe or {})
    if r.get('kind') != 'metal_slab':
        raise ValueError(f"配方 kind={r.get('kind')!r} 不是 metal_slab,无法再生不同层数 slab")
    required = ('element', 'structure', 'miller', 'nx', 'ny', 'vacuum', 'a')
    missing = [k for k in required if r.get(k) in (None, '')]
    if missing:
        raise ValueError(f'配方缺字段 {", ".join(missing)},无法再生 slab(配方损坏?)')

    def _fn(n_layers: int) -> str:
        return build_metal_slab(
            r['element'], r['structure'], r['miller'], int(n_layers),
            a=r['a'], c=r.get('c'), nx=r['nx'], ny=r['ny'], vacuum=r['vacuum'],
            fix_bottom=int(r.get('fix_bottom') or 0), orthogonal_note=False)['poscar']
    return _fn
