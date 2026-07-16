# Quickstart — generate a full VASP input set offline / 五分钟离线全链体验

This walkthrough exercises the complete `vcs gen` pipeline (POSCAR + your
INCAR → INCAR/KPOINTS/POSCAR/POTCAR + `job.yaml` provenance) **without a
cluster and without licensed pseudopotentials**, using a graphene demo cell.

> **POTCAR copyright warning / 赝势版权警告**
> VASP PAW pseudopotentials are licensed material and are **not** included in
> this repository. Step 1 below creates a *fake* demo library so the pipeline
> can run end-to-end; its output is **not valid for any real calculation**.
> For production, point `--lib-root` (or the `potcar` key in `config.yaml`)
> at your institution's licensed PAW library instead.

## 1. Create the fake demo POTCAR library / 生成演示假赝势库

```bash
python examples/make_demo_potcar_lib.py demo_lib
```

## 2. Generate the input set / 生成四件套

```bash
vcs gen --poscar examples/quickstart/POSCAR --incar examples/quickstart/INCAR --calc-type slab --lib-root demo_lib -o results/demo01
```

(One line — no shell line-continuation needed. If you installed without
`pip install -e .`, use `python -m vcstudio.cli.main gen ...` equivalently.)

## 3. Inspect the output / 检查产物

`results/demo01/` now contains:

| File | Origin |
|---|---|
| `INCAR` | your INCAR, validated and completed (user keys are never overwritten) |
| `KPOINTS` | auto-recommended for a 2D slab (kz = 1) |
| `POSCAR` | copied input structure |
| `POTCAR` | concatenated from the library (fake here — see warning above) |
| `job.yaml` | provenance manifest: sha256 of inputs, element list, state machine |

The job is also registered in the local ledger, so it appears in the GUI's
jobs page (`vcs gui` or the packaged EXE) with state `CREATED`.

## 4. Where to go next / 下一步

- Submit to a PBS/Slurm cluster from the GUI's cluster page (profiles,
  preflight checks and bounded self-healing are built in).
- See `docs/user-guide-en.md` (English) or `使用说明.md` (中文) for the full
  workflow: generate → submit → monitor → diagnose → ΔE analysis → report.
