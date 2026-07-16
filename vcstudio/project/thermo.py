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

# OUTCAR 频率行:'1 f  =  ... meV' / '4 f/i=  ... meV'(捕获 meV 值与实/虚)
_FREQ_RE = re.compile(
    r'^\s*\d+\s+f(/i)?\s*=\s*[\d.]+\s+THz\s+[\d.]+\s+2PI\*THz\s+'
    r'([\d.]+)\s+cm-1\s+([\d.]+)\s+meV\s*$')


G_CORR_MODES = ('zpe_ts', 'ase')     # ΔG 校正口径(见模块 docstring)


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


def harmonic_thermo(real_mev: list, temperature: float = DEFAULT_T) -> tuple:
    """实频列表(meV)→ (ZPE, U_vib(T), T·S_vib),单位都是 eV。

    谐振子公式(同一循环顺手把三项都算了;x = hν/k_B T):
    - ZPE      = Σ hν/2
    - U_vib(T) = Σ hν/(e^x−1) = Σ k_B T·x/(e^x−1)(热占据内能,'ase' 口径用)
    - S/k_B    = Σ [ x/(e^x−1) − ln(1−e^{−x}) ]
    忽略 <1e-8 eV 的病态频率;x≥700 时热项/熵项按 0(防溢出,物理上也 ≈0)。
    """
    zpe = 0.0
    u_thermal = 0.0
    s_over_kb = 0.0
    kt = KB_EV * temperature
    for mev in real_mev:
        hv = mev / 1000.0                            # eV
        if hv < 1e-8:
            continue
        zpe += hv / 2.0
        x = hv / kt
        # S/k_B = x/(e^x - 1) - ln(1 - e^{-x});x 大时各热项都趋 0(防溢出截断)
        if x < 700:
            u_thermal += hv / math.expm1(x)
            s_over_kb += x / math.expm1(x) - math.log1p(-math.exp(-x))
    return zpe, u_thermal, kt * s_over_kb


def analyze_outcar(path: str | os.PathLike,
                   temperature: float = DEFAULT_T) -> VibResult | None:
    """读频率计算的 OUTCAR → VibResult;无频率行/文件不可读 → None(绝不编数)。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return None
    real, imag, imag_cm = parse_outcar_frequencies(text)
    if not real and not imag:
        return None
    zpe, u_thermal, ts = harmonic_thermo(real, temperature)
    return VibResult(real_mev=real, imag_mev=imag, imag_cm1=imag_cm,
                     zpe_ev=round(zpe, 6), u_thermal_ev=round(u_thermal, 6),
                     ts_ev=round(ts, 6), temperature=temperature)


def load_corrections(freq_dirs: dict, temperature: float = DEFAULT_T,
                     mode: str = 'zpe_ts') -> dict:
    """{物种: 频率作业目录} → {物种: {'g_corr','zpe','u_thermal','ts','n_imag','imag_cm1'}}。

    mode 选 g_corr 口径(见 VibResult.g_corr):默认 'zpe_ts' = ZPE−TS(论文
    复现基准);'ase' = ZPE+U_vib−TS(严格谐振子)。zpe/u_thermal/ts 三项原样
    给出,调用方可自行复核任一口径。不读 config,模式由调用方显式传入。
    目录缺 OUTCAR/无频率 → 该物种跳过(不进结果;调用方按"无校正"处理并明示)。
    虚频只报告(imag_cm1),处理决策(忽略/抬频/重优化)交人工。
    """
    out = {}
    for sp, d in (freq_dirs or {}).items():
        r = analyze_outcar(os.path.join(str(d), 'OUTCAR'), temperature)
        if r is None:
            continue
        out[sp] = {'g_corr': round(r.g_corr(mode), 6), 'zpe': r.zpe_ev,
                   'u_thermal': r.u_thermal_ev, 'ts': r.ts_ev, 'n_imag': r.n_imag,
                   'imag_cm1': [round(c, 1) for c in r.imag_cm1]}
    return out
