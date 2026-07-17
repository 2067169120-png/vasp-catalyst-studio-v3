"""频率 → ZPE/熵热校正(纯函数零依赖):把自由能从"电子能近似"升到投稿级。

数据源:VASP 频率计算(IBRION=5/6)的 OUTCAR。解析行形如:
   1 f  =   98.309520 THz   617.699596 2PI*THz 3279.156295 cm-1   406.564208 meV
   4 f/i=    0.541996 THz     3.405475 2PI*THz   18.079172 cm-1     2.241701 meV
(f = 实频,f/i = 虚频)。

物理口径(吸附质谐振子近似,催化文献标准做法;x = hν/k_B T,只算实频):
- ZPE      = Σ hν/2
- U_vib(T) = Σ hν/(e^x−1)(振动内能热占据项,ASE HarmonicThermo 含此项)
- S_vib(谐振子): S = k_B Σ [ x/(e^x−1) − ln(1−e^{−x}) ]
- G 校正给两种口径(VibResult.g_corr(mode),二者都是文献里的正规约定):
  * 'zpe_ts'(默认): G_corr = ZPE − T·S_vib —— 吸附质常用简化口径,忽略
    U_vib 热占据项;与本组已发表论文一致,是复现基准,默认值不得更改。
  * 'ase':          G_corr = ZPE + U_vib(T) − T·S_vib —— 谐振子严格
    Helmholtz 自由能(ASE thermochemistry.HarmonicThermo 同款)。
  室温下二者差 = U_vib(T),通常 ~0.0x eV 量级;报告须写明所用口径。
- 虚频:吸附质弛豫构型残留小虚频(<~50 cm⁻¹)常规做法是忽略或抬到 50 cm⁻¹;
  本模块只报告不擅自处理,交调用方决策(绝不静默篡改)。
- 低频实频地板(harmonic_thermo 的 freq_floor_cm1,默认 50 cm⁻¹):低频实模的熵 S~−ln x
  在 x→0 时发散,催化文献常做法是把低于地板的实频**在熵/热能项上**抬到地板频;ZPE 仍用
  原频(见 harmonic_thermo 口径)。meta 记 n_floored 与被抬原频,报告须写明。

单位:输入 meV(OUTCAR 第 5 列),输出 eV。k_B = 8.617333262e-5 eV/K。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

KB_EV = 8.617333262e-5          # eV/K
DEFAULT_T = 298.15              # K
CM1_TO_EV = 1.239841984e-4      # eV per cm⁻¹(低频地板换算)
DEFAULT_FREQ_FLOOR_CM1 = 50.0   # cm⁻¹:低频熵/热能地板(见 harmonic_thermo)

# OUTCAR 频率行:'1 f  =  ... meV' / '4 f/i=  ... meV'(捕获 meV 值与实/虚)
_FREQ_RE = re.compile(
    r'^\s*\d+\s+f(/i)?\s*=\s*[\d.]+\s+THz\s+[\d.]+\s+2PI\*THz\s+'
    r'([\d.]+)\s+cm-1\s+([\d.]+)\s+meV\s*$')


G_CORR_MODES = ('zpe_ts', 'ase')     # ΔG 校正口径(见模块 docstring)

# 虚频质量闸(F15):|ν| < 噪声阈视作弛豫残留小虚频(可忽略/抬频),≥阈为"大虚频"。
DEFAULT_IMAG_NOISE_CM1 = 50.0
IMAG_CONTEXTS = ('minimum', 'ts')
# 四象限判定 verdict:极小点 clean/noise/bad_minimum;过渡态 valid_ts/invalid_ts。
IMAG_VERDICTS = ('clean', 'noise', 'bad_minimum', 'valid_ts', 'invalid_ts')


@dataclass
class VibResult:
    """频率解析 + 热校正结果(能量单位 eV;频率列表单位 meV)。"""
    real_mev: list = field(default_factory=list)     # 实频(meV)
    imag_mev: list = field(default_factory=list)     # 虚频(meV,报告用)
    imag_cm1: list = field(default_factory=list)     # 虚频(cm-1,更直观)
    zpe_ev: float = 0.0
    u_thermal_ev: float = 0.0                         # U_vib(T) = Σ hν/(e^x−1)(eV)
    ts_ev: float = 0.0                                # T·S_vib(eV,>0)
    temperature: float = DEFAULT_T
    freq_floor_cm1: float = DEFAULT_FREQ_FLOOR_CM1    # 低频地板(cm⁻¹)
    n_floored: int = 0                                # 被地板抬高的实频数(仅熵/热能项)
    floored_cm1: list = field(default_factory=list)   # 被抬原频列表(cm⁻¹,ZPE 仍用原频)

    def g_corr(self, mode: str = 'zpe_ts') -> float:
        """G 校正,按口径选择(单位 eV):

        - mode='zpe_ts'(默认): ZPE − T·S_vib(论文复现基准,吸附质常用简化);
        - mode='ase':          ZPE + U_vib(T) − T·S_vib(ASE HarmonicThermo
          的严格谐振子 Helmholtz 自由能)。
        未知 mode → ValueError(绝不静默给错口径的数)。
        """
        if mode == 'zpe_ts':
            return self.zpe_ev - self.ts_ev
        if mode == 'ase':
            return self.zpe_ev + self.u_thermal_ev - self.ts_ev
        raise ValueError(f'未知 g_corr 口径 {mode!r}(可选:{G_CORR_MODES})')

    @property
    def g_corr_ev(self) -> float:
        """默认口径 G 校正 = ZPE − T·S_vib(= g_corr('zpe_ts'),对齐已发表论文)。"""
        return self.g_corr('zpe_ts')

    @property
    def n_imag(self) -> int:
        return len(self.imag_mev)


def parse_outcar_frequencies(text: str) -> tuple:
    """OUTCAR 文本 → (实频 meV 列表, 虚频 meV 列表, 虚频 cm-1 列表)。

    找不到任何频率行 → ([], [], [])(调用方据此判"不是频率计算")。
    """
    real, imag, imag_cm = [], [], []
    for line in text.splitlines():
        m = _FREQ_RE.match(line)
        if not m:
            continue
        is_imag, cm1, mev = bool(m.group(1)), float(m.group(2)), float(m.group(3))
        if is_imag:
            imag.append(mev)
            imag_cm.append(cm1)
        else:
            real.append(mev)
    return real, imag, imag_cm


def harmonic_thermo(real_mev: list, temperature: float = DEFAULT_T,
                    freq_floor_cm1: float = DEFAULT_FREQ_FLOOR_CM1) -> tuple:
    """实频列表(meV)→ (ZPE, U_vib(T), T·S_vib, meta),前三项单位都是 eV。

    谐振子公式(同一循环顺手把三项都算了;x = hν/k_B T):
    - ZPE      = Σ hν/2
    - U_vib(T) = Σ hν/(e^x−1) = Σ k_B T·x/(e^x−1)(热占据内能,'ase' 口径用)
    - S/k_B    = Σ [ x/(e^x−1) − ln(1−e^{−x}) ]

    低频地板(freq_floor_cm1,默认 50 cm⁻¹):实频**低于地板**者,仅 **熵项 S 与热占据内能
    U_vib** 改用地板频计算(x 用地板频),防低频 S~−ln x 在 x→0 时发散(催化文献常规做法);
    **ZPE 仍按原频**累加(ZPE 对低频不发散,抬频会高估零点能)。freq_floor_cm1≤0 关闭地板。
    忽略 <1e-8 eV 的病态频率;x≥700 时热项/熵项按 0(防溢出,物理上也 ≈0)。
    meta = {'freq_floor_cm1','n_floored','floored_cm1'(被抬原频,cm⁻¹)}。
    """
    zpe = 0.0
    u_thermal = 0.0
    s_over_kb = 0.0
    kt = KB_EV * temperature
    hv_floor = max(freq_floor_cm1, 0.0) * CM1_TO_EV     # 地板频对应能量(eV)
    floored_cm1: list = []
    for mev in real_mev:
        hv = mev / 1000.0                            # eV
        if hv < 1e-8:
            continue
        zpe += hv / 2.0                             # ZPE 用原频(不抬)
        hv_eff = hv
        if hv < hv_floor:                           # 熵/热能项抬到地板频
            hv_eff = hv_floor
            floored_cm1.append(round(mev / (CM1_TO_EV * 1000.0), 1))
        x = hv_eff / kt
        # S/k_B = x/(e^x - 1) - ln(1 - e^{-x});x 大时各热项都趋 0(防溢出截断)
        if x < 700:
            u_thermal += hv_eff / math.expm1(x)
            s_over_kb += x / math.expm1(x) - math.log1p(-math.exp(-x))
    meta = {'freq_floor_cm1': float(freq_floor_cm1),
            'n_floored': len(floored_cm1), 'floored_cm1': floored_cm1}
    return zpe, u_thermal, kt * s_over_kb, meta


def analyze_outcar(path: str | os.PathLike, temperature: float = DEFAULT_T,
                   freq_floor_cm1: float = DEFAULT_FREQ_FLOOR_CM1) -> VibResult | None:
    """读频率计算的 OUTCAR → VibResult;无频率行/文件不可读 → None(绝不编数)。

    freq_floor_cm1 透传 harmonic_thermo(低频熵/热能地板,见其口径)。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    real, imag, imag_cm = parse_outcar_frequencies(text)
    if not real and not imag:
        return None
    zpe, u_thermal, ts, meta = harmonic_thermo(real, temperature, freq_floor_cm1)
    return VibResult(real_mev=real, imag_mev=imag, imag_cm1=imag_cm,
                     zpe_ev=round(zpe, 6), u_thermal_ev=round(u_thermal, 6),
                     ts_ev=round(ts, 6), temperature=temperature,
                     freq_floor_cm1=float(freq_floor_cm1),
                     n_floored=meta['n_floored'], floored_cm1=meta['floored_cm1'])


def classify_imaginary(freqs_cm1: list, *, noise_threshold: float = DEFAULT_IMAG_NOISE_CM1,
                       context: str = 'minimum') -> dict:
    """虚频质量闸(F15):按虚频构成给出四象限判定 + 中文建议。

    freqs_cm1:虚频**幅值**列表(cm⁻¹,即 VibResult.imag_cm1;取绝对值参与判定)。
    noise_threshold:|ν| 低于此视作弛豫残留小虚频(噪声),≥此为"大虚频"。
    context:
    - ``'minimum'``(极小点):无虚频→clean;有虚频但全 <阈→noise(可接受,按实模地板
      计入熵,建议报告注明);出现大虚频→bad_minimum(沿虚频模式扰动重弛豫,勿直接用于 ΔG)。
    - ``'ts'``(过渡态):恰一个大虚频→valid_ts;0 个或 ≥2 个大虚频→invalid_ts。

    Returns ``{'verdict','n_imag','n_imag_large','max_imag_cm1','advice','usable_for_thermo',
    'noise_threshold_cm1','context'}``。usable_for_thermo=False 的结果不应静默进 ΔG。
    """
    if context not in IMAG_CONTEXTS:
        raise ValueError(f'未知 context {context!r}(可选:{IMAG_CONTEXTS})')
    mags = [abs(float(c)) for c in (freqs_cm1 or [])]
    n_imag = len(mags)
    large = [c for c in mags if c >= noise_threshold]
    n_large = len(large)
    max_imag = round(max(mags), 1) if mags else 0.0

    if context == 'ts':
        if n_large == 1:
            verdict, usable = 'valid_ts', True
            advice = f'恰一个大虚频({max_imag:g} cm⁻¹),合格一阶鞍点;热校正用其余实模。'
        elif n_large == 0:
            verdict, usable = 'invalid_ts', False
            advice = ('过渡态应恰有一个大虚频,却未检出;可能已滑落到极小点,'
                      '需重新搜索过渡态,勿用于能垒/ΔG。')
        else:
            verdict, usable = 'invalid_ts', False
            advice = (f'过渡态出现 {n_large} 个大虚频(高阶鞍点),'
                      f'需沿多余虚频模式优化到一阶鞍点。')
    else:  # minimum
        if n_imag == 0:
            verdict, usable = 'clean', True
            advice = '无虚频,合格极小点。'
        elif n_large == 0:
            verdict, usable = 'noise', True
            advice = (f'仅残留小虚频(全部 <{noise_threshold:g} cm⁻¹,最大 {max_imag:g}),'
                      f'可接受;建议报告注明并按实模地板计入熵。')
        else:
            verdict, usable = 'bad_minimum', False
            advice = (f'存在大虚频(最大 {max_imag:g} cm⁻¹),沿该虚频模式扰动后重弛豫,'
                      f'勿直接用于 ΔG。')

    return {'verdict': verdict, 'n_imag': n_imag, 'n_imag_large': n_large,
            'max_imag_cm1': max_imag, 'advice': advice, 'usable_for_thermo': usable,
            'noise_threshold_cm1': float(noise_threshold), 'context': context}


def load_corrections(freq_dirs: dict, temperature: float = DEFAULT_T,
                     mode: str = 'zpe_ts',
                     freq_floor_cm1: float = DEFAULT_FREQ_FLOOR_CM1,
                     imag_noise_cm1: float = DEFAULT_IMAG_NOISE_CM1,
                     contexts: dict | None = None) -> dict:
    """{物种: 频率作业目录} → {物种: {'g_corr','zpe','u_thermal','ts','n_imag','imag_cm1',
    'n_floored','floored_cm1','classify','usable_for_thermo'[,'excluded','exclude_reason']}}。

    mode 选 g_corr 口径(见 VibResult.g_corr):默认 'zpe_ts' = ZPE−TS(论文
    复现基准);'ase' = ZPE+U_vib−TS(严格谐振子)。zpe/u_thermal/ts 三项原样
    给出,调用方可自行复核任一口径。freq_floor_cm1 透传 harmonic_thermo(低频熵/热能地板)。
    不读 config,模式/地板由调用方显式传入。
    目录缺 OUTCAR/无频率 → 该物种跳过(不进结果;调用方按"无校正"处理并明示)。

    虚频质量闸(F15):每物种附 classify_imaginary 结果('classify')与 'usable_for_thermo'。
    imag_noise_cm1 为小虚频噪声阈;contexts={物种:'minimum'|'ts'} 可给个别物种指定
    过渡态口径(默认全按 'minimum')。usable_for_thermo=False 的物种**不静默入 ΔG**:
    额外标 'excluded'=True 与 'exclude_reason'(中文),由调用方决定是否拦截。
    """
    out = {}
    contexts = dict(contexts or {})
    for sp, d in (freq_dirs or {}).items():
        r = analyze_outcar(os.path.join(str(d), 'OUTCAR'), temperature, freq_floor_cm1)
        if r is None:
            continue
        cls = classify_imaginary(r.imag_cm1, noise_threshold=imag_noise_cm1,
                                 context=contexts.get(sp, 'minimum'))
        row = {'g_corr': round(r.g_corr(mode), 6), 'zpe': r.zpe_ev,
               'u_thermal': r.u_thermal_ev, 'ts': r.ts_ev, 'n_imag': r.n_imag,
               'imag_cm1': [round(c, 1) for c in r.imag_cm1],
               'n_floored': r.n_floored, 'floored_cm1': list(r.floored_cm1),
               'classify': cls, 'usable_for_thermo': cls['usable_for_thermo']}
        if not cls['usable_for_thermo']:
            # 质量闸不放行 → 显式标记,绝不让坏虚频物种静默进 ΔG(调用方决定拦截)
            row['excluded'] = True
            row['exclude_reason'] = cls['advice']
        out[sp] = row
    return out
