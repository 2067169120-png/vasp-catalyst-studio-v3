# Publication Blocker Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the verified P1/P2 correctness, security, packaging, and publication blockers while preserving the current user worktree.

**Architecture:** Four file-disjoint implementation domains run in parallel. Each domain owns its production and test files, follows TDD, and returns a focused diff for main-agent integration and review.

**Tech Stack:** Python 3.10+, pytest, PyYAML, Paramiko, setuptools, GitHub Actions, PyInstaller, vanilla JavaScript.

---

### Task 1: Atomic job operations, provenance, and contained fetches

**Files:**
- Create: `vcstudio/shared/filelock.py`
- Modify: `vcstudio/shared/manifest.py`
- Modify: `vcstudio/cluster/submitter.py`
- Test: `tests/test_manifest.py`
- Test: `tests/test_submitter.py`
- Test: `tests/test_fetch_results.py`

- [ ] Add a failing concurrency test that starts two `continue_from_contcar` calls at a barrier and asserts the scheduler submit command is executed once.
- [ ] Run that test and confirm both calls currently pass the terminal-state check and submit twice.
- [ ] Add a failing manifest test asserting initial hashes for `POSCAR`, `INCAR`, `KPOINTS`, and `POTCAR`, plus per-attempt input and script hashes.
- [ ] Add failing fetch tests for `../x`, `/etc/passwd`, `C:\\Windows\\x`, and nested separators.
- [ ] Implement a stdlib cross-platform file lock with bounded acquisition, unique manifest temp files, `RECOVERING` operation state, full input snapshot helpers, and filename containment.
- [ ] Record the operation before remote mutation, close it only after the scheduler job id is durable, and preserve failure evidence.
- [ ] Run the focused tests until green, then run all manifest/submitter/batch tests.

### Task 2: LLM and SSH trust boundaries

**Files:**
- Modify: `vcstudio/shared/config.py`
- Modify: `vcstudio/project/ai_analysis.py`
- Modify: `vcstudio/cluster/ssh_test.py`
- Modify: `vcstudio/cluster/connection.py`
- Modify: `vcstudio/gui_web/api.py`
- Modify: `vcstudio/gui_web/assets/cluster.js`
- Modify: `vcstudio/gui_web/assets/jobs.js`
- Test: `tests/test_config.py`
- Test: `tests/test_ai_analysis.py`
- Test: `tests/test_ssh_test.py`
- Test: `tests/test_connection.py`
- Test: `tests/test_web_api.py`

- [ ] Add failing tests proving cwd YAML cannot set `llm`, network permission, automation, or SSH trust fields.
- [ ] Add failing tests rejecting non-loopback HTTP endpoints before transport invocation and rejecting unsafe redirects.
- [ ] Add failing SSH tests requiring algorithm plus SHA256 fingerprint in the unknown-host response and exact fingerprint on approval.
- [ ] Implement trusted user/project configuration merging, endpoint validation, redirect protection, and fingerprint-bound host-key policies for jump and target hosts.
- [ ] Update Web API and JavaScript confirmation flow to return the displayed fingerprint on retry.
- [ ] Run focused configuration, AI, SSH, connection, and Web API tests until green.

### Task 3: Distribution and CI integrity

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `vcstudio/gui_web/__main__.py`
- Modify: `packaging/build_exe.py`
- Create: `THIRD_PARTY_NOTICES.md`
- Test: `tests/test_packaging_meta.py`
- Test: `tests/test_ci_config.py`
- Test: `tests/test_web_resources.py`

- [ ] Add a failing archive test that builds a wheel and asserts `gui_web/assets/index.html`, CSS, JavaScript, fonts, and vendor files are present.
- [ ] Add a failing entry-point test asserting `vcs-gui` targets the Web UI and a legacy entry point remains available.
- [ ] Strengthen the CI configuration test so `package-smoke` must invoke `packaging/build_exe.py` and verify an EXE artifact.
- [ ] Declare recursive package data, correct entry points, add a noninteractive Web resource smoke, and make CI build/install/check wheel and EXE artifacts.
- [ ] Add complete third-party component attribution and ensure distribution tests require it.
- [ ] Build wheel/sdist, run Twine checks, install the wheel into a temporary target, and verify resource existence.

### Task 4: Scientific semantics, locale portability, and truthful documentation

**Files:**
- Modify: `vcstudio/project/freeenergy.py`
- Modify: `vcstudio/project/report_full.py`
- Modify: `tests/test_freeenergy.py`
- Modify: `tests/test_thermo.py`
- Modify: `tests/fixtures/vasp_outputs/*/OUTCAR`
- Modify: `paper/paper.md`
- Modify: `README.md`
- Modify: `docs/user-guide-en.md`
- Modify: `使用说明.md`
- Move: `install.cmd` to `tools/install-claude-code.cmd`

- [ ] Run parser-equivalence tests without `PYTHONUTF8` and record the current locale-dependent failures.
- [ ] Convert synthetic OUTCAR prose to ASCII and rerun the tests to green.
- [ ] Add failing free-energy tests showing that precipitate/reference corrections and lithium chemical-potential corrections are currently omitted.
- [ ] Introduce explicit system and molecular correction inputs, completeness metadata, and non-misleading result labels while preserving the electronic-energy default.
- [ ] Update report rendering to distinguish electronic, partial vibrational, and fully corrected modes.
- [ ] Correct recovery, provenance, zero-install, test-count, ΔG, and U_L claims; retain explicit pending validation and author metadata markers.
- [ ] Move the unrelated installer under `tools/` with an unambiguous name.
- [ ] Run free-energy, thermo, report, parser, documentation, and packaging tests.

### Task 5: Integration and review

**Files:**
- Review all files changed by Tasks 1-4.

- [ ] Run a spec-compliance review for each domain and return any missing or extra behavior to its implementer.
- [ ] Run a separate code-quality review after spec compliance is clean.
- [ ] Run full pytest in the default environment and with UTF-8 enabled.
- [ ] Run coverage, Ruff, compileall, `git diff --check`, wheel/sdist build, Twine, isolated wheel resource smoke, and targeted concurrency/security tests.
- [ ] Report remaining external blockers: real benchmark data, author affiliation/ORCID/funding, DOI/tag/release, and live cluster/EXE manual acceptance.

