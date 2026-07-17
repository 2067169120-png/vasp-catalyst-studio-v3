# Publication Blocker Remediation Design

## Goal

Close the highest-risk review findings without inventing scientific results or author metadata. The repaired tree must prevent duplicate remote submissions, preserve attempt-level provenance, reject unsafe trust boundaries, ship a usable Python package, and describe the scientific model exactly as implemented.

## Scope

This change set covers four independent domains:

1. Job transaction safety, fetch-path containment, and immutable input provenance.
2. LLM endpoint/configuration safety and verifiable SSH host-key approval.
3. Python wheel, Windows EXE CI, entry-point, and third-party notice correctness.
4. Free-energy correction semantics, Windows parser-fixture portability, and publication claim alignment.

Real VASP benchmark data, author affiliation/ORCID, funding statements, repository DOI, and release tags are explicitly out of scope because they require facts not present in the workspace.

## Architecture

### Job operations

Every mutating job operation obtains a cross-thread and cross-process lock scoped to the job directory. Continuation writes a durable `RECOVERING` operation record before any remote side effect. Successful submission closes the operation and records a complete attempt snapshot; failure preserves enough state for human reconciliation instead of silently returning to the old terminal state.

Manifest writes use unique same-directory temporary files followed by `os.replace`. Initial and attempt records hash POSCAR, INCAR, KPOINTS, POTCAR, and the submitted script when present. Fetch requests accept filenames only; both remote POSIX paths and local resolved paths must remain inside their job roots.

### External trust boundaries

Automatic configuration loading treats project-local YAML as scientific configuration only. Credentials, LLM endpoint, network permission, UI automation, and SSH policy come from the explicit environment override or the user configuration directory. LLM endpoints must be HTTPS, except loopback HTTP used by local gateways. Redirects that change origin or downgrade HTTPS are rejected before forwarding authorization headers.

Unknown SSH keys are returned to the UI with host, port, algorithm, and SHA256 fingerprint. Approval is bound to that fingerprint; a boolean “trust anything next time” is insufficient. Jump-host and target-host keys are approved and persisted independently.

### Distribution

Setuptools package data includes the complete `gui_web/assets` tree. The `vcs-gui` entry point launches the Web UI and a separate legacy entry point launches Tkinter. CI builds wheel/sdist, installs the wheel into an isolated directory, verifies the bundled index and vendor assets, and performs a real Windows PyInstaller build smoke. Third-party notices travel with source and binary distributions.

### Scientific semantics

Electronic-energy paths and thermally corrected paths are distinct result modes. A fully corrected path requires correction data for every adsorbed state and every molecular/precipitate reference participating in the reaction, including the species used to derive the lithium chemical potential. Partial correction is reported as partial and cannot be labelled publication-grade free energy.

Synthetic VASP OUTCAR fixtures remain ASCII, matching actual VASP text output and avoiding locale-dependent ASE failures. Paper and user documentation distinguish automatic frozen-INCAR continuation from explicitly confirmed tuning, describe the actual provenance fields, and avoid hard-coded test counts.

## Error handling

- Lock acquisition has a bounded timeout and a clear “operation already in progress” error.
- A crash after scheduler submission leaves an operation record that identifies possible orphan work.
- Unsafe filenames and endpoints fail before filesystem, network, or credential side effects.
- Host-key mismatch is always a hard failure; it is never handled as first-time trust.
- Missing correction data produces an explicit incomplete-correction error or an electronic-energy result, never a silently mixed ΔG value.
- Package smoke failures identify the missing resource or entry point.

## Test strategy

Each domain follows red-green-refactor:

- Concurrent continuation tests use a barrier and assert one scheduler submission.
- Provenance tests mutate inputs between attempts and assert distinct full hash snapshots.
- Path tests reject POSIX, Windows, absolute, and parent traversal.
- LLM tests reject non-loopback HTTP and project-local network overrides.
- SSH tests assert displayed fingerprint and exact-fingerprint approval.
- Wheel tests build and inspect the archive, then import resources from an isolated installation.
- Free-energy tests cover complete, partial, and missing molecular corrections.
- Parser-equivalence tests run under the default Windows locale without `PYTHONUTF8`.

## Acceptance criteria

- All new regression tests are observed failing before implementation and passing afterwards.
- Full pytest passes under the normal process environment and UTF-8 mode.
- Ruff and `git diff --check` pass.
- Wheel/sdist build and Twine checks pass; the wheel contains Web assets.
- The Windows package job invokes PyInstaller rather than import-only smoke.
- No documentation claims contradict executable behavior.

