"""反应网络描述与预设库:把 Li-S 硬编码抽象为"物种链 + 每步电子数 + 参比电对"通用引擎。

设计动机(见项目调研《图表菜单调研》《火山图与多催化剂对比图-功能规格》):
Li-S 的自由能台阶图与 ORR/OER/HER/CO2RR 的 CHE(computational hydrogen electrode)台阶图
形状相同、只是参考电对不同(Li/Li⁺ 电对 vs RHE 氢标)。故抽象成一个通用引擎:
每个反应族 = 一组预设参数(中间体物种链 + 每步累计转移电子数 + 参比电对 + 随步进出的分子),
而非两套独立代码。数值计算在 freeenergy.free_energy_path 里完成;本模块只负责"描述 + 校验"。

数据模型 ReactionSpec(纯 dict 约定,不引入 class):
    {
      'name':        str,                 # 预设标识
      'description':  str,                 # 一句话说明
      'electrode':   'Li/Li+' | 'RHE',    # 参比电对
      'direction':   'reduction' | 'oxidation',  # 放电/还原类 vs 析氧类(决定 U_L/η 符号)
      'steps': [
        {
          'label':   str,                 # 台阶图显示名
          'species': str,                 # 吸附态能量字典键(约定吸附态记 X*,干净基底记 *)
          'n_electrons_cumulative': int,  # 到该态为止累计转移的电子数(单调非降)
          'coadsorbates_or_gas': [{'name': str, 'coef': float}],  # 随步进出的分子(沉淀/气相/参考态)
        }, ...
      ],
      'reference_note': str,              # 中文口径说明(参考态/½H2/water trick 等)
      'expected_u_eq':  (a, b) | None,    # 有实验值时的平衡电位区间,用于 u_eq_check 对照告警
      'formulas':      {species: 化学式}, # 可选:非显式化学式物种的注册式映射
    }

守恒记账约定:参比电对每转移 1 个电子,配套进出 1 个参考离子(Li⁺+e⁻→Li、H⁺+e⁻→H),
电荷由"离子-电子配对"隐式守恒,故逐步质量/电荷校验只需核对原子:
    组成(下一态) − 组成(本态) == s·Δn·{参考原子}
其中还原类 s=+1(离子作反应物随电子进入体系)、氧化类 s=−1(离子作产物随电子离开体系)。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import re

# 参比电对 → 每转移 1 电子随之进出的参考原子数(离子-电子配对)。
ELECTRODE_REF_ATOMS = {
    'Li/Li+': {'Li': 1.0},
    'RHE': {'H': 1.0},
}

# 逐字母切分化学式:多字母元素(Li/Na/Cl…)优先,数字可含小数(支持 ½O2 类分数计量的分子式极少,
# 但计量系数由 coef 承担,这里的下标一般是整数)。
_ELEMENT_RE = re.compile(r'([A-Z][a-z]?)(\d*\.?\d*)')

_COMP_EPS = 1e-6


def parse_formula(formula: str) -> dict:
    """化学式 → {元素: 计量数}。

    '*'(吸附位标记)与空白忽略;'*' 或空串 → 空组成(干净基底,不含吸附原子)。
    多字母元素正确切分;出现无法识别的字符 → 中文 ValueError 点名。
    例:'Li2S8'→{Li:2,S:8}、'OOH'→{O:2,H:1}、'H2O'→{H:2,O:1}、'*'→{}。
    """
    s = (formula or '').replace('*', '').replace(' ', '')
    if not s:
        return {}
    comp: dict = {}
    pos = 0
    for m in _ELEMENT_RE.finditer(s):
        if m.start() != pos:
            raise ValueError(f'无法解析化学式 {formula!r}(第 {pos} 位出现无法识别的字符)')
        el, num = m.group(1), m.group(2)
        comp[el] = comp.get(el, 0.0) + (float(num) if num else 1.0)
        pos = m.end()
    if pos != len(s):
        raise ValueError(f'无法解析化学式 {formula!r}(第 {pos} 位起无法识别)')
    return comp


def _add_comp(dst: dict, src: dict, scale: float = 1.0) -> None:
    """把 src 组成按 scale 倍累加进 dst(就地)。"""
    for el, cnt in src.items():
        dst[el] = dst.get(el, 0.0) + cnt * scale


def _species_formula(species: str, formulas: dict) -> dict:
    """物种 → 组成:优先取 spec.formulas 注册式,否则按物种名(去 '*')解析。"""
    if formulas and species in formulas:
        return parse_formula(formulas[species])
    return parse_formula(species)


def _state_comp(step: dict, formulas: dict) -> dict:
    """一个态的总组成 = 吸附物种 + 随步分子(coadsorbates_or_gas,按 coef 计)。"""
    comp: dict = {}
    _add_comp(comp, _species_formula(step['species'], formulas))
    for g in step.get('coadsorbates_or_gas') or []:
        _add_comp(comp, parse_formula(g['name']), float(g.get('coef', 1.0)))
    return comp


def _nonzero(comp: dict) -> dict:
    """过滤掉近零项并四舍五入,便于报错信息可读。"""
    return {k: round(v, 4) for k, v in comp.items() if abs(v) > 1e-9}


def validate_spec(spec: dict) -> bool:
    """校验反应描述:参比电对合法、电子数单调、逐步质量/电荷守恒、步数≥2。

    任何不满足 → 中文 ValueError,并点名是哪一步、组成差多少。校验通过返回 True。
    """
    name = spec.get('name', '<未命名>')
    electrode = spec.get('electrode')
    if electrode not in ELECTRODE_REF_ATOMS:
        raise ValueError(
            f'预设 {name}:未知参比电对 electrode={electrode!r}(应为 "Li/Li+" 或 "RHE")')
    direction = spec.get('direction', 'reduction')
    if direction not in ('reduction', 'oxidation'):
        raise ValueError(
            f'预设 {name}:未知反应方向 direction={direction!r}(应为 "reduction" 或 "oxidation")')
    steps = spec.get('steps') or []
    if len(steps) < 2:
        raise ValueError(f'预设 {name}:反应至少需要 2 个状态(当前 {len(steps)} 个)')

    formulas = spec.get('formulas') or {}
    ns = []
    for i, st in enumerate(steps):
        for key in ('label', 'species', 'n_electrons_cumulative'):
            if key not in st:
                raise ValueError(f'预设 {name} 第 {i + 1} 步缺字段 {key!r}')
        ns.append(float(st['n_electrons_cumulative']))

    # 电子数单调非降(允许 Δn=0 的化学步,如脱附)。
    for i in range(len(ns) - 1):
        if ns[i + 1] < ns[i] - 1e-9:
            raise ValueError(
                f'预设 {name} 第 {i + 2} 步累计电子数 {ns[i + 1]:g} < 前一步 {ns[i]:g},'
                f'累计电子数须单调非降')
    if ns[-1] - ns[0] <= 1e-9:
        raise ValueError(f'预设 {name}:全程无净电子转移(首尾累计电子数相同),不是电化学路径')

    # 逐步质量/电荷守恒。
    ref = ELECTRODE_REF_ATOMS[electrode]
    s_add = 1.0 if direction == 'reduction' else -1.0
    for i in range(len(steps) - 1):
        dn = ns[i + 1] - ns[i]
        delta = _state_comp(steps[i + 1], formulas)
        _add_comp(delta, _state_comp(steps[i], formulas), -1.0)
        expected = {el: s_add * dn * cnt for el, cnt in ref.items()}
        for el in set(delta) | set(expected):
            if abs(delta.get(el, 0.0) - expected.get(el, 0.0)) > _COMP_EPS:
                raise ValueError(
                    f'预设 {name} 第 {i + 1} 步(→{steps[i + 1].get("label")})质量/电荷不守恒:'
                    f'净组成变化 {_nonzero(delta)} ≠ {dn:g} 电子转移应带来的 {_nonzero(expected)}'
                    f'(参比电对 {electrode} 每电子进出 {ref})')
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 预设库
# ─────────────────────────────────────────────────────────────────────────────

# Li-S 16 电子放电全路径,对齐现 freeenergy.LIS_PRESET 口径(硫/锂守恒:各态吸附态+沉淀合计 8 S)。
LIS_16E = {
    'name': 'LIS_16E',
    'description': 'Li-S 16 电子放电全路径 S8→Li2S8→Li2S6→Li2S4→Li2S2→Li2S(硫链逐步锂化 + 多硫化物沉淀)',
    'electrode': 'Li/Li+',
    'direction': 'reduction',
    'reference_note': (
        'μ_Li 由 S8/Li2S 整反应定标(μ_Li=(E(Li2S)−E(S8)/8)/2,可显式覆盖);'
        'U_L 相对 S8/Li2S 整反应平衡电位,而非 Li 金属电极(Li/Li⁺)。'
        '吸附态记 X*,随步析出的 Li2S2/Li2S 记为凝聚相分子(coadsorbates_or_gas)。'),
    'expected_u_eq': (2.15, 2.24),  # 实验放电平台(vs Li/Li⁺),用于 u_eq_check 对照
    'steps': [
        {'label': 'S8', 'species': 'S8*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': []},
        {'label': 'Li2S8', 'species': 'Li2S8*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': []},
        {'label': 'Li2S6', 'species': 'Li2S6*', 'n_electrons_cumulative': 4,
         'coadsorbates_or_gas': [{'name': 'Li2S2', 'coef': 1}]},
        {'label': 'Li2S4', 'species': 'Li2S4*', 'n_electrons_cumulative': 6,
         'coadsorbates_or_gas': [{'name': 'Li2S2', 'coef': 2}]},
        {'label': 'Li2S2', 'species': 'Li2S2*', 'n_electrons_cumulative': 8,
         'coadsorbates_or_gas': [{'name': 'Li2S2', 'coef': 3}]},
        {'label': 'Li2S', 'species': 'Li2S*', 'n_electrons_cumulative': 16,
         'coadsorbates_or_gas': [{'name': 'Li2S', 'coef': 7}]},
    ],
}

# 张洪毅论文(SAC 对锂硫电池正极催化)Li2S3 末段还原的三条竞争途径:
# 总反应 Li2S3 + 2(Li⁺+e⁻) → Li2S2 + Li2S,分别经 *LiS 缔合 / *LiS2 缔合 / 解离(*LiS 中间体)。
# *LiS、*LiS2 为开壳自由基中间体(奇电子),这里只记原子组成,守恒自洽。
_LIS_ASSOC_NOTE = (
    '张洪毅论文 Li2S3 末段还原(2e):总反应 Li2S3 + 2(Li⁺+e⁻) → Li2S2 + Li2S。'
    '*LiS/*LiS2 为开壳自由基中间体。expected_u_eq 用实验放电平台 2.15–2.24 V;'
    '论文中最优催化剂 Ti@P1N3 计算 E0≈1.66 V 与实验的差属电子能口径系统差(见 u_eq_check)。')

# *LiS2 缔合途径:Li2S3* → *LiS2 → Li2S2*(析出 Li2S)。
LIS_ASSOC_LIS2 = {
    'name': 'LIS_ASSOC_LIS2',
    'description': 'Li2S3 还原 *LiS2 缔合途径:Li2S3*→LiS2*→Li2S2*(2e,析出 Li2S)',
    'electrode': 'Li/Li+',
    'direction': 'reduction',
    'reference_note': _LIS_ASSOC_NOTE,
    'expected_u_eq': (2.15, 2.24),
    'steps': [
        {'label': 'Li2S3', 'species': 'Li2S3*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': []},
        {'label': '*LiS2', 'species': 'LiS2*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': [{'name': 'Li2S', 'coef': 1}]},
        {'label': 'Li2S2', 'species': 'Li2S2*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'Li2S', 'coef': 1}]},
    ],
}

# *LiS 缔合途径:Li2S3* → *LiS → Li2S*(析出 Li2S2)。
LIS_ASSOC_LIS = {
    'name': 'LIS_ASSOC_LIS',
    'description': 'Li2S3 还原 *LiS 缔合途径:Li2S3*→LiS*→Li2S*(2e,析出 Li2S2)',
    'electrode': 'Li/Li+',
    'direction': 'reduction',
    'reference_note': _LIS_ASSOC_NOTE,
    'expected_u_eq': (2.15, 2.24),
    'steps': [
        {'label': 'Li2S3', 'species': 'Li2S3*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': []},
        {'label': '*LiS', 'species': 'LiS*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': [{'name': 'Li2S2', 'coef': 1}]},
        {'label': 'Li2S', 'species': 'Li2S*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'Li2S2', 'coef': 1}]},
    ],
}

# 解离途径:Li2S3* → Li2S2*(先解离出 *LiS 自由基) → Li2S2*(*LiS 再还原为 Li2S 析出)。
LIS_DISSOC = {
    'name': 'LIS_DISSOC',
    'description': 'Li2S3 还原解离途径:Li2S3*→Li2S2*+*LiS→Li2S2*(2e,末态析出 Li2S)',
    'electrode': 'Li/Li+',
    'direction': 'reduction',
    'reference_note': _LIS_ASSOC_NOTE,
    'expected_u_eq': (2.15, 2.24),
    'steps': [
        {'label': 'Li2S3', 'species': 'Li2S3*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': []},
        {'label': 'Li2S2+*LiS', 'species': 'Li2S2*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': [{'name': 'LiS', 'coef': 1}]},
        {'label': 'Li2S2', 'species': 'Li2S2*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'Li2S', 'coef': 1}]},
    ],
}

# 4 电子 ORR(RHE,还原类,缔合机理):O2→*OOH→*O→*OH→H2O。
# 参考态约定:μ(H⁺+e⁻)=½G(H2)−eU;O2 建议用 water trick G(O2)=2G(H2O)−2G(H2)+4×1.23 eV,
# 避免 PBE 对 O2 三重态描述不佳。逐步析出 H2O 记入 coadsorbates_or_gas。
_RHE_O_NOTE = (
    'RHE 氢标:每个质子耦合电子转移(PCET)步 μ(H⁺+e⁻)=½G(H2)−eU,pH 项在 RHE 标度下自动抵消。'
    'O2 建议用 water trick 参考:G(O2)=2G(H2O)−2G(H2)+4×1.23 eV(PBE 对 O2 三重态描述差);'
    'OH 参考由 G(H2O)−½G(H2) 隐含。干净基底记 *,吸附态记 X*。')

ORR_4E = {
    'name': 'ORR_4E',
    'description': '4 电子氧还原 ORR(缔合机理):O2→*OOH→*O→*OH→H2O',
    'electrode': 'RHE',
    'direction': 'reduction',
    'reference_note': _RHE_O_NOTE,
    'expected_u_eq': (1.20, 1.26),  # O2/H2O 可逆电位 1.23 V
    'steps': [
        {'label': 'O2', 'species': '*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': [{'name': 'O2', 'coef': 1}]},
        {'label': '*OOH', 'species': 'OOH*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': []},
        {'label': '*O', 'species': 'O*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 1}]},
        {'label': '*OH', 'species': 'OH*', 'n_electrons_cumulative': 3,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 1}]},
        {'label': 'H2O', 'species': '*', 'n_electrons_cumulative': 4,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 2}]},
    ],
}

# 4 电子 OER(RHE,析氧/氧化类):2H2O→*OH→*O→*OOH→O2。为 ORR 的逆向,U_L/η 取氧化口径。
OER_4E = {
    'name': 'OER_4E',
    'description': '4 电子析氧 OER:2H2O→*OH→*O→*OOH→O2(析氧口径,η=U_L−U_eq)',
    'electrode': 'RHE',
    'direction': 'oxidation',
    'reference_note': _RHE_O_NOTE,
    'expected_u_eq': (1.20, 1.26),
    'steps': [
        {'label': '2H2O', 'species': '*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 2}]},
        {'label': '*OH', 'species': 'OH*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 1}]},
        {'label': '*O', 'species': 'O*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 1}]},
        {'label': '*OOH', 'species': 'OOH*', 'n_electrons_cumulative': 3,
         'coadsorbates_or_gas': []},
        {'label': 'O2', 'species': '*', 'n_electrons_cumulative': 4,
         'coadsorbates_or_gas': [{'name': 'O2', 'coef': 1}]},
    ],
}

# HER(RHE,还原类):2(H⁺+e⁻)→ *H →(第二个 PCET)→ H2。以 2 电子建模使两步均为电化学步,
# 决速由 ΔG(*H) 决定,η=|ΔG(*H)|。
HER = {
    'name': 'HER',
    'description': '析氢 HER:2(H⁺+e⁻)→*H→H2(经单氢中间体,η=|ΔG_H*|)',
    'electrode': 'RHE',
    'direction': 'reduction',
    'reference_note': (
        'RHE 氢标:μ(H⁺+e⁻)=½G(H2)−eU;初态为干净基底 + 2 个溶液 H⁺,末态析出 1 个 H2。'
        'U_eq=0 V,过电位 η=|ΔG(*H)|/e 由氢吸附自由能决定(Sabatier 顶点 ΔG(*H)≈0)。'),
    'expected_u_eq': (-0.03, 0.03),  # H⁺/H2 可逆电位 0 V
    'steps': [
        {'label': '2H⁺+2e⁻', 'species': '*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': []},
        {'label': '*H', 'species': 'H*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': []},
        {'label': 'H2', 'species': '*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'H2', 'coef': 1}]},
    ],
}

# CO2RR→CO(RHE,还原类):CO2→*COOH→*CO→CO(g)。末步 *CO 脱附为化学步(Δn=0),
# 不计入极限电位(freeenergy 会给出提示)。
CO2RR_TO_CO = {
    'name': 'CO2RR_TO_CO',
    'description': 'CO2 还原制 CO:CO2→*COOH→*CO→CO(末步脱附为化学步,不计极限电位)',
    'electrode': 'RHE',
    'direction': 'reduction',
    'reference_note': (
        'RHE 氢标:μ(H⁺+e⁻)=½G(H2)−eU。CO2 + 2(H⁺+e⁻) → CO + H2O,经 *COOH、*CO 两个 PCET;'
        '末步 *CO→CO(g) 为纯脱附(Δn=0),不随电位移动,故不计入极限电位。'),
    'expected_u_eq': None,  # 触发 u_eq_check 的"无实验区间"分支
    'steps': [
        {'label': 'CO2', 'species': '*', 'n_electrons_cumulative': 0,
         'coadsorbates_or_gas': [{'name': 'CO2', 'coef': 1}]},
        {'label': '*COOH', 'species': 'COOH*', 'n_electrons_cumulative': 1,
         'coadsorbates_or_gas': []},
        {'label': '*CO', 'species': 'CO*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'H2O', 'coef': 1}]},
        {'label': 'CO', 'species': '*', 'n_electrons_cumulative': 2,
         'coadsorbates_or_gas': [{'name': 'CO', 'coef': 1}, {'name': 'H2O', 'coef': 1}]},
    ],
}

_PRESETS = {
    p['name']: p for p in (
        LIS_16E, LIS_ASSOC_LIS, LIS_ASSOC_LIS2, LIS_DISSOC,
        ORR_4E, OER_4E, HER, CO2RR_TO_CO,
    )
}


def list_presets() -> dict:
    """返回全部内置预设 {name: spec}(浅拷贝顶层字典,防止外部误改注册表)。"""
    return dict(_PRESETS)


def get_preset(name: str) -> dict:
    """按名取预设;未知名 → 中文 ValueError 点名可选项。"""
    try:
        return _PRESETS[name]
    except KeyError:
        raise ValueError(
            f'未知预设 {name!r},可选:{", ".join(sorted(_PRESETS))}') from None
