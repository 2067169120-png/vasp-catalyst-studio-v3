# Publication Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 vcstudio 补齐到软件论文(JOSS/CPC 路线)可投稿的工程要件水平:LICENSE、CI、引用/贡献文件、可跑示例、验证基准管线、竞品对比、英文 README、JOSS paper 草稿。

**Architecture:** 全部为增量文件(零源码行为改动,唯一新增模块 `vcstudio/project/benchmark.py` 为纯函数);每项要件配结构校验测试,防止后续腐化(如 LICENSE 被误删、CI yaml 语法坏)。

**Tech Stack:** Python(既有栈:PyYAML/pytest),GitHub Actions YAML,JOSS paper.md 格式。

## Global Constraints

- 分支 `feature/gui-exe-config`,不新建分支不合 master
- 每个 commit 带窄 pathspec(`git commit -- <files>`),树上可能有并行 WIP
- 全量 pytest 全绿;当前基线 **413 passed 1 skipped**
- TDD:先写失败测试再实现(纯文档任务除外,文档任务以全量测试仍绿为门)
- 方法学键(ENCUT/泛函/IVDW/ISPIN)绝不改;不碰 vcstudio 现有源码行为
- 真实 POTCAR 数据绝不入库(版权);示例只用假库+醒目警告
- 每完成一个任务立刻追加 `.superpowers/sdd/progress.md`

---

### Task 1: LICENSE (MIT) + pyproject 元数据

**Files:**
- Create: `LICENSE`
- Modify: `pyproject.toml`(`[project]` 节)
- Test: `tests/test_packaging_meta.py`

**Interfaces:**
- Produces: 仓库根 `LICENSE`(MIT 纯文本);pyproject `license={text="MIT"}` + classifiers + urls。Task 2/8 依赖本测试文件追加用例。

- [ ] **Step 1: 写失败测试**

```python
"""发刊要件结构校验:LICENSE / pyproject 元数据。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def test_license_file_is_mit_plain_text():
    # JOSS 硬性要求:OSI 许可证纯文本文件(README 里提名字不算)
    text = _read('LICENSE')
    assert 'MIT License' in text
    assert 'Copyright' in text
    assert 'WITHOUT WARRANTY OF ANY KIND' in text.upper() or \
           'WITHOUT WARRANTY' in text.upper()


def test_pyproject_declares_license_and_metadata():
    text = _read('pyproject.toml')
    assert 'license' in text
    assert 'MIT' in text
    assert 'classifiers' in text
    assert 'License :: OSI Approved :: MIT License' in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_packaging_meta.py -v`
Expected: FAIL(FileNotFoundError: LICENSE)

- [ ] **Step 3: 创建 LICENSE(标准 MIT 全文)**

```
MIT License

Copyright (c) 2026 VASP Catalyst Studio Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

(版权行署名 "VASP Catalyst Studio Contributors" 为占位实体,发刊前由用户改真实署名。)

- [ ] **Step 4: pyproject `[project]` 节追加(不动 dependencies/scripts)**

```toml
license = {text = "MIT"}
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Science/Research",
    "License :: OSI Approved :: MIT License",
    "Operating System :: Microsoft :: Windows",
    "Programming Language :: Python :: 3",
    "Topic :: Scientific/Engineering :: Chemistry",
    "Topic :: Scientific/Engineering :: Physics",
]

[project.urls]
Repository = "https://github.com/vasp-catalyst-studio/vcstudio"
```

(Repository URL 为预留;用户建仓后如不同再改——README/CITATION 同步引用此占位。)

- [ ] **Step 5: 跑测试确认通过 + 全量绿**

Run: `python -m pytest tests/test_packaging_meta.py -v && python -m pytest -q`
Expected: 新测试 PASS;全量 415 passed 1 skipped(+2)

- [ ] **Step 6: Commit**

```bash
git add LICENSE pyproject.toml tests/test_packaging_meta.py
git commit -m "chore(publication): MIT LICENSE + pyproject metadata (classifiers/urls)" -- LICENSE pyproject.toml tests/test_packaging_meta.py
```

---

### Task 2: CITATION.cff + CONTRIBUTING.md

**Files:**
- Create: `CITATION.cff`, `CONTRIBUTING.md`
- Modify: `tests/test_packaging_meta.py`(追加 2 用例)

**Interfaces:**
- Consumes: Task 1 的 `tests/test_packaging_meta.py`(追加,不重建)
- Produces: 根目录 `CITATION.cff`(yaml 可解析)+ `CONTRIBUTING.md`(英文)。Task 7 README 引用二者。

- [ ] **Step 1: 追加失败测试**

```python
def test_citation_cff_parses_with_required_keys():
    import yaml
    data = yaml.safe_load(_read('CITATION.cff'))
    assert data['cff-version']
    assert data['title'] == 'VASP Catalyst Studio'
    assert data['authors'] and data['version'] and data['message']


def test_contributing_covers_tests_and_issues():
    text = _read('CONTRIBUTING.md')
    assert 'pytest' in text
    assert 'issue' in text.lower()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_packaging_meta.py -v`
Expected: 2 FAIL(FileNotFoundError)

- [ ] **Step 3: 写 CITATION.cff**

```yaml
cff-version: 1.2.0
message: "If you use this software, please cite it as below."
title: "VASP Catalyst Studio"
version: 0.1.0
date-released: 2026-07-15
license: MIT
repository-code: "https://github.com/vasp-catalyst-studio/vcstudio"
authors:
  - name: "VASP Catalyst Studio Contributors"
keywords:
  - VASP
  - density functional theory
  - high-throughput screening
  - catalysis
  - workflow automation
```

- [ ] **Step 4: 写 CONTRIBUTING.md(英文,内容:dev install `pip install -e .[dev]`;跑测试 `python -m pytest`;TDD 约定(先失败测试);中文注释/英文标识符约定;bug 报告走 issue tracker 附 OS/Python 版本/复现步骤;方法学键不可改的红线)**

- [ ] **Step 5: 跑测试确认通过 + 全量绿**

Run: `python -m pytest tests/test_packaging_meta.py -v && python -m pytest -q`
Expected: PASS;417 passed 1 skipped

- [ ] **Step 6: Commit**

```bash
git add CITATION.cff CONTRIBUTING.md tests/test_packaging_meta.py
git commit -m "chore(publication): CITATION.cff + CONTRIBUTING guide" -- CITATION.cff CONTRIBUTING.md tests/test_packaging_meta.py
```

---

### Task 3: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Test: `tests/test_ci_config.py`

**Interfaces:**
- Produces: CI 配置(推送到 GitHub 后自动激活;本地处于休眠,无副作用)。

- [ ] **Step 1: 写失败测试**

```python
"""CI 配置结构校验:yaml 可解析 + 关键步骤存在(防手滑改坏)。"""
import os
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_ci_yaml_parses_and_runs_pytest_on_matrix():
    path = os.path.join(ROOT, '.github', 'workflows', 'ci.yml')
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    # PyYAML 把裸 on 解析成 True 键
    assert 'on' in data or True in data
    job = data['jobs']['test']
    matrix = job['strategy']['matrix']
    assert 'ubuntu-latest' in matrix['os'] and 'windows-latest' in matrix['os']
    steps_text = str(job['steps'])
    assert 'pytest' in steps_text
    assert 'pip install -e' in steps_text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ci_config.py -v`
Expected: FAIL(FileNotFoundError)

- [ ] **Step 3: 写 ci.yml**

```yaml
name: CI

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python-version: ["3.10", "3.12"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: pip install -e .[dev]
      - name: Test
        run: python -m pytest -q
```

- [ ] **Step 4: 跑测试确认通过 + 全量绿**

Run: `python -m pytest tests/test_ci_config.py -v && python -m pytest -q`
Expected: PASS;418 passed 1 skipped

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml tests/test_ci_config.py
git commit -m "ci: GitHub Actions pytest matrix (ubuntu/windows x py3.10/3.12)" -- .github/workflows/ci.yml tests/test_ci_config.py
```

---

### Task 4: examples/quickstart 可跑示例

**Files:**
- Create: `examples/quickstart/POSCAR`, `examples/quickstart/INCAR`, `examples/quickstart/README.md`, `examples/make_demo_potcar_lib.py`
- Test: `tests/test_examples.py`

**Interfaces:**
- Consumes: `vcstudio.generate.job_builder.build_job_dir(poscar_path, incar, out_dir, *, calc_type, lib_root)`(既有)
- Produces: 端到端可跑示例;Task 7 README 的 Quickstart 节指向它。

- [ ] **Step 1: 写失败测试**

```python
"""examples/quickstart 端到端:假库生成脚本 + build_job_dir 出四件套。"""
import os
import subprocess
import sys

from vcstudio.generate.job_builder import build_job_dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, 'examples', 'quickstart')


def test_demo_potcar_lib_script_builds_fake_library(tmp_path):
    lib = tmp_path / 'demo_lib'
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'examples', 'make_demo_potcar_lib.py'),
         str(lib)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (lib / 'C' / 'POTCAR').read_text(encoding='utf-8')
    assert 'fake PAW_PBE C' in text and 'ENMAX' in text


def test_quickstart_example_generates_four_files(tmp_path):
    lib = tmp_path / 'demo_lib'
    subprocess.run(
        [sys.executable, os.path.join(ROOT, 'examples', 'make_demo_potcar_lib.py'),
         str(lib)], check=True)
    out = tmp_path / 'job'
    res = build_job_dir(os.path.join(EX, 'POSCAR'), os.path.join(EX, 'INCAR'),
                        str(out), calc_type='slab', lib_root=str(lib))
    assert res['ok']
    for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'):
        assert (out / name).is_file(), name
    # 二维 slab → kz=1
    assert res['kpoints'][2] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_examples.py -v`
Expected: FAIL(脚本不存在)

- [ ] **Step 3: 写示例文件**

`examples/quickstart/POSCAR`(石墨烯 2 原子,15 Å 真空):

```
Graphene demo (2-atom primitive cell, 15 A vacuum)
1.0
   2.4680000000   0.0000000000   0.0000000000
  -1.2340000000   2.1373507800   0.0000000000
   0.0000000000   0.0000000000  15.0000000000
C
2
Direct
  0.0000000000  0.0000000000  0.5000000000
  0.3333333333  0.6666666667  0.5000000000
```

`examples/quickstart/INCAR`:

```
SYSTEM = graphene-demo
ENCUT = 400
ISMEAR = 0
SIGMA = 0.05
IBRION = 2
NSW = 50
EDIFF = 1E-5
EDIFFG = -0.02
```

`examples/make_demo_potcar_lib.py`:

```python
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
```

`examples/quickstart/README.md`(双语):walkthrough — ①`python examples/make_demo_potcar_lib.py demo_lib` ②`vcs gen --poscar examples/quickstart/POSCAR --incar examples/quickstart/INCAR --calc-type slab --lib-root demo_lib -o results/demo01`(如 CLI 无 `--lib-root` 旗标则写 config.yaml 的赝势库键,以 CLI 实际为准)③检查四件套与 job.yaml;附真实赝势库替换说明与版权警告。

- [ ] **Step 4: 跑测试确认通过 + 全量绿**

Run: `python -m pytest tests/test_examples.py -v && python -m pytest -q`
Expected: PASS;420 passed 1 skipped

- [ ] **Step 5: Commit**

```bash
git add examples/ tests/test_examples.py
git commit -m "docs(examples): offline runnable quickstart (graphene demo + fake POTCAR lib script)" -- examples tests/test_examples.py
```

---

### Task 5: 验证基准管线 vcstudio/project/benchmark.py

**Files:**
- Create: `vcstudio/project/benchmark.py`, `docs/validation.md`
- Test: `tests/test_benchmark.py`

**Interfaces:**
- Consumes: 无(纯函数,不依赖集群)
- Produces: `compare_to_reference(computed: dict[str, float], reference: dict[str, float]) -> dict`(keys: `rows`(list[dict name/computed/reference/error]), `mae`, `n`, `missing`(在 reference 有而 computed 缺的名单));`render_markdown(result) -> str`;`load_csv(path) -> dict[str, float]`(两列 name,energy_eV,# 注释行跳过)。Task 8 paper 草稿引用 docs/validation.md 协议。

- [ ] **Step 1: 写失败测试**

```python
"""吸附能验证基准:CSV 载入 / MAE 对比 / Markdown 渲染。"""
import pytest

from vcstudio.project import benchmark


def test_load_csv_skips_comments(tmp_path):
    p = tmp_path / 'ref.csv'
    p.write_text('# name,energy_eV\nCo_S8,-1.23\nV_Li2S6,-2.5\n', encoding='utf-8')
    d = benchmark.load_csv(str(p))
    assert d == {'Co_S8': -1.23, 'V_Li2S6': -2.5}


def test_compare_reports_mae_and_missing():
    computed = {'a': -1.0, 'b': -2.0}
    reference = {'a': -1.2, 'b': -1.8, 'c': -3.0}
    r = benchmark.compare_to_reference(computed, reference)
    assert r['n'] == 2
    assert r['mae'] == pytest.approx(0.2)
    assert r['missing'] == ['c']
    errs = {row['name']: row['error'] for row in r['rows']}
    assert errs['a'] == pytest.approx(0.2)


def test_compare_empty_overlap_raises():
    with pytest.raises(ValueError):
        benchmark.compare_to_reference({'x': 1.0}, {'y': 2.0})


def test_render_markdown_table():
    r = benchmark.compare_to_reference({'a': -1.0}, {'a': -1.2})
    md = benchmark.render_markdown(r)
    assert '| a |' in md and 'MAE' in md
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_benchmark.py -v`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 实现 benchmark.py**

```python
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
```

- [ ] **Step 4: 写 docs/validation.md**(协议文档:目标=Li-S SAC 吸附能与文献参考值对比;数据来源=集群已收敛作业(待 VPN 回填);流程=`load_csv` 两份 CSV → `compare_to_reference` → `render_markdown` 进论文;明确当前状态"管线就绪,真实数据待回填")

- [ ] **Step 5: 跑测试确认通过 + 全量绿**

Run: `python -m pytest tests/test_benchmark.py -v && python -m pytest -q`
Expected: PASS;424 passed 1 skipped

- [ ] **Step 6: Commit**

```bash
git add vcstudio/project/benchmark.py docs/validation.md tests/test_benchmark.py
git commit -m "feat(benchmark): adsorption-energy validation pipeline (MAE vs reference, Montoya-style protocol)" -- vcstudio/project/benchmark.py docs/validation.md tests/test_benchmark.py
```

---

### Task 6: 竞品对比矩阵 docs/comparison.md

**Files:**
- Create: `docs/comparison.md`

**Interfaces:**
- Produces: 英文功能对比矩阵(vcstudio vs VASPKIT/qvasp/ALKEMIE/atomate2+custodian/AiiDA+AiiDAlab/pyiron/VASPilot/AutoDFT),行=GUI 形态/调度器/自愈机制(规则 vs LLM)/离线性/单文件分发/收敛可视化/提交前结构撞车拦截/Methods 段生成/DOS 出图/provenance/测试与 CI;每列附期刊引用(spec 的对标表)。结尾"差异化定位"段供 Task 7 README 与 Task 8 paper 复用。

- [ ] **Step 1: 写 docs/comparison.md**(内容以 spec 对标表为源,矩阵用 ✔/✘/部分;诚实标注 vcstudio 的短板:无 provenance DAG、无多代码支持、Windows-only GUI)

- [ ] **Step 2: 全量测试仍绿(纯文档)**

Run: `python -m pytest -q`
Expected: 424 passed 1 skipped

- [ ] **Step 3: Commit**

```bash
git add docs/comparison.md
git commit -m "docs(publication): feature comparison matrix vs published tools" -- docs/comparison.md
```

---

### Task 7: 英文 README + 英文 user guide

**Files:**
- Modify: `README.md`(重写为英文为主双语)
- Create: `docs/user-guide-en.md`

**Interfaces:**
- Consumes: Task 4 examples 路径、Task 6 comparison.md、Task 2 CONTRIBUTING/CITATION
- Produces: README 节:Statement of Need / Features / Install(pip -e + EXE 双轨)/ Quickstart(指 examples)/ Testing(真实测试数)/ Comparison(链 docs/comparison.md)/ Contributing / Citation / License;中文速览节保留原 README 核心信息(架构表/科学约定/入口)。

- [ ] **Step 1: 重写 README.md**(英文主体+`## 中文速览`节;测试数用当轮 pytest 实测值替换过期的 207;保留 STRUCTURE.md/使用说明.md 链接;"OpenClaw 论文"表述改为中性架构描述,除非仓库内有该文档依据)

- [ ] **Step 2: 写 docs/user-guide-en.md**(使用说明.md 的英文精简版:四页 GUI 流程 gen→submit→monitor→analyze→report、CLI 用法、配置文件、凭据安全模型、故障分类与有界恢复语义)

- [ ] **Step 3: 全量测试仍绿**

Run: `python -m pytest -q`
Expected: 424 passed 1 skipped

- [ ] **Step 4: Commit**

```bash
git add README.md docs/user-guide-en.md
git commit -m "docs(publication): English-first bilingual README + English user guide" -- README.md docs/user-guide-en.md
```

---

### Task 8: JOSS paper 草稿 + 收尾

**Files:**
- Create: `paper/paper.md`, `paper/paper.bib`
- Modify: `.superpowers/sdd/progress.md`(台账收尾)

**Interfaces:**
- Consumes: Task 5 validation 协议、Task 6 对比矩阵、Task 7 README statement of need

- [ ] **Step 1: 写 paper/paper.bib**(真实文献,来自调研:AiiDA SciData 2020 DOI 10.1038/s41597-020-00638-4;VASPKIT CPC 267:108033 2021;qvasp CPC 257:107535 2020;ALKEMIE CMS 186:110064 2021;atomate2 Digital Discovery 4:1944 2025 DOI 10.1039/D5DD00019J;pyiron npj Comput. Mater. 2024 s41524-024-01441-0;VASPilot arXiv:2508.07035;Montoya npj Comput. Mater. 3:14 2017;ASE J. Phys. Condens. Matter 29:273002 2017;Kresse & Furthmüller PRB 54:11169 1996)

- [ ] **Step 2: 写 paper/paper.md**(JOSS 格式 front-matter:title/tags/authors(`<AUTHOR-TODO>` 明标)/affiliations/date/bibliography;正文:Summary、Statement of need(引对比矩阵差异化)、Functionality(六大功能)、Validation(引 docs/validation.md 协议,注明数据回填中)、Acknowledgements)

- [ ] **Step 3: 全量测试 + 数字核准**

Run: `python -m pytest -q`
Expected: 424 passed 1 skipped;README 中测试数与实测一致

- [ ] **Step 4: Commit + 台账**

```bash
git add paper/
git commit -m "docs(paper): JOSS paper draft (summary, statement of need, validation protocol)" -- paper
# progress.md 追加本计划完成记录后:
git add .superpowers/sdd/progress.md
git commit -m "docs(sdd): publication-readiness plan complete" -- .superpowers/sdd/progress.md
```

---

## Self-Review

- Spec 覆盖:LICENSE(T1)/引用与贡献(T2)/CI(T3)/示例(T4)/验证基准(T5)/竞品对比(T6)/英文文档(T7)/paper 草稿(T8)✔;spec"不做"清单未混入 ✔
- 占位符:paper.md 作者字段是**用户身份数据**,以 `<AUTHOR-TODO>` 显式标注并列入 spec"需用户完成事项",非计划失败 ✔
- 类型一致:`compare_to_reference` 返回 dict 键 rows/mae/n/missing 在 T5 测试与实现一致;`build_job_dir` 签名与仓库实际核实一致 ✔
- 风险:CLI 是否有 `--lib-root` 旗标未核准 → T4 README 步骤已写"以 CLI 实际为准"的分支处理 ✔
