# Contributing to VASP Catalyst Studio

Thank you for considering a contribution. This project is a lightweight VASP
automation desktop platform; contributions of bug reports, tests, documentation
and features are all welcome.

## Development setup

```bash
git clone https://github.com/vasp-catalyst-studio/vcstudio
cd vcstudio
pip install -e .[dev]
```

Runtime dependencies are intentionally minimal (PyYAML, paramiko, keyring).
The GUI uses pywebview and is packaged as a single Windows EXE; the core
library and test suite run on Linux as well.

## Running the tests

```bash
python -m pytest
```

The full suite must pass before any commit. Optional integration smokes are
gated behind environment flags (e.g. `VCS_ORIGIN_SMOKE=1` for OriginLab
rendering) and are skipped by default.

## Development conventions

- **TDD**: write a failing test first, then the minimal implementation, then
  refactor. Every behavior change ships with a test.
- **Comments in Chinese, identifiers in English** — this is the established
  house style; keep new code consistent with it.
- **Narrow commits**: commit with explicit pathspecs
  (`git commit -- <files>`); keep each commit to one logical change.
- **Red lines**: never mutate a user's methodology INCAR keys
  (ENCUT / functional / IVDW / ISPIN) on a per-job basis; credentials go to
  the OS keyring only, never to YAML/plain text; no runtime CDN dependencies
  (all frontend assets are vendored offline).

## Reporting bugs

Open an issue on the tracker and include:

1. OS and Python version (and whether you run the EXE or `pip install -e .`),
2. exact steps to reproduce,
3. the full error output or log excerpt,
4. for cluster problems: scheduler type (PBS/Slurm) and a sanitized job script.

Do **not** attach POTCAR contents (they are licensed material) — TITEL lines
are enough.

## Submitting changes

1. Fork / branch from the default branch.
2. Add tests, keep `python -m pytest` green.
3. Follow the commit-message style visible in `git log` (conventional
   prefixes: `feat:`, `fix:`, `docs:`, `chore:`, `ci:`).
4. Open a pull request describing what changed and why.

## Seeking support

For usage questions, open a discussion or an issue with the `question` label.
