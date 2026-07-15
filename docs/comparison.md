# Feature comparison with published tools / 与已发刊工具的功能对比

Peer-reviewed or formally preprinted tools in the VASP-automation space,
compared feature-by-feature with VASP Catalyst Studio (vcstudio). Sources are
the tools' own papers (citations at the bottom). "Partial" means the
capability exists but with material caveats noted inline.

| Capability | vcstudio | VASPKIT [1] | qvasp [2] | ALKEMIE [3] | atomate2 + custodian [4] | AiiDA / AiiDAlab [5,6] | pyiron [7] | VASPilot [8] | AutoDFT [9] |
|---|---|---|---|---|---|---|---|---|---|
| Form factor | Desktop GUI, single-file EXE | Interactive terminal | Terminal | Desktop GUI platform | Python library | Server + Jupyter web GUI | Python/Jupyter library | Flask web UI | CLI/agents |
| Zero-install distribution | Yes (one EXE) | No (install) | No | No | No | No (DB + daemon) | No | No | No |
| Input generation (INCAR/KPOINTS/POTCAR) | Yes; user INCAR keys never overwritten | Yes | Yes | Yes | Yes (recipe sets) | Yes (plugins) | Yes (generic params) | Yes (LLM-drafted) | Yes (LLM-drafted) |
| Cluster submission | PBS + Slurm, double-hop SSH jump host | No (bash scripts) | No | Yes | Via FireWorks/jobflow-remote | Yes (daemon) | Yes (queue option) | Slurm | Yes |
| Job monitoring + failure classification | Yes: 12+11 failure classes → 4 terminal states | No | No | Partial | custodian error handlers | Yes (process states) | Job status in DB | LLM parses errors | LLM dual-path monitor |
| Self-healing | Rule-based, **bounded (max 3 rounds), INCAR frozen**, else NEEDS_HUMAN | No | No | No | custodian rule-based | Plugin-dependent | No | LLM-driven retry | LLM recovery agent |
| Works fully offline (no external services) | Yes (deterministic core is zero-token; LLM layer optional) | Yes | Yes | Yes | Yes | Yes | Yes | No (LLM inference required) | No (LLM required) |
| Pre-submission structure clash detection | Yes (molecule–slab gap analysis, covalent-radius clash scan) | No | No | No | No | No | No | No | No |
| Convergence visualization (E0/ΔE/\|F\|max per ionic step) | Yes (GUI chart) | Partial (grep-style extraction) | Partial | Partial | Via external plotting | Via provenance queries | Via notebooks | Plots on demand | Postprocessing agent |
| DOS plotting | Yes (publication SVG) | Yes (rich) | Yes | Yes | Yes (via pymatgen) | Yes (plugins) | Yes | Yes | Yes |
| Auto-generated Methods paragraph + BibTeX from real inputs | Yes (bilingual; GGA tag overrides POTCAR flavor wording) | No | No | No | No | No | No | No | No |
| Word/report export | Yes (Chinese journal format + charts) | No | No | Partial | No | No | No | No | No |
| Data provenance | job.yaml manifests (sha256 of inputs, state history) | No | No | Database | jobflow output docs | **Full DAG provenance** (strongest) | SQL + HDF5 | Calc-ID database | Logs |
| Multi-code support (beyond VASP) | No (VASP only) | No | No | Partial | Yes (many calculators) | Yes (plugin ecosystem) | Yes (code-agnostic) | Extensible via MCP | No |
| LLM agent layer | Analysis-only (deterministic core) | No | No | No | No | No | No | Yes (CrewAI multi-agent) | Yes (7-agent closed loop) |
| Test suite + CI | 424 tests, GitHub Actions matrix | n/a (closed dev) | n/a | n/a | Yes | Yes | Yes | Open source | n/a |

## Honest gaps / 诚实短板

- **No provenance DAG**: AiiDA's queryable full-provenance graph is strictly
  stronger than vcstudio's per-job `job.yaml` manifests.
- **VASP-only**: atomate2/pyiron/AiiDA support many codes; vcstudio does not.
- **Windows-first GUI**: the packaged EXE targets Windows; the core library
  and tests run cross-platform, but the desktop experience is Windows-centric.
- **No workflow language**: complex multi-step campaigns (e.g. phonons →
  thermal properties) are out of scope; vcstudio focuses on the
  generate→submit→monitor→analyze→report loop for catalysis screening.

## Differentiation summary / 差异化定位

vcstudio occupies a combination no published tool covers: a **zero-install
desktop EXE** driving **double-hop PBS clusters** with a **deterministic,
zero-token core** (LLM only in the optional analysis layer), plus
**pre-submission geometry clash interception** and **Methods-paragraph
generation guaranteed consistent with the actual INCAR/KPOINTS/POTCAR** —
aimed at graduate-student catalysis workflows where data must not leave the
local machine and cluster time is too expensive for avoidable resubmissions.

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
    (CE27 benchmark, MAE reporting) adopted by `docs/validation.md`.
