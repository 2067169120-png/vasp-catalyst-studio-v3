# Feature comparison with published tools / 与已发刊工具的功能对比

This comparison describes the **V4.0 working tree** of VASP Catalyst Studio
(`vcstudio`). It compares product surfaces, not numerical equivalence or
scientific validation. External-tool capabilities follow the cited papers;
"Partial" means that a capability exists with material limits stated in the
cell or in the honest-gaps section.

| Capability | vcstudio V4.0 | VASPKIT [1] | qvasp [2] | ALKEMIE [3] | atomate2 + custodian [4] | AiiDA / AiiDAlab [5,6] | pyiron [7] | VASPilot [8] | AutoDFT [9] |
|---|---|---|---|---|---|---|---|---|---|
| Form factor | Windows-first desktop workbench; single-file EXE build available | Interactive terminal | Terminal | Desktop GUI platform | Python library | Server + Jupyter web GUI | Python/Jupyter library | Flask web UI | CLI/agents |
| Distribution boundary | EXE does not require a separate Python install; WebView2 and all licensed/external scientific programs remain user/system dependencies | Install required | Install required | Install required | Install required | DB + daemon | Install required | Server deployment | Install required |
| Input generation | VASP four-file path with user INCAR sovereignty; bounded CP2K/Gaussian/CASTEP file adapters | VASP | VASP | VASP-oriented | Recipe/calculator based | Plugin based | Generic calculator parameters | VASP, LLM-assisted | VASP, LLM-assisted |
| Cluster submission | PBS + Slurm, SSH and ProxyJump, explicit host-key gates | Scripts only | Scripts only | Yes | Via FireWorks/jobflow-remote | Daemon | Queue adapters | Slurm | Yes |
| Monitoring + failure classification | 18 job outcomes + 16 VASP internal signatures mapped to 4 target states | No | No | Partial | custodian handlers | Process states | Database status | LLM parses errors | LLM monitor |
| Recovery | Rule-based, at most three rounds; INCAR frozen; otherwise `NEEDS_HUMAN` | No | No | No | Rule based | Plugin dependent | No | LLM driven | Agent driven |
| Network boundary | Deterministic generation/parsing/analysis/report core has no mandatory cloud service. SSH needs network; external LLM and first-time DECIMER model retrieval are optional, explicit network paths | Local | Local | Local | Local/cluster | Local/cluster services | Local/cluster | Online model path | Online model path |
| Structure clash interception | Covalent-radius molecule-slab preflight | No | No | No | No | No | No | No | No |
| Convergence and electronic analysis | E0/ΔE/\|F\|max traces; DOS and registered task-result views | Partial | Partial | Partial | Via libraries | Via plugins/provenance | Via notebooks | On demand | Agent post-processing |
| Methods and citation generation | Bilingual Methods/BibTeX derived from frozen real inputs | No | No | No | No | No | No | No | No |
| Report publishing | Revisioned ReportSpec → Snapshot → ValidationResult → model/manifest chain; HTML/DOCX/PDF capability-gated | No | No | Partial | No | No | No | No | No |
| Provenance | Per-job SHA manifests; file-backed campaign DAG/gates/ledger; report contract sidecars and revision history | No | No | Database | Output documents | **Queryable full DAG** | SQL + HDF5 | Calculation database | Logs |
| Multi-code support | **Partial and asymmetric**: VASP is the deepest path; CP2K/Gaussian/CASTEP expose bounded adapters, not code-agnostic parity | No | No | Partial | Many calculators | Plugin ecosystem | Code-agnostic framework | Extensible | VASP focused |
| LLM layer | Optional interpretation, paper extraction and assistant chat; external use defaults off and cannot set scientific state | No | No | No | No | No | No | In workflow | In workflow |
| Automated verification | 2026-08-11 local V4 snapshot: 3336 passed, 5 skipped; current counts are always the latest CI/pytest output | n/a | n/a | n/a | CI | CI | CI | Open source | n/a |

## Honest gaps / 诚实短板

- **Not an AiiDA-equivalent provenance database.** The campaign DAG and report
  evidence chain are inspectable files, but they do not provide a queryable,
  database-backed, all-code provenance graph.
- **Engine depth is intentionally asymmetric.** The 23-task catalog and most
  scientific gates are VASP-first. CP2K, Gaussian and CASTEP adapters are
  bounded file contracts; their results are not assumed equivalent to VASP or
  to one another.
- **No general workflow DSL.** File-backed campaign templates cover bounded
  task graphs and three-state validation gates, not arbitrary code-agnostic
  orchestration.
- **Windows-first desktop experience.** The Python core and CI are
  cross-platform, while the packaged desktop product is Windows-centric.
- **External dependencies remain external.** VASP/CP2K/Gaussian/CASTEP,
  licenses, POTCAR/BASIS/POTENTIAL data, Multiwfn, VMD, Origin and POV-Ray are
  not granted or silently bundled by the application.
- **Scientific benchmark remains pending.** The Li-S adsorption MAE protocol
  is implemented, but like-for-like real cluster data have not been backfilled;
  see [validation.md](validation.md).

## Reporting and accessibility boundary

Artifact generation and scientific publication eligibility are separate axes.
HTML is always renderable; DOCX and PDF are selectable only after their actual
renderer/assets pass capability probing. HTML and DOCX expose semantic
foundations but remain **conditional** on meaningful figure descriptions and
browser/Word plus human review. The current ReportLab PDF is visual and
searchable but untagged, with no structure tree or image alternative text:
`tagged=false` and `pdf_ua=false`. It must not be described as PDF/UA or as an
accessible substitute for HTML/DOCX. The authoritative contract is
[report-state-contract.md](report-state-contract.md).

## Energy and AI boundaries

Raw DFT totals and uncorrected adsorption energies use `E0`. Free-energy paths
may add explicit ZPE/thermal/entropy corrections only when qualified frequency
evidence, temperature, low-frequency treatment, imaginary-mode gates and the
correction fingerprint are recorded. Missing frequency evidence is never
silently inferred.

External LLM calls are optional and default off. They may assist with bounded
interpretation, paper-method/data extraction and conversational navigation,
but model text is not a numerical fact, cannot set `accepted`, and cannot
bypass submission, budget, method-consistency or report-publication gates.

## Differentiation summary / 差异化定位

V4 combines a project-context desktop shell, VASP-first bounded automation,
SSH/PBS/Slurm operations, pre-submission geometry checks, a registry-driven
analysis workbench and revisioned evidence-bound reporting. The deterministic
core remains usable without a cloud model while optional network features are
shown explicitly instead of being folded into the scientific fact chain.

## References

1. V. Wang et al., *VASPKIT: A user-friendly interface facilitating
   high-throughput computing and analysis using VASP code*,
   Comput. Phys. Commun. **267**, 108033 (2021). DOI 10.1016/j.cpc.2021.108033
2. *qvasp: A flexible toolkit for VASP users*, Comput. Phys. Commun. **257**,
   107535 (2020).
3. G. Wang et al., *ALKEMIE: An intelligent computational platform for
   accelerating materials discovery and design*, Comput. Mater. Sci. **186**,
   110064 (2021). DOI 10.1016/j.commatsci.2020.110064
4. A. Ganose et al., *Atomate2: modular workflows for materials science*,
   Digital Discovery **4**, 1944–1973 (2025). DOI 10.1039/D5DD00019J
5. S. P. Huber et al., *AiiDA 1.0, a scalable computational infrastructure for
   automated reproducible workflows and data provenance*, Sci. Data **7**, 300
   (2020). DOI 10.1038/s41597-020-00638-4
6. A. V. Yakutovich et al., *AiiDAlab – an ecosystem for developing, executing,
   and sharing scientific workflows*, Digital Discovery (RSC).
7. *pyiron workflow framework*, npj Comput. Mater. (2024).
   DOI 10.1038/s41524-024-01441-0
8. J. Liu et al., *VASPilot: MCP-facilitated multi-agent intelligence for
   autonomous VASP simulations*, arXiv:2508.07035 (2025).
9. *AutoDFT: closed-loop LLM-agent framework for DFT with VASPBench*,
   arXiv:2605.26179.
10. J. H. Montoya, K. A. Persson, *A high-throughput framework for determining
    adsorption energies on solid surfaces*, npj Comput. Mater. **3**, 14
    (2017). DOI 10.1038/s41524-017-0017-z — validation-methodology reference
    adopted by `docs/validation.md`.
