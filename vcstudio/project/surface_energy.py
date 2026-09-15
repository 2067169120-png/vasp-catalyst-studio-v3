"""表面能 γ(纯函数零依赖):slab 总能 + bulk 每原子能 + 表面积 → J/m²。

对称 slab 惯例(两面等价、上下都弛豫):

    γ = (E_slab − N_slab · E_bulk/atom) / (2 A)

分母 2A 因一块 slab 有上下两个表面(周期真空隔开)。单位 eV/Å²,乘 16.0218 换 J/m²
(1 eV/Å² = 1.602176634e-19 J / 1e-20 m² = 16.02176634 J/m²)。

口径边界(绝不静默):
- ``relaxed_both_sides=False``(非对称:一面固定成体相、只弛豫另一面,或吸附/掺杂只在一面):
  2A 口径不再严格成立,应改用「固定侧当体相参考」的非对称公式或双点法外推;本函数仍按
  2A 给数但附 warning,提醒用户核对口径,勿直接发表。
- E_bulk/atom 必须与 slab **同泛函/同 ENCUT/同赝势** 且取自收敛的体相单点(每原子能),
  否则大数相减(E_slab ~ 数十上百 eV)被基组不一致污染 → γ 系统性偏差,同样 warning 提示。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import math

from vcstudio.generate.poscar import read_cell_vectors

EV_A2_TO_J_M2 = 16.02176634        # 1 eV/Å² → J/m²


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def area_from_poscar(poscar_text: str) -> float:
    """POSCAR 表面积(Å²)= |a₁ × a₂|(前两个晶格矢量叉积模,slab 惯例真空沿 c)。

    复用 read_cell_vectors(含负值目标体积与三个分量缩放因子)。斜胞也正确
    (叉积模 = 平行四边形面积,不等于 |a₁|·|a₂|)。
    """
    cell = read_cell_vectors(poscar_text)
    cr = _cross(cell[0], cell[1])
    return math.sqrt(sum(x * x for x in cr))


def surface_energy(e_slab: float, n_slab: int, e_bulk_per_atom: float,
                   area_a2: float, *, relaxed_both_sides: bool = True) -> dict:
    """对称 slab 表面能 → ``{'gamma_evA2','gamma_jm2','note','warnings'}``。

    γ = (E_slab − N_slab·E_bulk/atom) / (2A)。单位 eV/Å² 与 J/m²(×16.0218)各给一份。

    Args:
        e_slab: slab 总能(eV)。
        n_slab: slab 原子数(N_slab)。
        e_bulk_per_atom: 体相每原子能(eV/atom;须与 slab 同泛函/ENCUT/赝势)。
        area_a2: 单面表面积 A(Å²,见 area_from_poscar)。
        relaxed_both_sides: True=上下两面等价且都弛豫(标准 2A 口径);
            False=非对称弛豫,2A 口径存疑 → 附 warning。

    Returns:
        dict:gamma_evA2 / gamma_jm2 / note(中文口径说明) / warnings(列表)。
    面积 ≤0 或原子数 ≤0 → ValueError(绝不给无意义的 γ)。
    """
    if area_a2 <= 0:
        raise ValueError(f'表面积须为正(Å²),收到 {area_a2!r}')
    if n_slab <= 0:
        raise ValueError(f'slab 原子数须为正整数,收到 {n_slab!r}')

    warnings: list[str] = []
    excess = float(e_slab) - int(n_slab) * float(e_bulk_per_atom)   # 过剩能(eV,两面之和)
    gamma_evA2 = excess / (2.0 * float(area_a2))
    gamma_jm2 = gamma_evA2 * EV_A2_TO_J_M2

    if not relaxed_both_sides:
        warnings.append(
            '非对称弛豫(relaxed_both_sides=False):slab 上下两面不等价,标准 2A 口径不严格'
            '成立;应改用「固定侧作体相参考」的非对称公式或增厚外推,当前 γ 仅供参考,勿直接发表。')
    if gamma_evA2 < 0:
        warnings.append(
            f'算得 γ={gamma_evA2:.4f} eV/Å² 为负:通常意味着 E_bulk/atom 与 slab 口径不一致'
            '(不同泛函/ENCUT/赝势)或体相能取错,请核对参考体相单点。')

    note = ('γ=(E_slab−N·E_bulk/atom)/(2A)(对称 slab,2A=上下两面);'
            f'{gamma_evA2:.4f} eV/Å² = {gamma_jm2:.3f} J/m²。'
            'E_bulk/atom 须与 slab 同泛函/ENCUT/赝势,取自收敛体相单点。')
    return {'gamma_evA2': gamma_evA2, 'gamma_jm2': gamma_jm2,
            'note': note, 'warnings': warnings}
