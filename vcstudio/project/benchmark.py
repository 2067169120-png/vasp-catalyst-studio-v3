"""吸附能验证基准:计算值 vs 独立参考值 → 逐项误差 + MAE。

对标 Montoya & Persson (npj Comput. Mater. 3:14, 2017) 的验证方法学:
与独立参考数据集做定量对比并报告 MAE。参考值可以来自文献表格、
实验数据库或另一套已发表计算。纯函数,不触网不触集群。
"""
from __future__ import annotations


def load_csv(path: str) -> dict[str, float]:
    """两列 CSV(name,energy_eV);# 开头行与空行跳过。"""
    out: dict[str, float] = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            name, val = line.split(',', 1)
            out[name.strip()] = float(val)
    return out


def compare_to_reference(computed: dict[str, float],
                         reference: dict[str, float]) -> dict:
    """逐名对齐求误差;无交集直接抛错(空基准无意义,绝不静默给 0)。"""
    common = sorted(set(computed) & set(reference))
    if not common:
        raise ValueError('computed 与 reference 无共同体系,无法对比')
    rows = [{'name': k, 'computed': computed[k], 'reference': reference[k],
             'error': computed[k] - reference[k]} for k in common]
    mae = sum(abs(r['error']) for r in rows) / len(rows)
    missing = sorted(set(reference) - set(computed))
    return {'rows': rows, 'mae': mae, 'n': len(rows), 'missing': missing}


def render_markdown(result: dict) -> str:
    """出版可用的 Markdown 对比表(带 MAE 汇总与未覆盖清单)。"""
    lines = ['| System | Computed (eV) | Reference (eV) | Error (eV) |',
             '|---|---|---|---|']
    for r in result['rows']:
        lines.append(f"| {r['name']} | {r['computed']:.3f} | "
                     f"{r['reference']:.3f} | {r['error']:+.3f} |")
    lines.append('')
    lines.append(f"**MAE = {result['mae']:.3f} eV** (n = {result['n']})")
    if result['missing']:
        lines.append(f"\n未覆盖参考体系: {', '.join(result['missing'])}")
    return '\n'.join(lines)
