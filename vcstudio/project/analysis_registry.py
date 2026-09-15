"""Declarative analysis registry and strict Phase D request contract.

The registry describes *available* analysis capabilities; it never claims that
one project's evidence is complete.  Project data, scientific gates and
results are resolved later by the API from server-owned manifests and parsers.

Browser requests may select bounded presentation and comparison options.  They
cannot submit results, paths, method fingerprints, evidence, or arbitrary
parameters.  Missing values are either shown explicitly or excluded through a
complete-case view; this contract deliberately has no imputation mode.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


CATALOG_SCHEMA = "vcstudio.analysis-catalog/v1"
SPEC_SCHEMA = "vcstudio.analysis-spec/v1"
VIEW_TEMPLATE_SCHEMA = "vcstudio.analysis-view-template/v1"

DATA_MODES = ("stable", "all")
MISSING_POLICIES = ("show_missing", "complete_cases")
SORT_DIRECTIONS = ("asc", "desc")
CAPABILITY_STATUSES = (
    "available", "missing_prerequisite", "mode_mismatch",
    "not_implemented", "unavailable",
)
ANALYSIS_CATEGORIES = (
    "energy-stability",
    "thermodynamics-kinetics",
    "electronic-structure",
    "charge-wavefunction",
    "comparison",
    "custom",
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PATH_RE = re.compile(
    r"(?i)(?:^|[\s'\"])(?:[A-Z]:[\\/]|\\\\|//|/[^/\s]|~[\\/]|file:)")
_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r"|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)"
)


class AnalysisRequestError(ValueError):
    """An untrusted analysis-workbench request violates the public contract."""


_ANALYSES = (
    {
        "id": "adsorption-energy",
        "version": "1",
        "category": "energy-stability",
        "label_zh": "吸附能与稳定性",
        "label_en": "Adsorption energy and stability",
        "description_zh": "查看全部构型或每个物种的最稳/近简并构型。",
        "route": "analyze-energy",
        "page": "analysis-workbench",
        "task_keys": ["adsorption_project", "relax", "static"],
        "data_modes": ["stable", "all"],
        "parameters": [
            "precision", "near_degenerate_eV", "missing_policy", "sort",
        ],
        "sort_keys": ["species", "energy", "name", "state"],
        "evidence": ["reference_state", "method_consistency", "energy_source"],
        "outputs": ["configuration_table", "near_degenerate_groups", "adsorption_bar"],
        "report_sections": ["adsorption_table", "key_findings", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "free-energy-path",
        "version": "1",
        "category": "thermodynamics-kinetics",
        "label_zh": "自由能与反应路径",
        "label_en": "Free-energy and reaction pathway",
        "description_zh": "从 canonical 催化对象投影构建反应图、热化学账本、条件派生修订与冻结网络。",
        "route": "analyze-thermo",
        "page": "analysis-workbench",
        "task_keys": ["freq", "neb", "dimer", "aimd"],
        "data_modes": ["stable"],
        "parameters": ["precision", "conditions"],
        "sort_keys": [],
        "evidence": [
            "canonical_domain_projection", "reaction_topology", "thermochemistry",
            "frequency_mode", "condition_set", "applicability", "reference",
        ],
        "outputs": [
            "reaction_graph", "thermochemistry_ledger",
            "condition_derived_revision", "parameter_sensitivity",
            "frozen_reaction_network",
        ],
        "report_sections": [
            "reaction_map", "thermochemistry_ledger", "figures",
            "methods", "limitations",
        ],
        "supports_baseline": False,
        "supports_sensitivity": False,
        "supports_parameter_sensitivity": True,
    },
    {
        "id": "neb-path",
        "version": "1",
        "category": "thermodynamics-kinetics",
        "label_zh": "NEB 路径与能垒诊断",
        "label_en": "NEB path and barrier diagnostics",
        "description_zh": "逐 image 审计反应坐标、能量、力、收敛与端点证据；证据不全时不报能垒。",
        "route": "analyze-thermo",
        "page": "analysis-workbench",
        "task_keys": ["neb"],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": [
            "ordered_images", "endpoint_identity", "method_consistency",
            "energy_units", "electronic_convergence", "ionic_convergence",
        ],
        "outputs": ["neb_image_table", "neb_profile", "barrier_gate", "path_quality"],
        "report_sections": ["figures", "methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "convergence-scan",
        "version": "1",
        "category": "energy-stability",
        "label_zh": "参数收敛扫描",
        "label_en": "Parameter convergence scans",
        "description_zh": "审计 ENCUT、k 网格、真空和 slab 层厚原始点、每原子差、平台与阈值敏感性。",
        "route": "analyze-custom",
        "page": "analysis-workbench",
        "task_keys": [
            "conv_scan", "conv_encut", "conv_kmesh", "conv_vacuum",
            "conv_thickness",
        ],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": [
            "series_manifest", "method_consistency", "energy_source",
            "atom_denominator", "declared_threshold",
        ],
        "outputs": [
            "convergence_points", "platform_interval", "threshold_sensitivity",
            "convergence_curve",
        ],
        "report_sections": ["figures", "methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "aimd-diagnostics",
        "version": "1",
        "category": "thermodynamics-kinetics",
        "label_zh": "AIMD 轨迹诊断",
        "label_en": "AIMD trajectory diagnostics",
        "description_zh": "显示温度、总能漂移、采样长度和结构位移指标；不把短轨迹称为热稳定证明。",
        "route": "analyze-thermo",
        "page": "analysis-workbench",
        "task_keys": ["aimd"],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": [
            "aimd_incar", "oszicar_md_samples", "xdatcar_frames",
            "method_consistency", "sampling_denominator",
        ],
        "outputs": ["aimd_samples", "drift_diagnostics", "structure_diagnostics"],
        "report_sections": ["figures", "methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "kinetic-dashboard",
        "version": "1",
        "category": "thermodynamics-kinetics",
        "label_zh": "微观动力学 Dashboard",
        "label_en": "Microkinetic Dashboard",
        "description_zh": "审计冻结反应网络，并只读展示外部 CatMAP 的服务端归一化结果。",
        "route": "analyze-kinetics",
        "page": "analysis-workbench",
        "task_keys": ["neb", "dimer", "freq"],
        "data_modes": ["stable"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": [
            "frozen_reaction_projection", "element_charge_site_balance",
            "forward_reverse_barriers", "detailed_balance", "standard_state",
            "method_consistency", "prefactor_bep_scaling_provenance", "uncertainty",
        ],
        "outputs": [
            "turnover_frequency", "coverage", "selectivity", "rate_control",
            "selectivity_control", "reaction_order", "apparent_activation_energy",
            "free_energy_diagram", "convergence", "sensitivity",
        ],
        "report_sections": ["methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "task-results",
        "version": "1",
        "category": "custom",
        "label_zh": "任务解析与结果工具",
        "label_en": "Task results and parsers",
        "description_zh": "按任务注册表调用真实解析器，并保留文件身份与能力边界。",
        "route": "analyze-custom",
        "page": "analysis-workbench",
        "task_keys": [],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": ["job_manifest", "output_hashes", "parser_version"],
        "outputs": ["task_summary", "trace_report"],
        "report_sections": ["methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "electronic-structure",
        "version": "1",
        "category": "electronic-structure",
        "label_zh": "电子结构",
        "label_en": "Electronic structure",
        "description_zh": "DOS/PDOS、能带和功函数的可追溯结果入口。",
        "route": "analyze-electronic",
        "page": "analysis-workbench",
        "task_keys": ["dos_pdos", "bands", "workfunction"],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": ["vasprun", "eigenval", "locpot", "fermi_energy"],
        "outputs": ["pdos", "band_structure", "work_function"],
        "report_sections": ["figures", "methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "charge-wavefunction",
        "version": "1",
        "category": "charge-wavefunction",
        "label_zh": "电荷与波函数",
        "label_en": "Charge and wavefunction",
        "description_zh": "Bader、差分电荷和 ELF 的产物与定量解析入口。",
        "route": "analyze-charge",
        "page": "analysis-workbench",
        "task_keys": ["bader", "chgdiff", "elf"],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": ["charge_files", "grid_identity", "zval"],
        "outputs": ["bader_table", "charge_profile", "artifact_inventory"],
        "report_sections": ["figures", "methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
    {
        "id": "multi-project-comparison",
        "version": "1",
        "category": "comparison",
        "label_zh": "多项目比较",
        "label_en": "Multi-project comparison",
        "description_zh": "按显式基准、分母和方法口径比较吸附能与自由能。",
        "route": "analyze-comparison",
        "page": "analysis-workbench",
        "task_keys": ["adsorption_project"],
        "data_modes": ["stable"],
        "parameters": [
            "precision", "near_degenerate_eV", "baseline_project_id",
            "missing_policy", "sensitivity_deadbands_eV",
        ],
        "sort_keys": [],
        "evidence": [
            "reference_state", "method_matrix", "reaction_path",
            "thermochemistry", "solvation",
        ],
        "outputs": [
            "comparison_matrix", "method_matrix", "baseline_deltas",
            "sensitivity",
        ],
        "report_sections": [
            "comparison_table", "key_findings", "figures", "methods", "limitations",
        ],
        "supports_baseline": True,
        "supports_sensitivity": True,
    },
    {
        "id": "property-calculators",
        "version": "1",
        "category": "energy-stability",
        "label_zh": "性质计算器",
        "label_en": "Property calculators",
        "description_zh": "表面能、形成/结合能和溶剂化能的显式操作数计算器。",
        "route": "analyze-properties",
        "page": "analysis-workbench",
        "task_keys": ["surface_energy", "formation_binding", "vaspsol"],
        "data_modes": ["all"],
        "parameters": ["precision"],
        "sort_keys": [],
        "evidence": ["operand_identity", "method_consistency", "formula"],
        "outputs": ["surface_energy", "formation_energy", "binding_energy", "solvation_energy"],
        "report_sections": ["key_findings", "methods", "limitations"],
        "supports_baseline": False,
        "supports_sensitivity": False,
    },
)
_ANALYSIS_BY_ID = {record["id"]: record for record in _ANALYSES}

_NEXT_ACTIONS = {
    "adsorption-energy": "Complete project member energies and reference evidence, then refresh.",
    "free-energy-path": (
        "Bind a canonical reaction-domain projection, or use the explicit Li-S "
        "legacy mode with complete reaction-path evidence."),
    "neb-path": "Complete a registered NEB job with image, endpoint, method and convergence evidence.",
    "convergence-scan": "Complete one explicit registered convergence series and its atom denominators.",
    "aimd-diagnostics": "Complete a registered AIMD job with OSZICAR and XDATCAR evidence.",
    "kinetic-dashboard": (
        "Provide a frozen reaction/thermochemistry projection, configure an external "
        "CatMAP adapter, and import a hash-matched diagnostic result."),
    "task-results": "Complete a registered project member or descendant job, then refresh.",
    "electronic-structure": "Create and complete a DOS/PDOS, bands, or work-function descendant job.",
    "charge-wavefunction": "Create and complete a Bader or charge-difference descendant job.",
    "multi-project-comparison": "Register at least two comparable projects with method evidence.",
    "property-calculators": "Create a manifest-bound surface, formation/binding, or VASPsol operand set.",
}

_VIEW_TEMPLATES = (
    {
        "id": "stable-screen",
        "label_zh": "最稳构型筛选",
        "label_en": "Stable-configuration screen",
        "analysis_id": "adsorption-energy",
        "values": {
            "data_mode": "stable",
            "near_degenerate_eV": 0.15,
            "precision": 4,
            "missing_policy": "show_missing",
            "sort": {"key": "species", "direction": "asc"},
        },
    },
    {
        "id": "all-configurations",
        "label_zh": "全部构型审阅",
        "label_en": "All-configuration review",
        "analysis_id": "adsorption-energy",
        "values": {
            "data_mode": "all",
            "near_degenerate_eV": 0.15,
            "precision": 6,
            "missing_policy": "show_missing",
            "sort": {"key": "species", "direction": "asc"},
        },
    },
    {
        "id": "method-audit",
        "label_zh": "方法证据审计",
        "label_en": "Method-evidence audit",
        "analysis_id": "multi-project-comparison",
        "values": {
            "data_mode": "stable",
            "near_degenerate_eV": 0.15,
            "precision": 6,
            "missing_policy": "show_missing",
            "sensitivity_deadbands_eV": [0.05, 0.10, 0.15, 0.20, 0.25],
        },
    },
    {
        "id": "robust-comparison",
        "label_zh": "稳健性比较",
        "label_en": "Robust comparison",
        "analysis_id": "multi-project-comparison",
        "values": {
            "data_mode": "stable",
            "near_degenerate_eV": 0.15,
            "precision": 4,
            "missing_policy": "complete_cases",
            "sensitivity_deadbands_eV": [0.05, 0.10, 0.15, 0.20, 0.25],
        },
    },
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_identifier(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise AnalysisRequestError(f"{field} must be a string")
    text = value.strip()
    if (not text or len(text) > 128 or _CONTROL_RE.search(text)
            or not _SAFE_ID_RE.fullmatch(text)):
        raise AnalysisRequestError(f"{field} must be a safe opaque identifier")
    if _PATH_RE.search(text) or _SECRET_RE.search(text):
        raise AnalysisRequestError(f"{field} must not contain a path or credential")
    return text


def _enum(value: Any, *, field: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AnalysisRequestError(
            f"{field} must be one of {tuple(allowed)!r}")
    return value


def _bounded_number(value: Any, *, field: str, minimum: float,
                    maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisRequestError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise AnalysisRequestError(
            f"{field} must be between {minimum:g} and {maximum:g}")
    return number


def _identifier_list(value: Any, *, field: str, maximum: int = 32) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AnalysisRequestError(f"{field} must be an array")
    if len(value) > maximum:
        raise AnalysisRequestError(f"{field} contains too many identifiers")
    result = []
    for index, item in enumerate(value):
        identifier = _safe_identifier(item, field=f"{field}[{index}]")
        if identifier not in result:
            result.append(identifier)
    return tuple(result)


@dataclass(frozen=True)
class AnalysisSpec:
    analysis_id: str
    project_id: str
    comparison_project_ids: tuple[str, ...] = ()
    data_mode: str = "stable"
    near_degenerate_eV: float = 0.15
    precision: int = 4
    baseline_project_id: str | None = None
    missing_policy: str = "show_missing"
    sort_key: str = "species"
    sort_direction: str = "asc"
    sensitivity_deadbands_eV: tuple[float, ...] = (0.10, 0.15, 0.20)
    conditions: tuple[tuple[str, float], ...] = ()
    view_id: str | None = None

    @property
    def schema(self) -> str:
        return SPEC_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        capability = _ANALYSIS_BY_ID.get(self.analysis_id)
        parameters = set((capability or {}).get("parameters") or ())
        result = {
            "schema": self.schema,
            "analysis_id": self.analysis_id,
            "project_id": self.project_id,
            "data_mode": self.data_mode,
            "precision": self.precision,
            "view_id": self.view_id,
        }
        if self.analysis_id == "multi-project-comparison":
            result["comparison_project_ids"] = list(self.comparison_project_ids)
        if "near_degenerate_eV" in parameters:
            result["near_degenerate_eV"] = self.near_degenerate_eV
        if capability is None or capability.get("supports_baseline"):
            result["baseline_project_id"] = self.baseline_project_id
        if "missing_policy" in parameters:
            result["missing_policy"] = self.missing_policy
        if "sort" in parameters:
            result["sort"] = {
                "key": self.sort_key, "direction": self.sort_direction}
        if capability is None or capability.get("supports_sensitivity"):
            result["sensitivity_deadbands_eV"] = list(
                self.sensitivity_deadbands_eV)
        if self.analysis_id == "free-energy-path":
            result["conditions"] = dict(self.conditions)
        return result

    @property
    def semantic_sha256(self) -> str:
        payload = self.to_dict()
        # ``view_id`` names a user-interface preset; it is not a scientific or
        # presentation value and must never manufacture a new data identity.
        payload.pop("view_id", None)
        capability = _ANALYSIS_BY_ID.get(self.analysis_id)
        parameters = set((capability or {}).get("parameters") or ())
        if capability is not None and "sort" not in parameters:
            payload.pop("sort", None)
        for field, parameter in (
                ("near_degenerate_eV", "near_degenerate_eV"),
                ("missing_policy", "missing_policy"),
                ("sensitivity_deadbands_eV", "sensitivity_deadbands_eV")):
            if capability is not None and parameter not in parameters:
                payload.pop(field, None)
        if capability is not None and not capability["supports_baseline"]:
            payload.pop("baseline_project_id", None)
        if self.analysis_id != "multi-project-comparison":
            payload.pop("comparison_project_ids", None)
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


_REQUEST_KEYS = frozenset({
    "schema", "analysis_id", "project_id", "comparison_project_ids",
    "data_mode", "near_degenerate_eV", "precision", "baseline_project_id",
    "missing_policy", "sort", "sensitivity_deadbands_eV", "view_id",
    "conditions",
})


_CONDITION_RANGES = {
    "temperature_k": (1.0, 5000.0),
    "pressure_pa": (1e-12, 1e12),
    "ph": (-5.0, 20.0),
    "electrode_potential_v": (-20.0, 20.0),
    "coverage": (0.0, 1.0),
}


def _condition_values(value: Any, *, supported: bool) -> tuple[tuple[str, float], ...]:
    if value is None:
        return ()
    if not supported:
        raise AnalysisRequestError("conditions are only supported by free-energy-path")
    if not isinstance(value, Mapping):
        raise AnalysisRequestError("conditions must be an object")
    unknown = set(value) - set(_CONDITION_RANGES)
    if unknown:
        raise AnalysisRequestError(
            "conditions contains unknown fields: " + ", ".join(sorted(unknown)))
    result = []
    for key in sorted(value):
        raw = value[key]
        if raw in (None, ""):
            continue
        lower, upper = _CONDITION_RANGES[key]
        result.append((key, _bounded_number(
            raw, field=f"conditions.{key}", minimum=lower, maximum=upper)))
    return tuple(result)


def normalize_analysis_request(
        request: Mapping[str, Any] | None, *, project_id: str,
        default_analysis_id: str = "adsorption-energy") -> AnalysisSpec:
    """Construct one canonical analysis request from untrusted browser data."""
    if not isinstance(request, (Mapping, type(None))):
        raise AnalysisRequestError("analysis request must be an object")
    data = {} if request is None else dict(request)
    unknown = set(data) - _REQUEST_KEYS
    if unknown:
        raise AnalysisRequestError(
            "analysis request contains unknown fields: " + ", ".join(sorted(unknown)))
    if data.get("schema") not in {None, SPEC_SCHEMA}:
        raise AnalysisRequestError("unsupported analysis request schema")
    server_project_id = _safe_identifier(project_id, field="project_id")
    supplied_project = data.get("project_id", server_project_id)
    if _safe_identifier(supplied_project, field="project_id") != server_project_id:
        raise AnalysisRequestError("analysis request project binding mismatch")
    analysis_id = _safe_identifier(
        data.get("analysis_id", default_analysis_id), field="analysis_id")
    try:
        capability = _ANALYSIS_BY_ID[analysis_id]
    except KeyError as exc:
        raise AnalysisRequestError(f"unknown analysis_id: {analysis_id}") from exc
    data_mode = _enum(
        data.get("data_mode", capability["data_modes"][0]),
        field="data_mode", allowed=capability["data_modes"])
    if ("near_degenerate_eV" in data
            and "near_degenerate_eV" not in capability["parameters"]):
        raise AnalysisRequestError(
            "this analysis does not support near_degenerate_eV")
    deadband = _bounded_number(
        data.get("near_degenerate_eV", 0.15), field="near_degenerate_eV",
        minimum=0.0, maximum=1.0)
    precision = data.get("precision", 4)
    if (isinstance(precision, bool) or not isinstance(precision, int)
            or not 2 <= precision <= 8):
        raise AnalysisRequestError("precision must be an integer between 2 and 8")
    comparison_ids = _identifier_list(
        data.get("comparison_project_ids"), field="comparison_project_ids")
    if analysis_id == "multi-project-comparison":
        if server_project_id not in comparison_ids:
            comparison_ids = (server_project_id, *comparison_ids)
        if len(comparison_ids) > 32:
            raise AnalysisRequestError("comparison_project_ids contains too many identifiers")
    elif comparison_ids:
        raise AnalysisRequestError(
            "comparison_project_ids is only supported by multi-project-comparison")
    baseline = _safe_identifier(
        data.get("baseline_project_id"), field="baseline_project_id", optional=True)
    if baseline is not None:
        if not capability["supports_baseline"]:
            raise AnalysisRequestError("this analysis does not support a baseline")
        if baseline not in comparison_ids:
            raise AnalysisRequestError("baseline_project_id must be in comparison_project_ids")
    if ("missing_policy" in data
            and "missing_policy" not in capability["parameters"]):
        raise AnalysisRequestError(
            "this analysis does not support missing_policy")
    missing_policy = _enum(
        data.get("missing_policy", "show_missing"), field="missing_policy",
        allowed=MISSING_POLICIES)
    supports_sort = "sort" in capability["parameters"]
    if not supports_sort:
        if "sort" in data:
            raise AnalysisRequestError("this analysis does not support sorting")
        # Preserve the v1 wire shape without pretending that this placeholder
        # can alter an authoritative reaction path or comparison projection.
        sort_key, sort_direction = "species", "asc"
    else:
        sort = data.get("sort") or {
            "key": capability["sort_keys"][0], "direction": "asc"}
        if not isinstance(sort, Mapping) or set(sort) != {"key", "direction"}:
            raise AnalysisRequestError("sort requires exactly key and direction")
        sort_key = _enum(
            sort.get("key"), field="sort.key", allowed=capability["sort_keys"])
        sort_direction = _enum(
            sort.get("direction"), field="sort.direction", allowed=SORT_DIRECTIONS)
    raw_deadbands = data.get("sensitivity_deadbands_eV", [0.10, 0.15, 0.20])
    if (isinstance(raw_deadbands, (str, bytes))
            or not isinstance(raw_deadbands, Sequence)
            or not 1 <= len(raw_deadbands) <= 16):
        raise AnalysisRequestError(
            "sensitivity_deadbands_eV must contain 1 to 16 numbers")
    sensitivity = tuple(sorted(set(
        _bounded_number(value, field="sensitivity_deadbands_eV",
                        minimum=0.0, maximum=1.0)
        for value in raw_deadbands
    )))
    if not capability["supports_sensitivity"] and data.get(
            "sensitivity_deadbands_eV") is not None:
        raise AnalysisRequestError("this analysis does not support sensitivity options")
    view_id = _safe_identifier(
        data.get("view_id"), field="view_id", optional=True)
    conditions = _condition_values(
        data.get("conditions"), supported=analysis_id == "free-energy-path")
    return AnalysisSpec(
        analysis_id=analysis_id,
        project_id=server_project_id,
        comparison_project_ids=comparison_ids,
        data_mode=data_mode,
        near_degenerate_eV=deadband,
        precision=precision,
        baseline_project_id=baseline,
        missing_policy=missing_policy,
        sort_key=sort_key,
        sort_direction=sort_direction,
        sensitivity_deadbands_eV=sensitivity,
        conditions=conditions,
        view_id=view_id,
    )


def get_analysis(analysis_id: str) -> dict[str, Any]:
    identifier = _safe_identifier(analysis_id, field="analysis_id")
    try:
        return copy.deepcopy(_ANALYSIS_BY_ID[identifier])
    except KeyError as exc:
        raise AnalysisRequestError(f"unknown analysis_id: {identifier}") from exc


def builtin_view_templates() -> list[dict[str, Any]]:
    return [
        {"schema": VIEW_TEMPLATE_SCHEMA, **copy.deepcopy(template)}
        for template in _VIEW_TEMPLATES
    ]


def analysis_catalog() -> dict[str, Any]:
    """Return one detached, JSON-safe capability catalog for the workbench."""
    from vcstudio.generate.task_catalog import list_catalog
    from vcstudio.project.task_analysis import capability

    tasks = []
    for record in list_catalog():
        task = copy.deepcopy(record)
        task["analysis"] = capability(task["key"])
        # A builder import string is an internal implementation locator, not a
        # browser capability.  The stable task key is sufficient for dispatch.
        task.pop("builder_ref", None)
        tasks.append(task)
    analyses = copy.deepcopy(list(_ANALYSES))
    for analysis in analyses:
        analysis.update({
            "implementation_status": "live",
            # A project-specific bootstrap replaces this neutral state with a
            # server preflight/result state.  The catalog alone has no source
            # authority and therefore cannot claim availability.
            "capability_status": "unavailable",
            "activatable": False,
            "next_action": _NEXT_ACTIONS[analysis["id"]],
        })
    return {
        "schema": CATALOG_SCHEMA,
        "analyses": analyses,
        "categories": list(ANALYSIS_CATEGORIES),
        "capability_statuses": list(CAPABILITY_STATUSES),
        "task_capabilities": tasks,
        "view_templates": builtin_view_templates(),
        "data_modes": list(DATA_MODES),
        "missing_policies": [
            {
                "id": "show_missing",
                "label_zh": "明确显示缺失",
                "label_en": "Show missing values",
            },
            {
                "id": "complete_cases",
                "label_zh": "仅完整案例",
                "label_en": "Complete cases only",
            },
        ],
        "limits": {
            "precision": {"minimum": 2, "maximum": 8},
            "near_degenerate_eV": {"minimum": 0.0, "maximum": 1.0},
            "comparison_projects": 32,
            "sensitivity_points": 16,
            "conditions": {
                key: {"minimum": bounds[0], "maximum": bounds[1]}
                for key, bounds in _CONDITION_RANGES.items()
            },
        },
    }


__all__ = [
    "ANALYSIS_CATEGORIES", "CAPABILITY_STATUSES", "CATALOG_SCHEMA", "DATA_MODES",
    "MISSING_POLICIES", "SPEC_SCHEMA", "VIEW_TEMPLATE_SCHEMA",
    "AnalysisRequestError", "AnalysisSpec", "analysis_catalog",
    "builtin_view_templates", "get_analysis", "normalize_analysis_request",
]
