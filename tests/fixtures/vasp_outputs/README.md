# 合成 VASP 输出格式变体样本 / Synthetic VASP-output format variants

**这些不是真实 VASP 产物。** 本目录的 `OUTCAR`/`OSZICAR` 是**人工合成的逼真
格式变体样本**,用于:

1. **解析器格式鲁棒性**——覆盖常见的产出形态(多离子步弛豫、含自旋、
   重启拼接),防止本项目解析器(`freeenergy.read_e0`、
   `convergence.parse_oszicar` 等)在某种真实排版下悄悄失效;
2. **ASE 交叉校验(对拍)**——每组的最终能量同时被本项目解析器与
   `ase.io.read(..., format='vasp-out')` 读取,两侧结果必须一致(容差 1e-6),
   把"本项目读到的 E0"钉在一个独立实现上。

> **These are NOT real VASP outputs.** They are hand-synthesized, realistic
> *format variants* used to guard parser robustness and to cross-check final
> energies against ASE's independent `vasp-out` reader.

## 目录约定 / Layout

每个子目录是一组样本,至少含 `OUTCAR` 与 `OSZICAR`:

| 子目录 | 覆盖的格式变体 |
|---|---|
| `relax_multistep_co/` | 多离子步几何弛豫(4 步收敛,CO 分子) |
| `spin_polarized_fe/` | 自旋极化(`ISPIN=2` + 磁矩行,bcc Fe) |
| `restart_concat_h2/` | 重启拼接(seg1 墙钟被杀无页脚 + seg2 续算,单文件含两段头) |

构造保证:各组 `OSZICAR` 末个 `E0` 与 `OUTCAR` 末离子步 `energy(sigma->0)`
按同一数值派生,故两侧对拍能量按构造一致;`OSZICAR` 离子步数与 `OUTCAR`
的离子步块数一致。

## 放入真实样本 / Dropping in real samples

**把真实 VASP 作业目录(含 `OUTCAR` 与 `OSZICAR`)拷进本目录即自动纳入测试**
——`tests/test_parser_equivalence.py` 扫描本目录下所有同时含 `OUTCAR` 和
`OSZICAR` 的子目录并参数化对拍,无需改动测试代码。真实样本能进一步验证解析器
面对真实排版的鲁棒性。

> Drop a real VASP job directory (with `OUTCAR` + `OSZICAR`) here and it is
> auto-discovered by `tests/test_parser_equivalence.py` — no test edits needed.
