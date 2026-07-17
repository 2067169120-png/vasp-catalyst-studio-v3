"""参考态注册与稳定性判据(F4 后半 + F24)。

两块能力:
1. **参考态引擎**:reference_spec 产出参考体系的 POSCAR + 口径提示——
   bcc_li(体相 Li,μ_Li 定标)/ isolated_atom(孤立原子,破简并大盒 + 自旋)/
   molecule(接 molecules.py)。ReferenceCache 落项目级参考能缓存(references.yaml),
   register/lookup;lookup 传方法指纹 → 不一致给中文 warning(**ΔE 口径守卫**)。
2. **稳定性判据(F24)**:结合能 / 形成能 / σ=Eb/Ecoh 判稳(内嵌常见金属内聚能表)。

纯 python(仅几何写出借 sac_builder / molecules);中文注释,英文标识符。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from vcstudio.generate import molecules as mol_mod
from vcstudio.generate import sac_builder
from vcstudio.generate.incar_builder import MAGNETIC_ELEMENTS

REFERENCES_FILE = 'references.yaml'
BCC_LI_A = 3.44        # Å bcc Li 惯用胞晶格常数

# 常见金属体相内聚能(eV/atom),Kittel《固体物理导论》惯用值;供 σ=Eb/Ecoh 稳定性判据。
COHESIVE_ENERGY = {
    'Fe': 4.28, 'Co': 4.39, 'Ni': 4.44, 'Cu': 3.49, 'V': 5.31,
    'Ti': 4.85, 'Mn': 2.92, 'Mo': 6.82, 'W': 8.90,
}
_ECOH_SOURCE = 'Kittel 固体物理惯用内聚能值(eV/atom)'


# ── 参考态模板 ───────────────────────────────────────────────────────────────
def _bcc_li_poscar():
    cell = [[BCC_LI_A, 0.0, 0.0], [0.0, BCC_LI_A, 0.0], [0.0, 0.0, BCC_LI_A]]
    frac = [[0.0, 0.0, 0.0], [0.5, 0.5, 0.5]]        # bcc 2 原子惯用胞
    return sac_builder.write_poscar(
        f'bcc Li (a={BCC_LI_A} A) conventional 2-atom cell',
        cell, ['Li', 'Li'], frac, mode='Direct')


def _isolated_atom_poscar(element, box):
    # 非对称盒破简并(孤立原子/开壳态惯例),避免简并态收敛困难
    bx, by, bz = float(box), float(box) + 0.6, float(box) + 1.2
    cell = [[bx, 0.0, 0.0], [0.0, by, 0.0], [0.0, 0.0, bz]]
    coords = [[bx / 2.0, by / 2.0, bz / 2.0]]
    return sac_builder.write_poscar(
        f'{element} isolated atom (asymmetric box, break degeneracy)',
        cell, [element], coords, mode='Cartesian')


def reference_spec(kind, *, element=None, name=None, box=15.0):
    """参考态规格 → dict(含 'poscar' 与口径 'note')。

    kind:
    - 'bcc_li':体相 Li 2 原子惯用胞(a=3.44 Å),建议密 k 静算。
    - 'isolated_atom'(需 element):非对称大盒单原子 + 自旋提示。
    - 'molecule'(需 name):接 molecules.molecule_in_box。
    """
    if kind == 'bcc_li':
        return {
            'kind': 'bcc_li', 'poscar': _bcc_li_poscar(),
            'note': (f'bcc Li 2 原子惯用胞(a={BCC_LI_A} Å);μ_Li 用体相 Li,'
                     '建议密 k 网格静态计算(如 ≥12×12×12,ISMEAR=1 金属展宽),'
                     '能量口径须与其它参考态一致。')}
    if kind == 'isolated_atom':
        if not element:
            raise ValueError("kind='isolated_atom' 需指定 element")
        spin = (f'ISPIN=2,MAGMOM 按元素初猜({element} 磁性)'
                if element in MAGNETIC_ELEMENTS
                else '若元素基态不确定,建议 ISPIN=2 自旋极化以取正确原子基态')
        return {
            'kind': 'isolated_atom', 'element': element,
            'poscar': _isolated_atom_poscar(element, box), 'spin_hint': spin,
            'note': (f'{element} 孤立原子(非对称大盒破简并,Γ 点);自旋极化基态能量作参考,'
                     '口径须与体相/分子一致。')}
    if kind == 'molecule':
        if not name:
            raise ValueError("kind='molecule' 需指定 name")
        info = mol_mod.molecule_info(name)
        return {
            'kind': 'molecule', 'name': name,
            'poscar': mol_mod.molecule_in_box(name, box=box),
            'formula': info['formula'], 'spin_hint': info['spin_hint'],
            'note': '分子参考(Γ 点,大盒);' + info['source_note']}
    raise ValueError(f'未知参考态类型 {kind!r};可选 bcc_li / isolated_atom / molecule')


# ── 项目级参考能缓存(references.yaml) ────────────────────────────────────────
class ReferenceCache:
    """项目级参考能缓存:name → {'energy','job_dir','fingerprint_hash'}。

    register/lookup;lookup 可传 fingerprint_hash,与登记指纹不一致 → 中文 warning
    "参考能与当前口径指纹不一致,ΔE 不可比"(口径守卫,防跨口径混算)。原子写。
    """

    def __init__(self, project_root):
        self.root = Path(project_root)
        self.path = self.root / REFERENCES_FILE

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.yaml.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.path)

    def register(self, name, *, energy, job_dir=None, fingerprint_hash=None):
        """登记/覆盖一条参考能。返回登记条目。"""
        data = self._load()
        data[name] = {'energy': float(energy),
                      'job_dir': (str(job_dir) if job_dir is not None else None),
                      'fingerprint_hash': fingerprint_hash}
        self._save(data)
        return data[name]

    def lookup(self, name, *, fingerprint_hash=None):
        """查参考能 → 条目 dict(含 'warnings' 列表)或 None(未登记)。

        传入 fingerprint_hash 且与登记指纹不一致 → warnings 追加口径守卫中文告警。
        """
        entry = self._load().get(name)
        if entry is None:
            return None
        out = dict(entry)
        out['warnings'] = []
        stored = out.get('fingerprint_hash')
        if fingerprint_hash is not None and stored is not None and stored != fingerprint_hash:
            out['warnings'].append(
                f'参考能 {name} 的方法指纹({stored})与当前口径({fingerprint_hash})不一致,'
                f'ΔE 不可比;请以同口径重算该参考态。')
        return out

    def all(self) -> dict:
        return self._load()


def reference_cache(project_root):
    """构造项目级参考能缓存对象。"""
    return ReferenceCache(project_root)


# ── 稳定性判据(F24) ─────────────────────────────────────────────────────────
def binding_energy(e_sac, e_substrate, e_atom):
    """金属原子结合能 Eb = E(SAC) − E(缺陷基底) − E(孤立金属原子)。

    约定:负值 = 放热结合(越负越稳)。e_substrate 为含空位、未嵌金属的基底能量。
    """
    return e_sac - e_substrate - e_atom


def formation_energy(e_sac, e_ref_graphene, chem_pots, counts):
    """形成能 Ef = E(SAC) − E(参考石墨烯) − Σ counts[s]·μ[s]。

    counts:各物种带符号原子数变化(+ 增 / − 减,如掺 N 为 +、挖 C 为 −);
    chem_pots:各物种化学势 μ(eV/atom)。counts 中物种缺 μ → ValueError(显式,不猜)。
    """
    total = e_sac - e_ref_graphene
    for sp, n in counts.items():
        if sp not in chem_pots:
            raise ValueError(f'缺物种 {sp!r} 的化学势 μ,无法算形成能')
        total -= n * chem_pots[sp]
    return total


def cohesive_energy(metal):
    """查内置金属体相内聚能(eV/atom);未登记 → ValueError。"""
    if metal not in COHESIVE_ENERGY:
        raise ValueError(f'内置内聚能表无 {metal!r};可选:{", ".join(sorted(COHESIVE_ENERGY))}')
    return COHESIVE_ENERGY[metal]


def stability_verdict(eb, ecoh):
    """σ = −Eb/Ecoh 稳定性判据 → {'sigma','stable','note'}。

    eb 为带符号结合能(负 = 结合);ecoh 为正的体相内聚能(eV/atom)。
    σ>1 → 金属-基底结合强于本体内聚,单原子抗团聚/抗溶解,判稳。
    """
    if ecoh <= 0:
        raise ValueError('内聚能 Ecoh 须为正(eV/atom)')
    sigma = -eb / ecoh
    stable = sigma > 1.0
    if stable:
        note = (f'σ={sigma:.3f}>1:金属-基底结合强于本体内聚,单原子抗团聚/抗溶解,'
                f'判稳(初判,需进一步核验)。')
    else:
        note = (f'σ={sigma:.3f}≤1:结合弱于本体内聚,倾向团聚/溶解,判不稳;'
                f'需换位点/配位或核验计算。')
    return {'sigma': round(sigma, 4), 'stable': stable, 'note': note}
