"""Built-in catalysis research recipes and side-effect-free DAG previews.

The catalogue is declarative.  ``preview`` validates opaque evidence references,
resolves every parameter with an explicit source, and returns a semantic hash.
It never creates a directory, writes ``job.yaml``, submits work, or contacts a
remote service.  A future execution adapter must remain behind the existing
explicit confirmation, operation-token, and idempotency gates.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from vcstudio.project.catalysis_contracts import (
    CatalysisContractError,
    EvidenceRef,
    MethodFingerprint,
    RecipeInput,
    RecipeParameter,
    ScientificLimit,
    WorkflowNode,
    WorkflowRecipe,
    WorkflowRunSnapshot,
    reject_sensitive,
    semantic_hash,
)


CATALOG_SCHEMA = "vcstudio.research-recipe-catalog/v1"
PREVIEW_SCHEMA = "vcstudio.workflow-preview/v1"


class ResearchRecipeError(CatalysisContractError):
    """A catalogue or dry-run request is invalid."""


def _evidence(reference_id: str) -> tuple[EvidenceRef, ...]:
    return (EvidenceRef(
        ref_type="publication_record", opaque_id=reference_id,
        origin="imported", revision_id="catalog-v1",
    ),)


def _method(recipe_id: str, reference_id: str) -> MethodFingerprint:
    refs = _evidence(reference_id)
    return MethodFingerprint(
        method_id=f"{recipe_id}-method-v1",
        scope="workflow_recipe",
        sha256=semantic_hash({"recipe_id": recipe_id, "method_contract": "v1"}),
        evidence_refs=refs,
    )


def _recipe(
    *, recipe_id: str, label_zh: str, label_en: str, summary_zh: str, summary_en: str,
    inputs: tuple[RecipeInput, ...], parameters: tuple[RecipeParameter, ...],
    nodes: tuple[WorkflowNode, ...], limits: tuple[ScientificLimit, ...],
    references: tuple[str, ...], reference_id: str,
) -> WorkflowRecipe:
    return WorkflowRecipe(
        recipe_id=recipe_id,
        recipe_version="1.0.0",
        label_zh=label_zh,
        label_en=label_en,
        summary_zh=summary_zh,
        summary_en=summary_en,
        inputs=inputs,
        parameters=parameters,
        nodes=nodes,
        scientific_limits=limits,
        official_reference_urls=references,
        provenance="imported",
        evidence_refs=_evidence(reference_id),
        method_fingerprint=_method(recipe_id, reference_id),
    )


_RECIPES = (
    _recipe(
        recipe_id="adsorption_energy",
        label_zh="吸附能",
        label_en="Adsorption energy",
        summary_zh="冻结清洁表面、吸附态与参考态的方法口径，预览 ΔE_ads 任务依赖。",
        summary_en=(
            "Freeze the clean surface, adsorbed state, reference state, and method basis "
            "before previewing the adsorption-energy dependency graph."
        ),
        inputs=(
            RecipeInput("surface_structure", "structure_record", "清洁表面结构证据",
                        "Clean-surface structure evidence"),
            RecipeInput("adsorbate_structure", "structure_record", "吸附态结构证据",
                        "Adsorbed-state structure evidence"),
            RecipeInput("reference_state", "calculation_result", "参考态能量证据",
                        "Reference-state energy evidence"),
            RecipeInput("method_record", "method_record", "统一方法指纹证据",
                        "Common method-fingerprint evidence"),
        ),
        parameters=(
            RecipeParameter("force_tolerance_ev_a", "number", 0.02, "力收敛阈值",
                            "Force tolerance", "eV_A", 0.0001, 1.0),
            RecipeParameter("energy_tolerance_ev", "number", 1e-5, "电子能量收敛阈值",
                            "Electronic energy tolerance", "eV", 1e-9, 0.1),
            RecipeParameter("reference_stoichiometry", "number", 1.0, "参考态计量系数",
                            "Reference-state stoichiometric factor", None, 0.000001, 1000.0),
        ),
        nodes=(
            WorkflowNode("validate_inputs", "evidence_validation", (),
                         ("surface_structure", "adsorbate_structure", "reference_state",
                          "method_record"), (), ("validated_input_set",)),
            WorkflowNode("relax_clean_surface", "relax", ("validate_inputs",),
                         (), ("force_tolerance_ev_a", "energy_tolerance_ev"),
                         ("clean_surface_energy", "clean_surface_structure")),
            WorkflowNode("relax_adsorbate_state", "relax", ("validate_inputs",),
                         (), ("force_tolerance_ev_a", "energy_tolerance_ev"),
                         ("adsorbate_state_energy", "adsorbate_state_structure")),
            WorkflowNode("compute_adsorption_energy", "analysis",
                         ("relax_clean_surface", "relax_adsorbate_state"), (),
                         ("reference_stoichiometry",), ("adsorption_energy_record",)),
        ),
        limits=(
            ScientificLimit("method_comparability_required",
                            "只有结构、赝势、泛函、自旋与数值设置可比时，ΔE_ads 才可解释。",
                            "ΔE_ads is interpretable only when structures, potentials, functional, "
                            "spin, and numerical settings are comparable."),
            ScientificLimit("reference_state_not_universal",
                            "参考态选择属于科学假设，配方 ready 不代表该选择已验证或被接受。",
                            "Reference-state choice is a scientific assumption; recipe readiness "
                            "does not mean it is validated or accepted."),
        ),
        references=(
            "https://www.catalysis-hub.org/energies",
            "https://docs.catalysis-hub.org/en/stable/reference/app.html",
        ),
        reference_id="vasp-wiki-binding-energy",
    ),
    _recipe(
        recipe_id="site_screening",
        label_zh="位点筛选",
        label_en="Site screening",
        summary_zh="从几何候选位点生成并比较多构型；活性位点结论保持为独立科学主张。",
        summary_en=(
            "Generate and compare configurations from geometric site candidates while keeping "
            "any active-site conclusion as a separate scientific claim."
        ),
        inputs=(
            RecipeInput("surface_structure", "structure_record", "表面结构证据",
                        "Surface structure evidence"),
            RecipeInput("adsorbate_record", "structure_record", "吸附质结构证据",
                        "Adsorbate structure evidence"),
            RecipeInput("geometric_sites", "imported_record", "几何候选位点集合",
                        "Geometric site-candidate set"),
            RecipeInput("method_record", "method_record", "统一方法指纹证据",
                        "Common method-fingerprint evidence"),
        ),
        parameters=(
            RecipeParameter("initial_height_a", "number", 2.0, "初始吸附高度",
                            "Initial adsorption height", "A", 0.5, 10.0),
            RecipeParameter("rotation_count", "integer", 4, "每个位点取向数",
                            "Orientations per site", None, 1, 72),
            RecipeParameter("near_degenerate_ev", "number", 0.05, "近简并阈值",
                            "Near-degenerate threshold", "eV", 0.0, 2.0),
        ),
        nodes=(
            WorkflowNode("validate_site_inputs", "evidence_validation", (),
                         ("surface_structure", "adsorbate_record", "geometric_sites",
                          "method_record"), (), ("validated_site_set",)),
            WorkflowNode("enumerate_configurations", "structure_enumeration",
                         ("validate_site_inputs",), (),
                         ("initial_height_a", "rotation_count"),
                         ("adsorption_configuration_set",)),
            WorkflowNode("relax_configurations", "relax",
                         ("enumerate_configurations",), (), (),
                         ("relaxed_configuration_set",)),
            WorkflowNode("rank_geometric_sites", "analysis", ("relax_configurations",),
                         (), ("near_degenerate_ev",), ("site_screening_record",)),
        ),
        limits=(
            ScientificLimit("geometry_not_activity",
                            "几何位点只是构型生成位置，不等同于催化活性位点。",
                            "A geometric site is a configuration-generation location, not an "
                            "identified catalytically active site."),
            ScientificLimit("sampling_not_exhaustive",
                            "有限位点与取向枚举不能证明已找到全局最稳构型。",
                            "Finite site and orientation enumeration cannot prove that the global "
                            "minimum has been found."),
        ),
        references=(
            "https://wiki.fysik.dtu.dk/ase/ase/build/surface.html",
            "https://vasp.at/wiki/KPOINTS",
        ),
        reference_id="ase-surface-builder",
    ),
    _recipe(
        recipe_id="neb_path",
        label_zh="NEB 路径",
        label_en="NEB pathway",
        summary_zh="核对始末态和原子映射，再预览图像初始化、NEB 优化与势垒提取。",
        summary_en=(
            "Validate endpoints and atom mapping before previewing image initialization, NEB "
            "optimization, and barrier extraction."
        ),
        inputs=(
            RecipeInput("initial_state", "structure_record", "已弛豫始态证据",
                        "Relaxed initial-state evidence"),
            RecipeInput("final_state", "structure_record", "已弛豫末态证据",
                        "Relaxed final-state evidence"),
            RecipeInput("atom_mapping", "imported_record", "原子一一映射证据",
                        "One-to-one atom-mapping evidence"),
            RecipeInput("method_record", "method_record", "统一方法指纹证据",
                        "Common method-fingerprint evidence"),
        ),
        parameters=(
            RecipeParameter("intermediate_images", "integer", 5, "中间图像数",
                            "Intermediate images", None, 1, 32),
            RecipeParameter("spring_ev_a2", "number", -5.0, "弹簧常数",
                            "Spring constant", "eV_A2", -20.0, 20.0),
            RecipeParameter("neb_force_ev_a", "number", 0.05, "NEB 力收敛阈值",
                            "NEB force tolerance", "eV_A", 0.001, 1.0),
            RecipeParameter("climbing_image", "boolean", True, "使用 climbing image",
                            "Use climbing image"),
        ),
        nodes=(
            WorkflowNode("validate_endpoints", "evidence_validation", (),
                         ("initial_state", "final_state", "atom_mapping", "method_record"),
                         (), ("validated_endpoint_pair",)),
            WorkflowNode("initialize_images", "path_interpolation",
                         ("validate_endpoints",), (), ("intermediate_images",),
                         ("neb_image_set",)),
            WorkflowNode("optimize_neb", "neb", ("initialize_images",), (),
                         ("spring_ev_a2", "neb_force_ev_a", "climbing_image"),
                         ("neb_energy_path",)),
            WorkflowNode("extract_barrier", "analysis", ("optimize_neb",), (), (),
                         ("activation_barrier_record",)),
        ),
        limits=(
            ScientificLimit("mapping_required",
                            "原子次序相同不自动证明物理映射正确；必须有明确映射证据。",
                            "Matching atom order does not prove a physically correct mapping; an "
                            "explicit mapping record is required."),
            ScientificLimit("path_not_mechanism_proof",
                            "收敛的一条 NEB 路径不证明不存在更低势垒机制。",
                            "One converged NEB path does not prove that no lower-barrier mechanism "
                            "exists."),
        ),
        references=(
            "https://vasp.at/wiki/Nudged_elastic_bands",
            "https://vasp.at/wiki/Improved_dimer_method",
        ),
        reference_id="vasp-wiki-neb",
    ),
    _recipe(
        recipe_id="vibrational_thermochemistry",
        label_zh="振动与热化学",
        label_en="Vibrational thermochemistry",
        summary_zh="从固定的平衡结构预览有限差分振动、频率检查和热化学校正。",
        summary_en=(
            "Preview finite-displacement vibrations, frequency checks, and thermochemical "
            "corrections from a fixed stationary structure."
        ),
        inputs=(
            RecipeInput("stationary_structure", "structure_record", "平衡结构证据",
                        "Stationary-structure evidence"),
            RecipeInput("electronic_energy", "calculation_result", "电子能证据",
                        "Electronic-energy evidence"),
            RecipeInput("condition_set", "experimental_record", "温度/压力条件证据",
                        "Temperature/pressure condition evidence"),
            RecipeInput("method_record", "method_record", "方法指纹证据",
                        "Method-fingerprint evidence"),
        ),
        parameters=(
            RecipeParameter("displacement_a", "number", 0.015, "有限位移步长",
                            "Finite-displacement amplitude", "A", 0.001, 0.2),
            RecipeParameter("temperature_k", "number", 298.15, "温度",
                            "Temperature", "K", 0.01, 5000.0),
            RecipeParameter("pressure_pa", "number", 101325.0, "压力",
                            "Pressure", "Pa", 0.000001, 1e10),
            RecipeParameter("imaginary_tolerance_cm1", "number", 20.0, "虚频容差",
                            "Imaginary-frequency tolerance", "cm-1", 0.0, 1000.0),
        ),
        nodes=(
            WorkflowNode("validate_stationary_point", "evidence_validation", (),
                         ("stationary_structure", "electronic_energy", "condition_set",
                          "method_record"), (), ("validated_stationary_point",)),
            WorkflowNode("finite_displacements", "frequency",
                         ("validate_stationary_point",), (), ("displacement_a",),
                         ("vibrational_modes",)),
            WorkflowNode("check_frequencies", "analysis", ("finite_displacements",), (),
                         ("imaginary_tolerance_cm1",), ("frequency_validation_record",)),
            WorkflowNode("thermochemistry", "analysis", ("check_frequencies",), (),
                         ("temperature_k", "pressure_pa"),
                         ("thermochemistry_record",)),
        ),
        limits=(
            ScientificLimit("harmonic_approximation",
                            "默认谐振子/刚性转子近似；低频、受限平移与非谐性需单独处理。",
                            "The default uses harmonic-oscillator/rigid-rotor assumptions; low "
                            "frequencies, hindered translation, and anharmonicity need separate treatment."),
            ScientificLimit("stationary_point_required",
                            "配方 ready 不证明结构是正确极小值或过渡态；频率模式仍需科学检查。",
                            "Recipe readiness does not prove that the structure is the intended minimum "
                            "or transition state; modes still require scientific review."),
        ),
        references=(
            "https://vasp.at/wiki/Phonons_from_finite_differences",
            "https://wiki.fysik.dtu.dk/ase/ase/thermochemistry/thermochemistry.html",
        ),
        reference_id="vasp-wiki-phonons",
    ),
    _recipe(
        recipe_id="convergence_scan",
        label_zh="收敛扫描",
        label_en="Convergence scan",
        summary_zh="固定体系与方法，仅扫描 ENCUT、k 点或真空层并保留完整参数来源。",
        summary_en=(
            "Hold the system and method fixed while scanning ENCUT, k-point density, or vacuum, "
            "with every resolved parameter source preserved."
        ),
        inputs=(
            RecipeInput("baseline_structure", "structure_record", "基线结构证据",
                        "Baseline structure evidence"),
            RecipeInput("baseline_method", "method_record", "基线方法指纹证据",
                        "Baseline method-fingerprint evidence"),
            RecipeInput("convergence_target", "imported_record", "收敛判据定义",
                        "Convergence-target definition"),
        ),
        parameters=(
            RecipeParameter("encut_ev", "number_list", (350.0, 400.0, 450.0, 500.0, 550.0),
                            "ENCUT 扫描值", "ENCUT scan values", "eV", 1.0, 10000.0),
            RecipeParameter("kpoint_density_a1", "number_list", (0.30, 0.24, 0.20, 0.16),
                            "k 点间距扫描值", "K-point spacing scan values", "A-1",
                            0.0001, 10.0),
            RecipeParameter("energy_threshold_ev_atom", "number", 0.001,
                            "每原子能量收敛阈值", "Energy threshold per atom", "eV_atom",
                            1e-7, 1.0),
            RecipeParameter("consecutive_passes", "integer", 2, "连续通过点数",
                            "Consecutive passing points", None, 1, 20),
        ),
        nodes=(
            WorkflowNode("validate_baseline", "evidence_validation", (),
                         ("baseline_structure", "baseline_method", "convergence_target"),
                         (), ("validated_convergence_baseline",)),
            WorkflowNode("encut_scan", "convergence_scan", ("validate_baseline",), (),
                         ("encut_ev",), ("encut_series",)),
            WorkflowNode("kpoint_scan", "convergence_scan", ("validate_baseline",), (),
                         ("kpoint_density_a1",), ("kpoint_series",)),
            WorkflowNode("evaluate_convergence", "analysis",
                         ("encut_scan", "kpoint_scan"), (),
                         ("energy_threshold_ev_atom", "consecutive_passes"),
                         ("convergence_decision_record",)),
        ),
        limits=(
            ScientificLimit("property_specific_convergence",
                            "总能量收敛不自动保证力、应力、势垒或频率等目标性质收敛。",
                            "Total-energy convergence does not automatically establish convergence of "
                            "forces, stress, barriers, frequencies, or another target property."),
            ScientificLimit("one_variable_design",
                            "单变量扫描要求其它方法设置冻结；联动变化会破坏归因。",
                            "A one-variable scan requires all other method settings to remain frozen; "
                            "coupled changes break attribution."),
        ),
        references=(
            "https://vasp.at/wiki/ENCUT",
            "https://vasp.at/wiki/KPOINTS",
        ),
        reference_id="vasp-wiki-convergence",
    ),
)

def _index_recipes(recipes: tuple[WorkflowRecipe, ...]) -> dict[str, dict[str, WorkflowRecipe]]:
    indexed: dict[str, dict[str, WorkflowRecipe]] = {}
    for item in recipes:
        versions = indexed.setdefault(item.recipe_id, {})
        if item.recipe_version in versions:
            raise ResearchRecipeError("duplicate recipe ID/version")
        versions[item.recipe_version] = item
    return indexed


_BY_ID = _index_recipes(_RECIPES)


def get(recipe_id: Any, recipe_version: Any = None) -> WorkflowRecipe:
    identifier = str(recipe_id or "").strip()
    versions = _BY_ID.get(identifier)
    if not versions:
        raise ResearchRecipeError("unknown recipe ID")
    wanted = str(recipe_version or "").strip()
    if not wanted:
        wanted = sorted(versions, key=lambda value: tuple(map(int, value.split("."))))[-1]
    recipe = versions.get(wanted)
    if recipe is None:
        raise ResearchRecipeError("requested recipe version is unavailable")
    return recipe


def catalog() -> dict[str, Any]:
    """Return a detached, versioned, bilingual built-in recipe catalogue."""

    recipes = []
    for recipe in sorted(_RECIPES, key=lambda item: item.recipe_id):
        payload = recipe.to_dict()
        recipes.append({**payload, "recipe_semantic_sha256": recipe.semantic_hash()})
    result = {
        "schema": CATALOG_SCHEMA,
        "recipes": recipes,
        "recipe_count": len(recipes),
        "job_source_of_truth": "job.yaml",
        "read_only": True,
        "authorizes_execution": False,
        "scientific_validation_implied": False,
    }
    return copy.deepcopy(result)


def _request(value: Any) -> tuple[dict[str, Any], dict[str, EvidenceRef]]:
    request = {} if value is None else value
    if not isinstance(request, Mapping):
        raise ResearchRecipeError("preview request must be an object")
    if set(request) - {"overrides", "evidence"}:
        raise ResearchRecipeError("preview request contains unknown fields")
    reject_sensitive(request, field="preview_request")
    overrides = request.get("overrides") or {}
    evidence = request.get("evidence") or {}
    if not isinstance(overrides, Mapping) or not isinstance(evidence, Mapping):
        raise ResearchRecipeError("overrides and evidence must be objects")
    parsed_evidence = {}
    for raw_id, raw_ref in evidence.items():
        input_id = str(raw_id or "").strip()
        parsed_evidence[input_id] = (
            raw_ref if isinstance(raw_ref, EvidenceRef) else EvidenceRef.from_dict(raw_ref))
    return dict(overrides), parsed_evidence


def preview(recipe_id: Any, recipe_version: Any = None,
            request: Any = None) -> dict[str, Any]:
    """Build a pure, deterministic dry-run DAG with explicit missing inputs."""

    recipe = get(recipe_id, recipe_version)
    overrides, evidence = _request(request)
    parameter_defs = {item.parameter_id: item for item in recipe.parameters}
    if set(overrides) - set(parameter_defs):
        raise ResearchRecipeError("preview overrides contain unknown parameters")
    input_defs = {item.input_id: item for item in recipe.inputs}
    if set(evidence) - set(input_defs):
        raise ResearchRecipeError("preview evidence contains unknown input IDs")
    for input_id, ref in evidence.items():
        if ref.ref_type != input_defs[input_id].evidence_type:
            raise ResearchRecipeError("preview evidence type does not match its input slot")

    resolved = {}
    sources = {}
    parameter_rows = []
    for definition in recipe.parameters:
        source = "user_override" if definition.parameter_id in overrides else "recipe_default"
        raw_value = overrides.get(definition.parameter_id, definition.default)
        value = definition.normalize(raw_value)
        if isinstance(value, tuple):
            value = list(value)
        resolved[definition.parameter_id] = value
        sources[definition.parameter_id] = source
        parameter_rows.append({
            "parameter_id": definition.parameter_id,
            "value": value,
            "source": source,
            "unit": definition.unit,
        })

    missing_inputs = sorted(
        item.input_id for item in recipe.inputs if item.required and item.input_id not in evidence)
    nodes = []
    status_by_node: dict[str, str] = {}
    missing_by_node: dict[str, set[str]] = {}
    for definition in recipe.nodes:
        direct_missing = {item for item in definition.input_ids if item not in evidence}
        inherited_missing = set().union(
            *(missing_by_node.get(parent, set()) for parent in definition.depends_on),
        ) if definition.depends_on else set()
        missing = direct_missing | inherited_missing
        blocked_by = [parent for parent in definition.depends_on
                      if status_by_node.get(parent) == "blocked"]
        status = "blocked" if missing or blocked_by else "ready"
        status_by_node[definition.node_id] = status
        missing_by_node[definition.node_id] = missing
        nodes.append({
            "node_id": definition.node_id,
            "task_kind": definition.task_kind,
            "depends_on": list(definition.depends_on),
            "status": status,
            "missing_prerequisites": sorted(missing),
            "blocked_by": blocked_by,
            "parameters": {
                parameter_id: resolved[parameter_id]
                for parameter_id in definition.parameter_ids
            },
            "parameter_sources": {
                parameter_id: sources[parameter_id]
                for parameter_id in definition.parameter_ids
            },
            "outputs": list(definition.outputs),
        })
    ready = not missing_inputs and all(item["status"] == "ready" for item in nodes)
    semantic = {
        "schema": PREVIEW_SCHEMA,
        "recipe_id": recipe.recipe_id,
        "recipe_version": recipe.recipe_version,
        "recipe_semantic_sha256": recipe.semantic_hash(),
        "status": "preview_ready" if ready else "blocked",
        "nodes": nodes,
        "missing_prerequisites": missing_inputs,
        "resolved_parameters": resolved,
        "parameter_sources": sources,
        "parameter_overrides": parameter_rows,
        "input_evidence": {key: ref.to_dict() for key, ref in sorted(evidence.items())},
        "scientific_limits": [item.to_dict() for item in recipe.scientific_limits],
        "official_reference_urls": list(recipe.official_reference_urls),
        "read_only": True,
        "creates_directories": False,
        "creates_jobs": False,
        "remote_side_effects": False,
        "job_source_of_truth": "job.yaml",
        "authorizes_execution": False,
        "scientific_validation_implied": False,
    }
    return {**semantic, "preview_semantic_sha256": semantic_hash(semantic)}


def pin_preview(run_id: Any, value: Mapping[str, Any]) -> WorkflowRunSnapshot:
    """Create an immutable plan snapshot with the exact recipe and parameters."""

    if not isinstance(value, Mapping):
        raise ResearchRecipeError("preview must be an object")
    required = {
        "schema", "recipe_id", "recipe_version", "recipe_semantic_sha256", "status",
        "nodes", "missing_prerequisites", "resolved_parameters", "parameter_sources",
        "parameter_overrides", "input_evidence", "scientific_limits",
        "official_reference_urls", "read_only", "creates_directories", "creates_jobs",
        "remote_side_effects", "job_source_of_truth", "authorizes_execution",
        "scientific_validation_implied", "preview_semantic_sha256",
    }
    if set(value) != required:
        raise ResearchRecipeError("preview shape is invalid")
    if value.get("schema") != PREVIEW_SCHEMA:
        raise ResearchRecipeError("preview schema is invalid")
    expected = semantic_hash({key: copy.deepcopy(item) for key, item in value.items()
                              if key != "preview_semantic_sha256"})
    if expected != value.get("preview_semantic_sha256"):
        raise ResearchRecipeError("preview semantic hash mismatch")
    recipe = get(value.get("recipe_id"), value.get("recipe_version"))
    if recipe.semantic_hash() != value.get("recipe_semantic_sha256"):
        raise ResearchRecipeError("preview recipe binding is stale")
    sources = value.get("parameter_sources")
    resolved = value.get("resolved_parameters")
    if not isinstance(sources, Mapping) or not isinstance(resolved, Mapping):
        raise ResearchRecipeError("preview parameter bindings are invalid")
    if set(sources) != set(resolved) or any(
            source not in {"recipe_default", "user_override"}
            for source in sources.values()):
        raise ResearchRecipeError("preview parameter sources are invalid")
    recomputed = preview(
        recipe.recipe_id,
        recipe.recipe_version,
        {
            "overrides": {
                key: copy.deepcopy(resolved[key])
                for key, source in sources.items() if source == "user_override"
            },
            "evidence": copy.deepcopy(value.get("input_evidence") or {}),
        },
    )
    if recomputed != copy.deepcopy(dict(value)):
        raise ResearchRecipeError("preview does not match the pinned recipe resolution")
    return WorkflowRunSnapshot(
        run_id=run_id,
        recipe_id=value["recipe_id"],
        recipe_version=value["recipe_version"],
        recipe_semantic_sha256=value["recipe_semantic_sha256"],
        resolved_parameters=copy.deepcopy(value["resolved_parameters"]),
        parameter_sources=copy.deepcopy(value["parameter_sources"]),
        input_evidence={key: EvidenceRef.from_dict(item)
                        for key, item in value["input_evidence"].items()},
        preview_semantic_sha256=value["preview_semantic_sha256"],
        plan_status=value["status"],
    )


__all__ = [
    "CATALOG_SCHEMA", "PREVIEW_SCHEMA", "ResearchRecipeError", "catalog", "get",
    "pin_preview", "preview",
]
