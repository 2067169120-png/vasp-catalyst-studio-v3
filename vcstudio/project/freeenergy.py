"""Li-S 放电路径自由能(移植原版 sac_energy 核心,纯函数零依赖)。

约定(与原版一致,报告/提示词里都显式写明):
- ΔG(i) = [E(slab+Xᵢ) + Σ precip·E(mol) − n_li·μ_Li] − E(slab+S8),参照 S8*=0
- μ_Li 估算:S8 + 16Li → 8Li2S 整反应,μ_Li = (E(Li2S) − E(S8)/8)/2(可显式覆盖)
- 这里的 E 是 DFT 电子能(E0),未含 ZPE/熵——报告中明示,不冒充真自由能
- U_L(CHE):U_L = −max(ΔG_step/Δn_e),PDS = 逐电子上坡最陡的步

能量来源:本地作业目录 OSZICAR 末行 E0(read_e0)/ 旧结果目录批量扫描
(load_molecule_energies:mol_* 与 molecule_* 两种命名都认)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re

_E0_RE = re.compile(r'E0=\s*([-+.\dEe]+)')

# Li-S 16 电子放电路径 preset:(吸附物种, 累计 n_li, 沉淀 [(分子, 个数)])
# 硫/锂守恒:每态 吸附态+沉淀 合计 8 个 S;n_li = 吸附态 Li + 沉淀 Li。
LIS_PRESET = [
    ('S8', 0, []),
    ('Li2S8', 2, []),
    ('Li2S6', 4, [('Li2S2', 1)]),
    ('Li2S4', 6, [('Li2S2', 2)]),
    ('Li2S2', 8, [('Li2S2', 3)]),
    ('Li2S', 16, [('Li2S', 7)]),
]


def read_e0(job_dir) -> float | None:
    """读本地作业目录 OSZICAR 的末个 E0(拉回结果后可用)。缺文件/无 E0 → None。"""
    p = os.path.join(str(job_dir), 'OSZICAR')
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    hits = _E0_RE.findall(text)
    try:
        return float(hits[-1]) if hits else None
    except ValueError:
        return None


def load_molecule_energies(folder) -> dict:
    """扫描目录下 mol_<X>/molecule_<X> 子目录 → {X: E0}(旧版 results/done 兼容)。"""
    out = {}
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in entries:
        low = name.lower()
        for prefix in ('mol_', 'molecule_'):
            if low.startswith(prefix):
                e = read_e0(os.path.join(str(folder), name))
                if e is not None:
                    out[name[len(prefix):]] = e
                break
    return out


def mu_li_from_molecules(mol_energies: dict) -> float:
    """μ_Li = (E(Li2S) − E(S8)/8)/2。缺 Li2S/S8 → ValueError(显式,不猜)。"""
    try:
        e_li2s, e_s8 = mol_energies['Li2S'], mol_energies['S8']
    except KeyError as e:
        raise ValueError(f'μ_Li 估算需要分子能量 {e},请提供 mol_Li2S 与 mol_S8(或显式给 mu_li)')
    return (e_li2s - e_s8 / 8.0) / 2.0


def discharge_path(system_energies: dict, mol_energies: dict, *,
                   mu_li: float | None = None, preset=None,
                   g_corr: dict | None = None) -> dict:
    """放电路径 → charts.ladder 契约(steps+pds_index+u_l)+ 电化学量。

    system_energies: {物种: E(slab+X)};preset 各态物种必须齐(缺 → ValueError 点名)。
    g_corr(可选): {物种: ZPE−TS 校正 eV}(project.thermo.load_corrections 产物);
    提供则逐态叠加到 E(slab+X),结果为含振动热校正的 ΔG(投稿级);
    不提供保持纯电子能口径(报告须明示)。返回带 'thermo_corrected' 标志。
    返回 {'steps':[{label,G,sub_label}], 'pds_index', 'u_l', 'mu_li',
          'per_electron':[...], 'thermo_corrected': bool}
    注意:pds_index 是**逐电子** ΔG 口径(与 u_l 同源);画阶梯图时必须把它
    传给 charts.ladder_data(steps, u_l, pds_index=…)——各步电子数不等
    (末步 8 e⁻),让 charts 按原始 ΔG 重算会高亮错决速步。
    """
    preset = preset or LIS_PRESET
    g_corr = dict(g_corr or {})
    if mu_li is None:
        mu_li = mu_li_from_molecules(mol_energies)
    missing = [sp for sp, _, _ in preset if sp not in system_energies]
    if missing:
        raise ValueError(f'缺吸附态能量:{", ".join(missing)}(需 slab+X 的 DONE 能量)')
    for _, _, precip in preset:
        for mol, _ in precip:
            if mol not in mol_energies:
                raise ValueError(f'缺沉淀分子能量:{mol}')
    ref_sp = preset[0][0]
    e_ref = system_energies[ref_sp] + g_corr.get(ref_sp, 0.0)
    steps, nlis = [], []
    for sp, n_li, precip in preset:
        g = (system_energies[sp] + g_corr.get(sp, 0.0)
             + sum(cnt * mol_energies[mol] for mol, cnt in precip)
             - n_li * mu_li) - e_ref
        sub = ' + '.join(f'{cnt}×{mol}' for mol, cnt in precip)
        steps.append({'label': f'{sp}*', 'G': round(g, 6), 'sub_label': sub})
        nlis.append(n_li)
    # 逐电子(=逐 Li)ΔG:决速步 = 最陡上坡;U_L = −max(每电子 ΔG)
    per_e = []
    for i in range(len(steps) - 1):
        dn = nlis[i + 1] - nlis[i]
        dg = steps[i + 1]['G'] - steps[i]['G']
        per_e.append(dg / dn if dn else float('inf'))
    pds = max(range(len(per_e)), key=lambda i: per_e[i]) if per_e else None
    u_l = -max(per_e) if per_e else None
    return {'steps': steps, 'pds_index': pds,
            'u_l': (round(u_l, 4) if u_l is not None else None),
            'mu_li': round(mu_li, 6), 'per_electron': [round(x, 4) for x in per_e],
            'thermo_corrected': bool(g_corr)}


def path_from_project_and_molecules(delta_rows: list, e_slab: float,
                                    molecules_dir, *, mu_li=None,
                                    g_corr: dict | None = None) -> dict:
    """便捷入口:项目 ΔE 行(带 e_config)+ 旧分子目录 → 放电路径。

    组态名需含物种名(如 ads_Li2S4_on_slab / Li2S6_top):按物种子串匹配唯一组态;
    多个匹配取 E 最低(最稳组态,常规口径)。
    g_corr 透传 discharge_path(逐物种 ZPE−TS 校正,见 project.thermo)。
    """
    mol_e = load_molecule_energies(molecules_dir)
    system_e = {}
    for sp, _, _ in LIS_PRESET:
        # 只取 DONE 成员(审查#3):NEEDS_HUMAN/BAD_ENERGY 的能量不得漏进 ΔG/U_L
        cands = [r['e_config'] for r in delta_rows
                 if r.get('e_config') is not None and r.get('state') == 'DONE'
                 and _species_in_name(sp, r.get('name', ''))]
        if cands:
            system_e[sp] = min(cands)
    return discharge_path(system_e, mol_e, mu_li=mu_li, g_corr=g_corr)


def _species_in_name(species: str, name: str) -> bool:
    """物种作为独立 token 匹配:前不能是字母/数字(防 S8 误配 Li2S8 后缀),
    后不能是数字(防 Li2S 误配 Li2S8 前缀)。命名用分隔符即可,如 ads_Li2S4_top。"""
    low, sp = name.lower(), species.lower()
    i = low.find(sp)
    while i != -1:
        prev = low[i - 1:i]
        nxt = low[i + len(sp):i + len(sp) + 1]
        if not nxt.isdigit() and not (prev.isalnum()):
            return True
        i = low.find(sp, i + 1)
    return False
