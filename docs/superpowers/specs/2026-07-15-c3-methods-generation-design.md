# C3 计算方法段自动生成 — 设计 (spec / 实施报告)

> 对齐 `2026-07-14-ui-redesign-pywebview-design.md` §C.3。读**作业目录里真实的**
> INCAR/KPOINTS/POTCAR → 中英双语 Methods 段 + BibTeX。与实际计算强一致,非模板
> 文字;调研确认竞品无人做,差异化点。

## 目标与非目标

**目标**:`methods_text(job_dir)` 一键产出:
- 事实抽取(facts):泛函(**GGA 标签优先于 POTCAR 味**——本项目 GGA=RP + PAW_PBE
  赝势 = RPBE 泛函/PBE 赝势,措辞必须精确)、ENCUT、K 网格(scheme+grid)、
  smearing(ISMEAR/SIGMA)、收敛判据(EDIFF/EDIFFG,EDIFFG<0=力判据)、
  IVDW 色散校正映射、ISPIN、弛豫算法(IBRION/NSW)、赝势 TITEL 逐条(variant+日期)、
  LDAU(若有,如实报)。
- 中/英双语 Methods 段(期刊风格散文,只写文件里真有的;缺文件/缺键明说,绝不编造)。
- BibTeX:只引真用到的——VASP(Kresse-Furthmüller PRB 54,11169 (1996);CMS 6,15 (1996))、
  PAW(Blöchl PRB 50,17953 (1994);Kresse-Joubert PRB 59,1758 (1999))、
  泛函(PBE: Perdew et al. PRL 77,3865 (1996);RPBE: Hammer et al. PRB 59,7413 (1999))、
  色散(D3: Grimme et al. JCP 132,154104 (2010);D3(BJ): Grimme et al. JCC 32,1456 (2011))。

**不做**:LaTeX 全文/Word 导出(报告线已有)、引文样式定制、非 VASP 码支持。

## 映射表(科学口径,模块常量)

- GGA:PE=PBE, RP=RPBE, PS=PBEsol, 91=PW91, RE=revPBE, AM=AM05;无 GGA 键 →
  以 POTCAR 味(PAW_PBE→PBE, PAW_GGA→PW91, PAW_LDA→LDA)。
- IVDW:1/10=DFT-D2, 11=DFT-D3(zero), 12=DFT-D3(BJ), 2/20=TS, 21=TS+SCS, 4=dDsC;缺省/0=无。
- ISMEAR:0=Gaussian, >0=Methfessel-Paxton N 阶, -5=tetrahedron+Blöchl, -1=Fermi。
- IBRION:2=CG, 1=quasi-Newton(RMM-DIIS), 3=damped MD, 0=MD;NSW=0 或缺 IBRION → 静态。
- KPOINTS:auto 四件套是 Gamma/MP + 网格行;line-mode/显式列表降级为原文摘要。

## 分层

- 纯模块 `vcstudio/generate/methods_text.py`(零 IO):
  `parse_potcar_titels(text)`、`parse_kpoints_scheme(text)`、
  `extract_facts(incar_text, kpoints_text, potcar_text) -> {'facts','warnings'}`、
  `render_zh(facts)` / `render_en(facts)` / `render_bibtex(facts)`。
  复用 `incar_builder.parse_incar` 与 `potcar._TITEL_RE` 口径(TITEL 正则复制,不 import 私有名)。
- api `methods_text(job_dir) -> {'ok','zh','en','bibtex','warnings'|'error'}`:
  读 job_dir 三文件(缺哪个 warnings 明说哪个,INCAR 缺则直接 error);注入 `methods_mod`。
- 前端 jobs.js 行内「方法」按钮 → modal(zh/en/BibTeX 三段 `<pre>` + 各自「复制」,
  clipboard 失败降级选中文本);零 emoji、中文界面文案。

## 测试策略

TDD:真实 INCAR 片段(GGA=RP/ENCUT=400/IVDW=11/ISPIN=2/EDIFFG=-0.03)断言 facts 与
双语文本关键句(RPBE、D3 zero、力收敛 0.03 eV/Å);无 GGA 键→PBE;缺 KPOINTS/POTCAR
降级 warnings;BibTeX 只含用到的条目;api happy+缺文件+注入。全量 pytest 全绿(基线 389+1)。

## 约束(同 C1/C2)

运行时零 CDN;方法学键只读绝不改;api 薄+注入+异常兜底;窄 pathspec 提交。
