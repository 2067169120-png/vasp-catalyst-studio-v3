"""Li-S 放电路径自由能(移植原版 sac_energy 核心,纯函数零依赖)。

约定(与原版一致,报告/提示词里都显式写明):
- ΔG(i) = [E(slab+Xᵢ) + Σ precip·E(mol) − n_li·μ_Li] − E(slab+S8),参照 S8*=0
- μ_Li 估算:S8 + 16Li → 8Li2S 整反应,μ_Li = (E(Li2S) − E(S8)/8)/2(可显式覆盖)
- 这里的 E 是 DFT 电子能(E0),未含 ZPE/熵——报告中明示,不冒充真自由能
- U_L(CHE):U_L = −max(ΔG_step/Δn_e),PDS = 逐电子上坡最陡的步。
  注:μ_Li 由 S8/Li2S 整反应定标,故 U_L 是**相对 S8/Li2S 整反应平衡电位**的极限
  电位(整反应参照系),**非相对 Li/Li⁺ 金属电极**;报告方法学节同款措辞。

能量来源:本地作业目录 OSZICAR 末行 E0(read_e0)/ 旧结果目录批量扫描
(load_molecule_energies:mol_* 与 molecule_* 两种命名都认)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import re

from vcstudio.project import reactions

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

    构型名需含物种名(如 ads_Li2S4_on_slab / Li2S6_top):按物种子串匹配唯一构型;
    多个匹配取 E 最低(最稳构型,常规口径)。
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


# ─────────────────────────────────────────────────────────────────────────────
# 通用 CHE/ΔG 台阶引擎(F16):物种链 + 每步电子数 + 参比电对 → 自由能路径 + 电化学量
#
# 核心公式(累计口径,首步归零;e=1,电位单位 V、能量单位 eV):
#   G_i(U) = E(species_i) + Σ_k coef_k·E(mol_k) + s·n_i·(μ_e0 − U) − G_ref
# 其中
#   μ_e0 = 电子耦合化学势(U=0):Li/Li⁺ 用 μ_Li;RHE 用 μ(H⁺+e⁻)=½G(H2)。
#   s    = 电子项符号:还原类 −1(离子+电子作反应物随电子进入)、氧化类 +1(作产物离开)。
#   → 展开得 G_i(U)=G_i(0) − s·n_i·U;还原类 G_i(U)=G_i(0)+n_i·U、氧化类 G_i(U)=G_i(0)−n_i·U。
# 极限电位/过电位(逐电子口径,化学步 Δn=0 不计入):
#   per_electron_i = ΔG_step_i / Δn_i;
#   还原类 U_L=−max_i(per_e_i)、η=U_eq−U_L;氧化类 U_L=max_i(per_e_i)、η=U_L−U_eq。
#   U_eq = ∓ΔG_total/(n_total·e)(还原 −、氧化 +),两向对同一电对给出同号平衡电位。
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_mu_e(spec, energies, mu_e):
    """确定电子耦合化学势 μ_e0(U=0)。缺必要能量 → 中文 ValueError 点名。"""
    if mu_e is not None:
        return float(mu_e)
    electrode = spec['electrode']
    if electrode == 'Li/Li+':
        # 复用现有 μ_Li 口径(需 energies 里含分子 Li2S/S8;否则请显式传 mu_e)。
        return mu_li_from_molecules(energies)
    # RHE:μ(H⁺+e⁻)=½G(H2)−eU,U=0 → ½E(H2)。
    if 'H2' not in energies:
        raise ValueError('RHE 口径需要 H2 能量 energies["H2"](或显式传入 mu_e=½G(H2))')
    return 0.5 * float(energies['H2'])


def free_energy_path(spec, energies: dict, *, mu_e: float | None = None,
                     g_corr: dict | None = None, u: float = 0.0) -> dict:
    """通用自由能台阶引擎:reactions 反应描述 + 能量字典 → 台阶 + 电化学量。

    spec:      reactions.ReactionSpec(或等价 dict);进入前做 validate_spec 防御性校验。
    energies:  {物种/分子名: 能量 eV}。键含 spec 各步 species(约定吸附态 X*)与
               coadsorbates_or_gas 的分子名(裸式,如 Li2S/H2O/O2);两者须为不同的键
               (故吸附态用 '*' 后缀,与同名分子区分,如 'Li2S*' vs 'Li2S')。
    mu_e:      电子耦合化学势 μ_e0(U=0);None 时按参比电对推断(Li/Li⁺→mu_li_from_molecules,
               RHE→½E(H2))。
    g_corr:    {物种/分子: ZPE−TS 校正 eV};提供则逐键叠加到能量(投稿级含振动热校正 ΔG),
               返回 thermo_corrected=True。不提供保持纯电子能口径。
    u:         展示台阶所用外加电位(V),默认 0;u_l/u_eq/pds 为内禀量与 u 无关。

    返回 {'steps':[{'label','G'}], 'pds_index', 'per_electron', 'u_l', 'u_eq', 'eta',
          'mu_e', 'electrode', 'direction', 'thermo_corrected', 'u', 'warnings':[...]}。
    注:pds_index 为**逐电子**决速步序号(化学步 Δn=0 被排除);电子数不等步(如 Li-S 末步
    8 e⁻)必须按 ΔG/Δn 判定,画图时把该 pds_index 透传 charts.ladder_data。
    """
    reactions.validate_spec(spec)  # 非法描述直接中文报错,绝不静默算错
    direction = spec.get('direction', 'reduction')
    s = -1.0 if direction == 'reduction' else 1.0  # 电子项符号:还原 −1、氧化 +1
    g_corr = dict(g_corr or {})
    steps_spec = spec['steps']
    mu0 = _resolve_mu_e(spec, energies, mu_e)

    def e_of(key, kind):
        if key not in energies:
            raise ValueError(f'缺{kind}能量:{key}(spec 需要,请在 energies 中提供)')
        return float(energies[key]) + float(g_corr.get(key, 0.0))

    def build(u_val):
        raw = []
        for st in steps_spec:
            n_i = float(st['n_electrons_cumulative'])
            g = e_of(st['species'], '吸附态/物种')
            for gg in st.get('coadsorbates_or_gas') or []:
                g += float(gg.get('coef', 1.0)) * e_of(gg['name'], '参考态/随步分子')
            g += s * n_i * (mu0 - u_val)
            raw.append(g)
        ref = raw[0]
        return [x - ref for x in raw]

    g0 = build(0.0)                       # 内禀 U=0 图(定 pds/u_l/u_eq)
    gu = g0 if not u else build(float(u))  # 展示图(默认与 U=0 同)
    ns = [float(st['n_electrons_cumulative']) for st in steps_spec]

    warnings: list = []
    per_e: list = []          # 逐步逐电子 ΔG(U=0);化学步为 None
    elec = []                 # (相邻步序号, per_electron) 仅电化学步
    for i in range(len(steps_spec) - 1):
        dn = ns[i + 1] - ns[i]
        dg = g0[i + 1] - g0[i]
        if dn > 1e-9:
            pe = dg / dn
            per_e.append(pe)
            elec.append((i, pe))
        else:
            per_e.append(None)
            warnings.append(
                f'第 {i + 1} 步(→{steps_spec[i + 1]["label"]})为化学步(Δn=0),不计入极限电位')
    if not elec:
        raise ValueError(f'预设 {spec["name"]}:无电化学步(全部 Δn=0),无法定义极限电位')

    worst_i, worst_pe = max(elec, key=lambda t: t[1])  # 逐电子最不利步
    pds_index = worst_i
    u_l = -worst_pe if direction == 'reduction' else worst_pe

    n_total = ns[-1] - ns[0]
    dG_total = g0[-1] - g0[0]
    if direction == 'reduction':
        u_eq = -dG_total / n_total
        eta = u_eq - u_l
    else:
        u_eq = dG_total / n_total
        eta = u_l - u_eq

    warnings.extend(_u_eq_warnings(round(u_eq, 4), spec))
    return {
        'steps': [{'label': st['label'], 'G': round(gu[i], 6)}
                  for i, st in enumerate(steps_spec)],
        'pds_index': pds_index,
        'per_electron': [None if v is None else round(v, 4) + 0.0 for v in per_e],
        'u_l': round(u_l, 4) + 0.0,   # +0.0 归一化负零(−0.0 → 0.0)
        'u_eq': round(u_eq, 4) + 0.0,
        'eta': round(eta, 4) + 0.0,
        'mu_e': round(mu0, 6),
        'electrode': spec['electrode'],
        'direction': direction,
        'thermo_corrected': bool(g_corr),
        'u': float(u),
        'warnings': warnings,
    }


def ladder_at_potentials(path_result: dict, spec: dict, potentials: list) -> dict:
    """多电位台阶数据 {U: [{'label','G'}]}(用于 U=0/U_eq/U_L 三线叠加)。

    ΔG_i(U)=ΔG_i(U0)+sign·(n_i−n_0)·(U−U0),sign 按电极类型(还原 +、氧化 −),
    U0 取 path_result['u'](通常 0)。返回 dict 每项 [{'label','G'}] 可直接映射为
    native_charts.free_energy_ladder 多体系入参:[{'name':f'U={U} V','G':[G_i,...]}]。
    """
    direction = spec.get('direction', 'reduction')
    sign = 1.0 if direction == 'reduction' else -1.0
    steps_spec = spec['steps']
    ns = [float(st['n_electrons_cumulative']) for st in steps_spec]
    n0 = ns[0]
    base = [st['G'] for st in path_result['steps']]
    u0 = float(path_result.get('u', 0.0) or 0.0)
    out = {}
    for U in potentials:
        out[U] = [{'label': steps_spec[i]['label'],
                   'G': round(base[i] + sign * (ns[i] - n0) * (float(U) - u0), 6)}
                  for i in range(len(steps_spec))]
    return out


def _u_eq_warnings(u_eq: float, spec: dict) -> list:
    """expected_u_eq 区间对照 → 科学准确的中文告警(超区间才返回,否则空)。"""
    exp = spec.get('expected_u_eq')
    if not exp:
        return []
    lo, hi = (min(float(exp[0]), float(exp[1])), max(float(exp[0]), float(exp[1])))
    if lo - 1e-9 <= u_eq <= hi + 1e-9:
        return []
    return [
        f'U_eq={u_eq:.2f} V 偏离实验区间[{lo:g}, {hi:g}],请检查参考能/溶剂化口径。'
        f'说明:纯 DFT 电子能口径(未含 ZPE/熵/溶剂化,且参比电对能量存在系统误差)与实验'
        f'自由能之间存在已知的系统性偏移(例:张洪毅体系计算 ~1.66 V vs 实验放电平台 2.15–2.24 V),'
        f'此类偏差属电子能 vs 实验的口径差,通常并不代表计算出错;'
        f'若要与实验直接对标,请叠加热校正(thermo.py)与溶剂化修正并复核参考态能量。'
    ]


def u_eq_check(path_result: dict, spec: dict) -> dict:
    """算出的 U_eq 与 spec.expected_u_eq 区间对照。

    返回 {'u_eq', 'expected':(lo,hi)|None, 'within':bool|None, 'warnings':[...]}。
    expected_u_eq 为 None(无实验值)→ within=None、warnings=[]。超区间 → 科学准确告警。
    """
    u_eq = path_result['u_eq']
    exp = spec.get('expected_u_eq')
    if not exp:
        return {'u_eq': u_eq, 'expected': None, 'within': None, 'warnings': []}
    lo, hi = (min(float(exp[0]), float(exp[1])), max(float(exp[0]), float(exp[1])))
    within = lo - 1e-9 <= u_eq <= hi + 1e-9
    return {'u_eq': u_eq, 'expected': (lo, hi), 'within': within,
            'warnings': [] if within else _u_eq_warnings(u_eq, spec)}
