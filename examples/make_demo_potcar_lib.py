"""生成演示用假 POTCAR 库 — 仅供离线体验 vcs gen 全链。

!! 警告:这是假数据,严禁用于任何真实 VASP 计算 !!
真实计算必须使用你所在机构持有许可的 PAW 赝势库(POTCAR 受版权保护,
本仓库依法不包含任何真实赝势数据)。

用法: python examples/make_demo_potcar_lib.py <输出目录>
"""
import os
import sys

# 常用元素的真实 ENMAX 量级(仅用于让 ENCUT>=ENMAX 检查行为逼真)
_ENMAX = {'C': 273.214, 'H': 250.000, 'O': 400.000, 'N': 400.000,
          'S': 258.689, 'Li': 140.000, 'Mo': 224.580, 'Co': 267.965}


def build_lib(root: str) -> None:
    for el, enmax in _ENMAX.items():
        d = os.path.join(root, el)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'POTCAR'), 'w', encoding='utf-8') as f:
            f.write(f' fake PAW_PBE {el}\n'
                    f'   ENMAX  =  {enmax}; ENMIN = 200.000 eV\n')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    build_lib(sys.argv[1])
    print(f'假 POTCAR 库已生成: {sys.argv[1]} (仅演示,勿用于真实计算)')
