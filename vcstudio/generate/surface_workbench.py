"""通用表面与吸附几何工作台（纯函数、确定性、失败关闭）。

本模块刻意只实现一个可审计的最小安全子集：右手、非退化的正交 VASP5 体相
晶胞，以及绝对值不超过 :data:`MAX_MILLER_INDEX` 的三指标 Miller 面。超出该
范围时显式拒绝，不用经验常数猜结构。表面 ``termination`` 和吸附位点都只是
几何候选；这里不宣称催化活性、热力学稳定性或科学有效性。

公开门面：

``build_slabs(bulk_poscar, request)``
    枚举并去重 termination，构造带确定性 hash/provenance 的 slab 候选。

``explore_sites(slab_poscar, request, adsorbate_poscar=None)``
    枚举 ontop/bridge/hollow/other 几何位点和近似等价组；可把传入的分子
    POSCAR 按 binding atom、取向、覆盖度和双面选择生成候选。碰撞时只返回
    rejection，绝不写出该构型。

输出不含路径。``poscar`` 是服务器内部后续保存所需的私有载荷；调用者在投影
公开 DTO 时必须移除它。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from vcstudio.generate.sac_builder import cart_to_frac, write_poscar
from vcstudio.generate.slab_builder import (
    LAYER_TOL,
    count_layers,
    fix_bottom_layers,
)
from vcstudio.generate.structure_view import parse_positions

BUILDER_NAME = "vcstudio.surface_workbench"
BUILDER_VERSION = "1.0.0"
MAX_MILLER_INDEX = 4
DEFAULT_VACUUM = 15.0
DEFAULT_LAYER_TOLERANCE = 0.35
DEFAULT_MIN_DISTANCE = 1.5
MIN_CONFIGURABLE_DISTANCE = 1.0
_ORTHOGONAL_TOL = 1.0e-8
_PHASE_TOL = 1.0e-8
_SITE_KEY_DIGITS = 7
_MAX_SURFACE_ATOMS = 128
_MAX_ADSORBATE_ATOMS = 200
_MAX_SLAB_ATOMS_FOR_PLACEMENT = 2000
_MAX_OCCUPIED_SITES = 32
_MAX_PLACED_ADSORBATE_ATOMS = 1000


def _norm(vector: Sequence[float]) -> float:
    arr = np.asarray(vector, dtype=float)
    return float(np.sqrt(np.dot(arr, arr)))


def _unit(vector: Sequence[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype=float)
    length = _norm(arr)
    if not math.isfinite(length) or length <= 1.0e-12:
        raise ValueError("几何向量长度为零或非有限值，无法确定表面方向")
    return arr / length


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_payload(value: Any) -> str:
    return _sha256_text(_stable_json(value))


def structure_hash(poscar_text: str) -> str:
    """返回与路径、注释、数字排版和同种原子行顺序无关的几何 SHA-256。"""
    parsed = parse_positions(poscar_text)
    cell = np.asarray(parsed["cell"], dtype=float)
    fracs = np.asarray(cart_to_frac(np.asarray(parsed["coords"], dtype=float), cell), dtype=float)
    fracs -= np.floor(fracs)
    atoms = []
    for element, frac in zip(parsed["elements"], fracs):
        canonical = []
        for value in frac:
            wrapped = _wrap_fraction(float(value))
            canonical.append(round(wrapped, 10))
        atoms.append([element, *canonical])
    atoms.sort(key=lambda atom: (atom[0], atom[1], atom[2], atom[3]))
    payload = {
        "schema": "vcstudio.structure-geometry/v1",
        "cell": [[round(float(v), 10) for v in row] for row in parsed["cell"]],
        "atoms": atoms,
    }
    return _hash_payload(payload)


def _raw_structure_hash(poscar_text: str) -> str:
    """原始文本 hash；与 :func:`structure_hash` 的规范化几何 hash 分开。"""
    return _sha256_text(poscar_text)


def _as_finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数值")
    return result


def _as_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是正整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是正整数") from exc
    if result < 1 or result != value:
        raise ValueError(f"{field} 必须是正整数")
    return result


def _normalize_miller(value: Any) -> tuple[int, int, int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 3:
        raise ValueError("miller 必须是含 3 个整数的序列，例如 [1, 1, 1]")
    values: list[int] = []
    for component in value:
        if isinstance(component, bool):
            raise ValueError("Miller 指数必须是整数，且不能是布尔值")
        try:
            integer = int(component)
        except (TypeError, ValueError) as exc:
            raise ValueError("Miller 指数必须是整数") from exc
        if integer != component:
            raise ValueError("Miller 指数必须是整数")
        values.append(integer)
    if values == [0, 0, 0]:
        raise ValueError("Miller 指数 (0, 0, 0) 不定义晶面，已拒绝")
    divisor = math.gcd(math.gcd(abs(values[0]), abs(values[1])), abs(values[2]))
    values = [component // divisor for component in values]
    if max(abs(component) for component in values) > MAX_MILLER_INDEX:
        raise ValueError(
            f"安全实现只支持约化后 |h|,|k|,|l| ≤ {MAX_MILLER_INDEX} 的低指数面；"
            "更高指数面需要独立晶体学库复核，已拒绝"
        )
    first = next(component for component in values if component)
    if first < 0:
        values = [-component for component in values]
    return values[0], values[1], values[2]


def _bezout_two(a: int, b: int) -> tuple[int, int, int]:
    """返回 ``g, x, y``，满足 ``a*x + b*y = g = gcd(|a|,|b|)``。"""
    if a == 0 and b == 0:
        return 0, 0, 0
    old_r, r = abs(a), abs(b)
    old_s, s = 1, 0
    old_t, t = 0, 1
    while r:
        quotient = old_r // r
        old_r, r = r, old_r - quotient * r
        old_s, s = s, old_s - quotient * s
        old_t, t = t, old_t - quotient * t
    x = old_s if a >= 0 else -old_s
    y = old_t if b >= 0 else -old_t
    return old_r, x, y


def _integer_surface_basis(
    miller: tuple[int, int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构造整数 ``p,q,w``：``p×q=hkl`` 且 ``hkl·w=1``。"""
    h, k, ell = miller
    if h == 0 and k == 0:
        # 约化和符号规范化后 l 必为 1。
        return (
            np.array([1, 0, 0], dtype=int),
            np.array([0, 1, 0], dtype=int),
            np.array([0, 0, 1], dtype=int),
        )
    gcd_hk, x0, y0 = _bezout_two(h, k)
    p = np.array([k // gcd_hk, -h // gcd_hk, 0], dtype=int)
    q = np.array([ell * x0, ell * y0, -gcd_hk], dtype=int)
    gcd_all, u, z = _bezout_two(gcd_hk, ell)
    if gcd_all != 1:
        raise ValueError("内部错误：约化 Miller 指数未形成本原整数法向")
    w = np.array([x0 * u, y0 * u, z], dtype=int)
    if not np.array_equal(np.cross(p, q), np.asarray(miller, dtype=int)):
        raise ValueError("内部错误：无法构造确定性的表面整数基")
    if int(np.dot(np.asarray(miller, dtype=int), w)) != 1:
        raise ValueError("内部错误：无法构造沿表面法向的本原平移")
    return p, q, w


def _validate_cell(parsed: Mapping[str, Any], *, require_surface_cell: bool = False) -> np.ndarray:
    cell = np.asarray(parsed["cell"], dtype=float)
    if cell.shape != (3, 3) or not np.isfinite(cell).all():
        raise ValueError("晶格必须是有限的 3×3 矩阵")
    lengths = [_norm(row) for row in cell]
    if min(lengths) <= 1.0e-8:
        raise ValueError("晶格含零长度矢量，已拒绝")
    volume = float(np.dot(cell[0], np.cross(cell[1], cell[2])))
    if not math.isfinite(volume) or volume <= 1.0e-10:
        raise ValueError("只支持右手、非退化晶格；当前晶格体积非正或接近零")
    if require_surface_cell:
        normal = _unit(np.cross(cell[0], cell[1]))
        if abs(float(np.dot(cell[2], normal))) / lengths[2] < 1.0 - _ORTHOGONAL_TOL:
            raise ValueError("slab 的 c 矢量必须垂直表面 a/b 平面，已失败关闭")
        return cell
    for i in range(3):
        for j in range(i + 1, 3):
            cosine = abs(float(np.dot(cell[i], cell[j]))) / (lengths[i] * lengths[j])
            if cosine > _ORTHOGONAL_TOL:
                raise ValueError(
                    "通用 slab 最小实现只支持正交体相晶胞；非正交晶胞需晶体学库复核，已拒绝"
                )
    return cell


def _validate_coordinates(parsed: Mapping[str, Any], label: str) -> np.ndarray:
    coords = np.asarray(parsed["coords"], dtype=float)
    if coords.ndim != 2 or coords.shape[1:] != (3,) or not np.isfinite(coords).all():
        raise ValueError(f"{label} 原子坐标必须是有限的 N×3 数值矩阵")
    return coords


def _phase_value(frac: Sequence[float], miller: tuple[int, int, int]) -> float:
    phase = sum(float(frac[i]) * miller[i] for i in range(3)) % 1.0
    if phase < _PHASE_TOL or 1.0 - phase < _PHASE_TOL:
        return 0.0
    return phase


def _surface_context(bulk_poscar: str, miller_value: Any) -> dict[str, Any]:
    parsed = parse_positions(bulk_poscar)
    if not parsed["elements"]:
        raise ValueError("体相 POSCAR 不含可解析原子")
    cell = _validate_cell(parsed)
    miller = _normalize_miller(miller_value)
    p, q, w = _integer_surface_basis(miller)

    reciprocal_normal = sum(
        miller[i] * cell[i] / float(np.dot(cell[i], cell[i])) for i in range(3)
    )
    normal = _unit(reciprocal_normal)
    u_cart = np.asarray(p, dtype=float) @ cell
    v_cart = np.asarray(q, dtype=float) @ cell
    if float(np.dot(np.cross(u_cart, v_cart), normal)) < 0:
        q = -q
        v_cart = -v_cart
    e1 = _unit(u_cart)
    e2 = _unit(np.cross(normal, e1))
    a_local = np.array([_norm(u_cart), 0.0, 0.0])
    b_local = np.array([float(np.dot(v_cart, e1)), float(np.dot(v_cart, e2)), 0.0])
    if b_local[1] <= 1.0e-10:
        raise ValueError("无法得到右手二维表面晶格，已失败关闭")
    plane_spacing = 1.0 / _norm(reciprocal_normal)

    coords = _validate_coordinates(parsed, "体相 POSCAR")
    fracs = np.asarray(cart_to_frac(coords, cell), dtype=float)
    fracs -= np.floor(fracs)
    raw_groups: dict[float, list[int]] = {}
    for index, frac in enumerate(fracs):
        phase = _phase_value(frac, miller)
        key = round(phase, 8)
        if key == 1.0:
            key = 0.0
        raw_groups.setdefault(key, []).append(index)
    groups = [
        {"phase": float(phase), "atom_indices": tuple(sorted(indices))}
        for phase, indices in sorted(raw_groups.items())
    ]
    if not groups:
        raise ValueError("没有可用于切面的原子层")
    return {
        "parsed": parsed,
        "cell": cell,
        "miller": miller,
        "p": p,
        "q": q,
        "w": w,
        "normal": normal,
        "e1": e1,
        "e2": e2,
        "a_local": a_local,
        "b_local": b_local,
        "plane_spacing": plane_spacing,
        "fracs": fracs,
        "groups": groups,
    }


def _wrap_fraction(value: float) -> float:
    wrapped = value - math.floor(value)
    return 0.0 if wrapped < 1.0e-10 or 1.0 - wrapped < 1.0e-10 else wrapped


def _layer_points(context: Mapping[str, Any], group_index: int, period: int) -> list[dict]:
    group = context["groups"][group_index]
    cell = context["cell"]
    shift = period * context["w"]
    a_local = context["a_local"]
    b_local = context["b_local"]
    points = []
    for atom_index in group["atom_indices"]:
        cart = (context["fracs"][atom_index] + shift) @ cell
        x = float(np.dot(cart, context["e1"]))
        y = float(np.dot(cart, context["e2"]))
        fy = y / b_local[1]
        fx = (x - fy * b_local[0]) / a_local[0]
        fx, fy = _wrap_fraction(fx), _wrap_fraction(fy)
        points.append(
            {
                "atom_index": atom_index,
                "element": context["parsed"]["elements"][atom_index],
                "fractional_xy": (fx, fy),
            }
        )
    return sorted(
        points,
        key=lambda item: (
            item["element"],
            round(item["fractional_xy"][0], 10),
            round(item["fractional_xy"][1], 10),
            item["atom_index"],
        ),
    )


def _pbc_xy_distance(
    first: Sequence[float], second: Sequence[float], a_vec: np.ndarray, b_vec: np.ndarray
) -> float:
    best = math.inf
    for ia in (-1, 0, 1):
        for ib in (-1, 0, 1):
            delta = (
                (float(first[0]) - float(second[0]) + ia) * a_vec
                + (float(first[1]) - float(second[1]) + ib) * b_vec
            )
            best = min(best, _norm(delta))
    return best


def _layer_signature(context: Mapping[str, Any], group_index: int) -> dict[str, Any]:
    points = _layer_points(context, group_index, 0)
    pairs = []
    for i, first in enumerate(points):
        for second in points[i + 1 :]:
            pairs.append(
                [
                    min(first["element"], second["element"]),
                    max(first["element"], second["element"]),
                    round(
                        _pbc_xy_distance(
                            first["fractional_xy"],
                            second["fractional_xy"],
                            context["a_local"],
                            context["b_local"],
                        ),
                        7,
                    ),
                ]
            )
    composition: dict[str, int] = {}
    for point in points:
        composition[point["element"]] = composition.get(point["element"], 0) + 1
    return {
        "composition": sorted(composition.items()),
        "pair_distances": sorted(pairs),
        "natoms": len(points),
    }


def _termination_records(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups = context["groups"]
    layer_signatures = [_layer_signature(context, index) for index in range(len(groups))]
    by_signature: dict[str, dict[str, Any]] = {}
    for start in range(len(groups)):
        sequence = []
        for offset in range(len(groups)):
            index = (start + offset) % len(groups)
            next_index = (index + 1) % len(groups)
            next_phase = groups[next_index]["phase"] + (1.0 if next_index == 0 else 0.0)
            gap = (next_phase - groups[index]["phase"]) * context["plane_spacing"]
            sequence.append({"layer": layer_signatures[index], "next_gap": round(gap, 7)})
        signature = _hash_payload(sequence)
        record = by_signature.get(signature)
        if record is None:
            record = {
                "termination_id": f"term-{signature[:20]}",
                "geometric_signature": signature,
                "representative_group": start,
                "representative_offset": round(groups[start]["phase"], 10),
                "equivalent_offsets": [],
                "surface_composition": dict(layer_signatures[start]["composition"]),
                "classification": "geometric_termination_candidate",
                "equivalence_kind": "deterministic_geometric_signature_approximation",
            }
            by_signature[signature] = record
        record["equivalent_offsets"].append(round(groups[start]["phase"], 10))
    records = sorted(
        by_signature.values(),
        key=lambda item: (item["representative_offset"], item["termination_id"]),
    )
    for index, record in enumerate(records):
        record["index"] = index
        record["equivalent_offsets"] = sorted(set(record["equivalent_offsets"]))
    return records


def enumerate_terminations(bulk_poscar: str, miller: Sequence[int]) -> list[dict[str, Any]]:
    """枚举确定性 termination 候选并按几何签名近似去重。"""
    context = _surface_context(bulk_poscar, miller)
    return _termination_records(context)


def _normalize_surface_sides(value: Any) -> str:
    aliases = {"single": "top", "double": "both"}
    normalized = aliases.get(str(value or "top").strip().lower(), str(value or "top").strip().lower())
    if normalized not in {"top", "bottom", "both"}:
        raise ValueError("surface_sides/sides 只能是 top、bottom、both（或 single、double）")
    return normalized


def _select_terminations(
    records: Sequence[Mapping[str, Any]], selection: Any
) -> list[Mapping[str, Any]]:
    if selection in (None, "", "all"):
        return list(records)
    if isinstance(selection, bool):
        raise ValueError("termination 必须是 opaque termination_id 或整数索引")
    if isinstance(selection, int):
        selected = [record for record in records if record["index"] == selection]
    else:
        selected = [record for record in records if record["termination_id"] == str(selection)]
    if not selected:
        raise ValueError("termination 不是当前 bulk/Miller 枚举得到的候选，已拒绝")
    return selected


def build_slabs(bulk_poscar: str, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从正交体相 POSCAR 构造一个或全部去重后的 slab 候选。

    ``request`` 必需 ``miller``；可选 ``layers``、``vacuum``、``fixed_layers``、
    ``surface_sides`` 和 ``termination``/``termination_id``。``surface_sides`` 是
    后续工作流选择元数据，不会把裸 slab 自动复制或吸附。
    """
    if not isinstance(request, Mapping):
        raise ValueError("slab request 必须是映射")
    allowed = {
        "miller",
        "layers",
        "vacuum",
        "fixed_layers",
        "surface_sides",
        "sides",
        "termination",
        "termination_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ValueError(f"未知 slab request 字段：{', '.join(unknown)}")
    if "miller" not in request:
        raise ValueError("slab request 缺少 miller")
    context = _surface_context(bulk_poscar, request["miller"])
    layers = _as_positive_int(request.get("layers", 4), "layers")
    if layers > 200:
        raise ValueError("layers > 200 超出最小实现的可审计规模上限")
    vacuum = _as_finite_float(request.get("vacuum", DEFAULT_VACUUM), "vacuum")
    if vacuum < 5.0:
        raise ValueError("vacuum 必须 ≥ 5 Å；更薄周期真空已失败关闭")
    fixed_value = request.get("fixed_layers", 0)
    if isinstance(fixed_value, bool):
        raise ValueError("fixed_layers 必须是非负整数")
    try:
        fixed_layers = int(fixed_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("fixed_layers 必须是非负整数") from exc
    if fixed_layers != fixed_value or fixed_layers < 0 or fixed_layers >= layers:
        raise ValueError("fixed_layers 必须是非负整数且严格小于 layers")
    sides = _normalize_surface_sides(request.get("surface_sides", request.get("sides", "top")))

    records = _termination_records(context)
    selection = request.get("termination_id", request.get("termination"))
    selected = _select_terminations(records, selection)
    group_to_termination: dict[int, str] = {}
    for record in records:
        signature = record["geometric_signature"]
        for group_index in range(len(context["groups"])):
            probe = _termination_records_for_group(context, group_index)
            if probe == signature:
                group_to_termination[group_index] = record["termination_id"]

    requested_miller = [int(component) for component in request["miller"]]
    parameters = {
        "miller_requested": requested_miller,
        "miller": list(context["miller"]),
        "layers": layers,
        "vacuum": vacuum,
        "fixed_layers": fixed_layers,
        "surface_sides": sides,
    }
    raw_hash = _raw_structure_hash(bulk_poscar)
    input_hash = structure_hash(bulk_poscar)
    candidates = []
    groups = context["groups"]
    for termination in selected:
        start = int(termination["representative_group"])
        layer_atoms: list[dict[str, Any]] = []
        heights: list[float] = []
        start_phase = groups[start]["phase"]
        for layer_number in range(layers):
            absolute_index = start + layer_number
            group_index = absolute_index % len(groups)
            period = absolute_index // len(groups)
            height = (
                groups[group_index]["phase"] + period - start_phase
            ) * context["plane_spacing"]
            heights.append(height)
            for point in _layer_points(context, group_index, period):
                layer_atoms.append({**point, "height": height, "layer": layer_number})
        span = heights[-1] if heights else 0.0
        cell = np.array(
            [
                context["a_local"],
                context["b_local"],
                [0.0, 0.0, span + vacuum],
            ],
            dtype=float,
        )
        coords = []
        elements = []
        for atom in layer_atoms:
            fx, fy = atom["fractional_xy"]
            xy = fx * cell[0] + fy * cell[1]
            coords.append([float(xy[0]), float(xy[1]), vacuum / 2.0 + atom["height"]])
            elements.append(atom["element"])
        comment = (
            f"generic slab ({','.join(str(v) for v in context['miller'])}) "
            f"{layers}L {termination['termination_id']}"
        )
        poscar = write_poscar(comment, cell, elements, coords, mode="Cartesian")
        if fixed_layers:
            # 既有冻结工具按 z 间距 0.5 Å 分层；不满足其合同就显式拒绝。
            if count_layers(poscar) != layers:
                raise ValueError(
                    f"该晶面相邻原子层间距不满足既有 {LAYER_TOL:g} Å 分层合同，"
                    "无法安全应用 fixed_layers，已拒绝"
                )
            poscar = fix_bottom_layers(poscar, fixed_layers)
        top_group = (start + layers - 1) % len(groups)
        candidate_parameters = {**parameters, "termination_id": termination["termination_id"]}
        provenance = {
            "schema": "vcstudio.surface-builder-provenance/v1",
            "builder": BUILDER_NAME,
            "builder_version": BUILDER_VERSION,
            "raw_structure_hash": raw_hash,
            "input_structure_hash": input_hash,
            "parameters": candidate_parameters,
            "capability": "orthogonal_bulk_low_index_deterministic_minimum",
            "limitations": [
                "termination_equivalence_is_a_deterministic_geometric_signature_approximation",
                "no_surface_energy_or_reconstruction_ranking",
                "surface_sides_selects_workflow_faces_and_does_not_assert_slab_symmetry",
            ],
        }
        candidate = {
            "poscar": poscar,
            "poscar_sha256": _sha256_text(poscar),
            "structure_hash": structure_hash(poscar),
            "natoms": len(elements),
            "termination": {
                key: value
                for key, value in termination.items()
                if key != "representative_group"
            },
            "bottom_termination_id": termination["termination_id"],
            "top_termination_id": group_to_termination.get(top_group),
            "parameters": candidate_parameters,
            "provenance": provenance,
            "scientific_status": "geometric_candidate",
        }
        candidates.append(candidate)
    return sorted(candidates, key=lambda item: item["termination"]["termination_id"])


def _termination_records_for_group(context: Mapping[str, Any], start: int) -> str:
    """重算一个 group 的循环签名，供 top/bottom termination 映射。"""
    groups = context["groups"]
    layer_signatures = [_layer_signature(context, index) for index in range(len(groups))]
    sequence = []
    for offset in range(len(groups)):
        index = (start + offset) % len(groups)
        next_index = (index + 1) % len(groups)
        next_phase = groups[next_index]["phase"] + (1.0 if next_index == 0 else 0.0)
        gap = (next_phase - groups[index]["phase"]) * context["plane_spacing"]
        sequence.append({"layer": layer_signatures[index], "next_gap": round(gap, 7)})
    return _hash_payload(sequence)


def _surface_fractional_xy(cart: Sequence[float], cell: np.ndarray) -> tuple[float, float]:
    frac = np.asarray(cart_to_frac(np.asarray(cart, dtype=float), cell), dtype=float)
    return _wrap_fraction(float(frac[0])), _wrap_fraction(float(frac[1]))


def _fractional_xy_cart(frac_xy: Sequence[float], cell: np.ndarray) -> np.ndarray:
    return float(frac_xy[0]) * cell[0] + float(frac_xy[1]) * cell[1]


def _site_key(frac_xy: Sequence[float]) -> tuple[float, float]:
    return round(_wrap_fraction(float(frac_xy[0])), _SITE_KEY_DIGITS), round(
        _wrap_fraction(float(frac_xy[1])), _SITE_KEY_DIGITS
    )


def _circumcenter(first: np.ndarray, second: np.ndarray, third: np.ndarray) -> np.ndarray | None:
    ax, ay = first
    bx, by = second
    cx, cy = third
    denominator = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(denominator) < 1.0e-10:
        return None
    a2, b2, c2 = float(np.dot(first, first)), float(np.dot(second, second)), float(
        np.dot(third, third)
    )
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / denominator
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / denominator
    return np.array([ux, uy], dtype=float)


def _xy_cart_to_frac(point: Sequence[float], cell: np.ndarray) -> tuple[float, float]:
    a = cell[0]
    b = cell[1]
    normal = _unit(np.cross(a, b))
    e1 = _unit(a)
    e2 = _unit(np.cross(normal, e1))
    ax = _norm(a)
    bx, by = float(np.dot(b, e1)), float(np.dot(b, e2))
    x, y = float(point[0]), float(point[1])
    fy = y / by
    fx = (x - fy * bx) / ax
    return _wrap_fraction(fx), _wrap_fraction(fy)


def _surface_xy_basis(cell: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    normal = _unit(np.cross(cell[0], cell[1]))
    e1 = _unit(cell[0])
    e2 = _unit(np.cross(normal, e1))
    return normal, e1, e2


def _surface_atom_cloud(
    surface_atoms: Sequence[Mapping[str, Any]], cell: np.ndarray
) -> list[dict[str, Any]]:
    _normal, e1, e2 = _surface_xy_basis(cell)
    a_xy = np.array([float(np.dot(cell[0], e1)), float(np.dot(cell[0], e2))])
    b_xy = np.array([float(np.dot(cell[1], e1)), float(np.dot(cell[1], e2))])
    cloud = []
    for atom in surface_atoms:
        base = atom["xy"]
        for ia in (-1, 0, 1):
            for ib in (-1, 0, 1):
                cloud.append(
                    {
                        "atom_index": atom["atom_index"],
                        "element": atom["element"],
                        "shift": (ia, ib),
                        "xy": base + ia * a_xy + ib * b_xy,
                    }
                )
    return cloud


def _nearest_shell(point: np.ndarray, cloud: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    distances = sorted(
        [(_norm(point - item["xy"]), item) for item in cloud],
        key=lambda pair: (
            round(pair[0], 12),
            pair[1]["element"],
            pair[1]["atom_index"],
            pair[1]["shift"],
        ),
    )
    minimum = distances[0][0]
    tolerance = max(1.0e-6, minimum * 0.04)
    shell = [item for distance, item in distances if distance <= minimum + tolerance]
    return {"distance": minimum, "items": shell, "coordination": len(shell)}


def _enumerate_side_sites(
    parsed: Mapping[str, Any],
    cell: np.ndarray,
    side: str,
    layer_tolerance: float,
) -> list[dict[str, Any]]:
    normal, e1, e2 = _surface_xy_basis(cell)
    projections = [float(np.dot(coord, normal)) for coord in parsed["coords"]]
    plane = max(projections) if side == "top" else min(projections)
    indices = [
        index
        for index, projection in enumerate(projections)
        if abs(projection - plane) <= layer_tolerance
    ]
    if not indices:
        raise ValueError(f"{side} 表面未找到原子")
    if len(indices) > _MAX_SURFACE_ATOMS:
        raise ValueError(
            f"{side} 表面原子数 {len(indices)} 超过 {_MAX_SURFACE_ATOMS} 的纯 Python 安全上限"
        )
    surface_atoms = []
    for index in indices:
        frac_xy = _surface_fractional_xy(parsed["coords"][index], cell)
        cart_in_plane = _fractional_xy_cart(frac_xy, cell)
        surface_atoms.append(
            {
                "atom_index": index,
                "element": parsed["elements"][index],
                "fractional_xy": frac_xy,
                "xy": np.array(
                    [float(np.dot(cart_in_plane, e1)), float(np.dot(cart_in_plane, e2))]
                ),
            }
        )
    surface_atoms.sort(
        key=lambda atom: (
            round(atom["fractional_xy"][0], 10),
            round(atom["fractional_xy"][1], 10),
            atom["element"],
            atom["atom_index"],
        )
    )
    cloud = _surface_atom_cloud(surface_atoms, cell)
    candidates: dict[tuple[str, tuple[float, float]], dict[str, Any]] = {}

    def add(kind: str, frac_xy: Sequence[float], contributors: Sequence[int], coordination: int):
        key_xy = _site_key(frac_xy)
        key = (kind, key_xy)
        entry = {
            "kind": kind,
            "side": side,
            "fractional_xy": [float(key_xy[0]), float(key_xy[1])],
            "contributors": sorted(set(int(value) for value in contributors)),
            "coordination": int(coordination),
        }
        previous = candidates.get(key)
        if previous is None or entry["contributors"] < previous["contributors"]:
            candidates[key] = entry

    for atom in surface_atoms:
        add("ontop", atom["fractional_xy"], [atom["atom_index"]], 1)

    # 每个表面原子的第一近邻壳层给出 bridge；周期自像也参与，故单原子表面胞仍可枚举。
    neighbor_map: dict[int, list[Mapping[str, Any]]] = {}
    for atom in surface_atoms:
        distances = []
        for image in cloud:
            if image["atom_index"] == atom["atom_index"] and image["shift"] == (0, 0):
                continue
            distance = _norm(atom["xy"] - image["xy"])
            if distance > 1.0e-9:
                distances.append((distance, image))
        if not distances:
            continue
        distances.sort(
            key=lambda pair: (
                round(pair[0], 12),
                pair[1]["atom_index"],
                pair[1]["shift"],
            )
        )
        nearest = distances[0][0]
        neighbors = [image for distance, image in distances if distance <= nearest * 1.08]
        neighbor_map[atom["atom_index"]] = neighbors
        for image in neighbors:
            midpoint = (atom["xy"] + image["xy"]) / 2.0
            frac_xy = _xy_cart_to_frac(midpoint, cell)
            add("bridge", frac_xy, [atom["atom_index"], image["atom_index"]], 2)

    # 第一近邻三角形的外心；只有至少 3 个等距表面像时才标记 hollow。
    for atom in surface_atoms:
        neighbors = neighbor_map.get(atom["atom_index"], [])
        for first_index, first in enumerate(neighbors):
            for second in neighbors[first_index + 1 :]:
                center = _circumcenter(atom["xy"], first["xy"], second["xy"])
                if center is None:
                    continue
                shell = _nearest_shell(center, cloud)
                if shell["distance"] <= 1.0e-6 or shell["coordination"] < 3:
                    continue
                frac_xy = _xy_cart_to_frac(center, cell)
                add(
                    "hollow",
                    frac_xy,
                    [item["atom_index"] for item in shell["items"]],
                    shell["coordination"],
                )

    # 若胞中心没有落在已识别位点上，保留一个明确标作 other 的几何探针。
    center_key = _site_key((0.5, 0.5))
    occupied = {key_xy for _kind, key_xy in candidates}
    if center_key not in occupied:
        center_xy_cart = _fractional_xy_cart(center_key, cell)
        center_xy = np.array(
            [float(np.dot(center_xy_cart, e1)), float(np.dot(center_xy_cart, e2))]
        )
        shell = _nearest_shell(center_xy, cloud)
        add(
            "other",
            center_key,
            [item["atom_index"] for item in shell["items"]],
            shell["coordination"],
        )

    outward = normal if side == "top" else -normal
    sites = []
    for candidate in candidates.values():
        in_plane = _fractional_xy_cart(candidate["fractional_xy"], cell)
        cartesian = in_plane + plane * normal
        frac = np.asarray(cart_to_frac(cartesian, cell), dtype=float)
        sites.append(
            {
                **candidate,
                "fractional": [
                    _wrap_fraction(float(frac[0])),
                    _wrap_fraction(float(frac[1])),
                    float(frac[2]),
                ],
                "cartesian": [float(value) for value in cartesian],
                "outward_normal": [float(value) for value in outward],
                "classification": "geometric_site_candidate",
            }
        )
    return sites


def _site_local_signature(
    site: Mapping[str, Any],
    parsed: Mapping[str, Any],
    cell: np.ndarray,
    layer_tolerance: float,
) -> str:
    normal, _e1, _e2 = _surface_xy_basis(cell)
    projections = [float(np.dot(coord, normal)) for coord in parsed["coords"]]
    plane = max(projections) if site["side"] == "top" else min(projections)
    atoms = []
    for index, coord in enumerate(parsed["coords"]):
        if abs(projections[index] - plane) <= layer_tolerance:
            frac_xy = _surface_fractional_xy(coord, cell)
            distance = _pbc_xy_distance(
                site["fractional_xy"], frac_xy, cell[0], cell[1]
            )
            atoms.append([parsed["elements"][index], round(distance, 5)])
    atoms.sort(key=lambda item: (item[1], item[0]))
    payload = {
        "kind": site["kind"],
        "side": site["side"],
        "coordination": site["coordination"],
        "neighbor_distance_signature": atoms[:12],
    }
    return _hash_payload(payload)


def _attach_site_identity(
    sites: list[dict[str, Any]],
    parsed: Mapping[str, Any],
    cell: np.ndarray,
    slab_hash: str,
    layer_tolerance: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, list[str]] = {}
    for site in sites:
        signature = _site_local_signature(site, parsed, cell, layer_tolerance)
        group_id = f"geomgrp-{signature[:20]}"
        site_payload = {
            "slab_structure_hash": slab_hash,
            "side": site["side"],
            "kind": site["kind"],
            "fractional_xy": site["fractional_xy"],
        }
        site["site_id"] = f"site-{_hash_payload(site_payload)[:20]}"
        site["equivalence_group_id"] = group_id
        site["equivalence_kind"] = "geometric_symmetry_candidate"
        groups.setdefault(group_id, []).append(site["site_id"])
    kind_order = {"ontop": 0, "bridge": 1, "hollow": 2, "other": 3}
    sites.sort(
        key=lambda site: (
            0 if site["side"] == "top" else 1,
            kind_order[site["kind"]],
            site["fractional_xy"],
            site["site_id"],
        )
    )
    equivalence_groups = [
        {
            "equivalence_group_id": group_id,
            "site_ids": sorted(site_ids),
            "classification": "geometric_symmetry_candidate",
            "method": "local_neighbor_distance_signature_approximation",
            "validated_crystallographic_symmetry": False,
        }
        for group_id, site_ids in sorted(groups.items())
    ]
    return sites, equivalence_groups


def _rotation_axis(axis: Sequence[float], radians: float) -> np.ndarray:
    x, y, z = _unit(axis)
    cosine, sine = math.cos(radians), math.sin(radians)
    cross_matrix = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + sine * cross_matrix + (1.0 - cosine) * (cross_matrix @ cross_matrix)


def _rotation_from_to(first: Sequence[float], second: Sequence[float]) -> np.ndarray:
    source, target = _unit(first), _unit(second)
    cross = np.cross(source, target)
    cosine = float(np.dot(source, target))
    if _norm(cross) < 1.0e-10:
        if cosine > 0:
            return np.eye(3)
        reference = np.array([1.0, 0.0, 0.0])
        if abs(float(source[0])) > 0.8:
            reference = np.array([0.0, 1.0, 0.0])
        return _rotation_axis(np.cross(source, reference), math.pi)
    cross_matrix = np.array(
        [
            [0.0, -cross[2], cross[1]],
            [cross[2], 0.0, -cross[0]],
            [-cross[1], cross[0], 0.0],
        ]
    )
    return np.eye(3) + cross_matrix + cross_matrix @ cross_matrix / (1.0 + cosine)


def _binding_index(elements: Sequence[str], value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, Mapping):
        if "index" in value:
            value = value["index"]
        elif "element" in value:
            value = value["element"]
        else:
            raise ValueError("binding_atom 映射必须含 index 或 element")
    if isinstance(value, bool):
        raise ValueError("binding_atom 不能是布尔值")
    if isinstance(value, int):
        if not 0 <= value < len(elements):
            raise ValueError("binding_atom index 超出 adsorbate 原子范围")
        return value
    symbol = str(value).strip()
    matches = [index for index, element in enumerate(elements) if element == symbol]
    if not matches:
        raise ValueError(f"adsorbate 不含 binding_atom 元素 {symbol!r}")
    return matches[0]


def _orientation_target(value: Any, outward: np.ndarray) -> np.ndarray:
    if value in (None, "normal", "outward"):
        return outward
    if value in ("inverted", "inward"):
        return -outward
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 3:
        raise ValueError("orientation 只能是 normal、inverted 或含 3 个数值的方向向量")
    vector = np.array([_as_finite_float(item, "orientation") for item in value], dtype=float)
    return _unit(vector)


def _oriented_adsorbate(
    coords: np.ndarray,
    binding_index: int,
    orientation: Any,
    outward: np.ndarray,
    rotation_deg: float,
) -> np.ndarray:
    relative = coords - coords[binding_index]
    body = relative.mean(axis=0)
    target = _orientation_target(orientation, outward)
    if _norm(body) > 1.0e-10:
        relative = relative @ _rotation_from_to(body, target).T
    relative = relative @ _rotation_axis(outward, math.radians(rotation_deg)).T
    return relative


def _periodic_cross_distance(
    first: np.ndarray,
    second: np.ndarray,
    cell: np.ndarray,
    *,
    exclude_zero_shift: bool = False,
) -> float:
    best = math.inf
    for ia in (-1, 0, 1):
        for ib in (-1, 0, 1):
            for ic in (-1, 0, 1):
                if exclude_zero_shift and (ia, ib, ic) == (0, 0, 0):
                    continue
                shift = ia * cell[0] + ib * cell[1] + ic * cell[2]
                for first_atom in first:
                    for second_atom in second:
                        best = min(best, _norm(first_atom - (second_atom + shift)))
    return best


def _source_selective_flags(poscar_text: str, natoms: int) -> list[tuple[str, str, str]] | None:
    lines = poscar_text.splitlines()
    has_selective = len(lines) > 7 and lines[7].strip()[:1].lower() == "s"
    if not has_selective:
        return None
    coordinate_start = 9
    flags = []
    for offset in range(natoms):
        parts = lines[coordinate_start + offset].split()
        flags.append(tuple(parts[3:6]) if len(parts) >= 6 else ("T", "T", "T"))
    return flags


def _write_merged_poscar(
    comment: str,
    cell: np.ndarray,
    elements: list[str],
    coords: np.ndarray,
    slab_elements: Sequence[str],
    slab_flags: Sequence[tuple[str, str, str]] | None,
) -> str:
    element_order = list(dict.fromkeys(slab_elements))
    text = write_poscar(
        comment,
        cell,
        elements,
        coords,
        mode="Cartesian",
        element_order=element_order,
    )
    if slab_flags is None:
        return text
    flags = list(slab_flags) + [("T", "T", "T")] * (len(elements) - len(slab_elements))
    species = element_order + [element for element in elements if element not in element_order]
    species = list(dict.fromkeys(species))
    permutation = [index for element in species for index, actual in enumerate(elements) if actual == element]
    grouped_flags = [flags[index] for index in permutation]
    lines = text.splitlines()
    out = lines[:7] + ["Selective dynamics", lines[7]]
    for line, flag in zip(lines[8:], grouped_flags):
        out.append(f"{line}  {' '.join(flag)}")
    return "\n".join(out) + "\n"


def _normalize_rotations(value: Any) -> list[float]:
    if value is None:
        value = [0.0]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ValueError("rotations 必须是非空数值序列")
    rotations = []
    for item in value:
        rotation = _as_finite_float(item, "rotations") % 360.0
        if not any(abs(rotation - existing) < 1.0e-10 for existing in rotations):
            rotations.append(rotation)
    return sorted(rotations)


def _coverage_plan(
    sites: Sequence[Mapping[str, Any]], groups: Sequence[Mapping[str, Any]], coverage: float
) -> list[dict[str, Any]]:
    by_id = {site["site_id"]: site for site in sites}
    plans = []
    for group in groups:
        members = sorted(site_id for site_id in group["site_ids"] if site_id in by_id)
        if not members:
            continue
        occupied = max(1, min(len(members), int(math.floor(len(members) * coverage + 0.5))))
        plans.append(
            {
                "equivalence_group_id": group["equivalence_group_id"],
                "denominator": len(members),
                "numerator": occupied,
                "requested_coverage": coverage,
                "realized_coverage": occupied / len(members),
                "site_ids": members[:occupied],
            }
        )
    return plans


def _generate_adsorbate_candidates(
    slab_poscar: str,
    parsed: Mapping[str, Any],
    cell: np.ndarray,
    sites: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
    adsorbate_poscar: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    adsorbate = parse_positions(adsorbate_poscar)
    if not adsorbate["elements"]:
        raise ValueError("adsorbate POSCAR 不含可解析原子")
    if len(adsorbate["elements"]) > _MAX_ADSORBATE_ATOMS:
        raise ValueError(
            f"adsorbate 原子数超过 {_MAX_ADSORBATE_ATOMS} 的纯 Python 碰撞检查上限"
        )
    molecule_coords = _validate_coordinates(adsorbate, "adsorbate POSCAR")
    binding = _binding_index(adsorbate["elements"], request.get("binding_atom"))
    orientation = request.get("orientation", "normal")
    height = _as_finite_float(request.get("height", 2.0), "height")
    if height <= 0:
        raise ValueError("height 必须为正数")
    minimum = _as_finite_float(
        request.get("min_distance", DEFAULT_MIN_DISTANCE), "min_distance"
    )
    if minimum < MIN_CONFIGURABLE_DISTANCE:
        raise ValueError(
            f"min_distance 不得低于 {MIN_CONFIGURABLE_DISTANCE:g} Å；"
            "更低阈值会掩盖不合理原子距离"
        )
    rotations = _normalize_rotations(request.get("rotations", [0.0]))
    slab_coords = _validate_coordinates(parsed, "slab POSCAR")
    if len(slab_coords) > _MAX_SLAB_ATOMS_FOR_PLACEMENT:
        raise ValueError(
            f"slab 原子数超过 {_MAX_SLAB_ATOMS_FOR_PLACEMENT} 的纯 Python 碰撞检查上限"
        )
    slab_elements = list(parsed["elements"])
    slab_flags = _source_selective_flags(slab_poscar, len(slab_elements))
    molecule_elements = list(adsorbate["elements"])
    by_id = {site["site_id"]: site for site in sites}
    generated, rejections = [], []
    for plan in plans:
        selected_sites = [by_id[site_id] for site_id in plan["site_ids"]]
        for rotation in rotations:
            if (
                len(selected_sites) > _MAX_OCCUPIED_SITES
                or len(selected_sites) * len(molecule_elements) > _MAX_PLACED_ADSORBATE_ATOMS
            ):
                rejections.append(
                    {
                        "equivalence_group_id": plan["equivalence_group_id"],
                        "site_ids": list(plan["site_ids"]),
                        "rotation_deg": rotation,
                        "coverage": {
                            key: plan[key]
                            for key in (
                                "denominator",
                                "numerator",
                                "requested_coverage",
                                "realized_coverage",
                            )
                        },
                        "reason": "requested discrete coverage exceeds auditable placement scale",
                        "classification": "geometry_rejected_fail_closed",
                        "poscar": None,
                    }
                )
                continue
            placed_molecules: list[np.ndarray] = []
            closest = math.inf
            rejection = None
            for site in selected_sites:
                outward = np.asarray(site["outward_normal"], dtype=float)
                relative = _oriented_adsorbate(
                    molecule_coords, binding, orientation, outward, rotation
                )
                placed = relative + np.asarray(site["cartesian"], dtype=float) + height * outward
                slab_distance = _periodic_cross_distance(placed, slab_coords, cell)
                closest = min(closest, slab_distance)
                if slab_distance < minimum:
                    rejection = (
                        f"adsorbate-slab minimum distance {slab_distance:.4f} Å "
                        f"< {minimum:.4f} Å"
                    )
                    break
                self_image_distance = _periodic_cross_distance(
                    placed, placed, cell, exclude_zero_shift=True
                )
                closest = min(closest, self_image_distance)
                if self_image_distance < minimum:
                    rejection = (
                        f"adsorbate periodic-image minimum distance {self_image_distance:.4f} Å "
                        f"< {minimum:.4f} Å"
                    )
                    break
                for previous in placed_molecules:
                    molecule_distance = _periodic_cross_distance(placed, previous, cell)
                    closest = min(closest, molecule_distance)
                    if molecule_distance < minimum:
                        rejection = (
                            f"adsorbate-adsorbate minimum distance {molecule_distance:.4f} Å "
                            f"< {minimum:.4f} Å"
                        )
                        break
                if rejection:
                    break
                placed_molecules.append(placed)
            common = {
                "equivalence_group_id": plan["equivalence_group_id"],
                "site_ids": list(plan["site_ids"]),
                "rotation_deg": rotation,
                "coverage": {
                    key: plan[key]
                    for key in (
                        "denominator",
                        "numerator",
                        "requested_coverage",
                        "realized_coverage",
                    )
                },
            }
            if rejection:
                rejections.append(
                    {
                        **common,
                        "reason": rejection,
                        "classification": "geometry_rejected_fail_closed",
                        "poscar": None,
                    }
                )
                continue
            merged_coords = np.vstack([slab_coords, *placed_molecules])
            merged_elements = slab_elements + molecule_elements * len(placed_molecules)
            text = _write_merged_poscar(
                f"adsorption geometry candidate {plan['equivalence_group_id']} r{rotation:g}",
                cell,
                merged_elements,
                merged_coords,
                slab_elements,
                slab_flags,
            )
            generated.append(
                {
                    **common,
                    "poscar": text,
                    "poscar_sha256": _sha256_text(text),
                    "structure_hash": structure_hash(text),
                    "min_distance": round(closest, 6),
                    "natoms": len(merged_elements),
                    "scientific_status": "geometric_candidate",
                }
            )
    generated.sort(key=lambda item: (item["equivalence_group_id"], item["rotation_deg"]))
    rejections.sort(key=lambda item: (item["equivalence_group_id"], item["rotation_deg"]))
    return generated, rejections


def explore_sites(
    slab_poscar: str,
    request: Mapping[str, Any],
    adsorbate_poscar: str | None = None,
) -> dict[str, Any]:
    """枚举通用几何吸附位点，并可生成碰撞检查后的吸附初始构型。

    ``coverage`` 在每个近似等价组内解释为占据数/该组候选数；不能精确表达时会
    返回 ``requested_coverage`` 与 ``realized_coverage``，不伪装成精确热力学覆盖度。
    """
    if not isinstance(request, Mapping):
        raise ValueError("site request 必须是映射")
    allowed = {
        "sides",
        "surface_sides",
        "layer_tolerance",
        "binding_atom",
        "orientation",
        "coverage",
        "height",
        "min_distance",
        "rotations",
        "site_ids",
        "site_kinds",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ValueError(f"未知 site request 字段：{', '.join(unknown)}")
    parsed = parse_positions(slab_poscar)
    if not parsed["elements"]:
        raise ValueError("slab POSCAR 不含可解析原子")
    cell = _validate_cell(parsed, require_surface_cell=True)
    _validate_coordinates(parsed, "slab POSCAR")
    sides = _normalize_surface_sides(request.get("sides", request.get("surface_sides", "top")))
    layer_tolerance = _as_finite_float(
        request.get("layer_tolerance", DEFAULT_LAYER_TOLERANCE), "layer_tolerance"
    )
    if not 0.05 <= layer_tolerance <= 1.0:
        raise ValueError("layer_tolerance 必须在 0.05–1.0 Å 之间")
    coverage = _as_finite_float(request.get("coverage", 1.0), "coverage")
    if not 0.0 < coverage <= 1.0:
        raise ValueError("coverage 必须满足 0 < coverage ≤ 1")
    requested_sides = ["top", "bottom"] if sides == "both" else [sides]
    sites = []
    for side in requested_sides:
        sites.extend(_enumerate_side_sites(parsed, cell, side, layer_tolerance))
    slab_hash = structure_hash(slab_poscar)
    sites, equivalence_groups = _attach_site_identity(
        sites, parsed, cell, slab_hash, layer_tolerance
    )

    kinds_value = request.get("site_kinds")
    if kinds_value is not None:
        if isinstance(kinds_value, (str, bytes)) or not isinstance(kinds_value, Sequence):
            raise ValueError("site_kinds 必须是位点类型序列")
        kinds = {str(value).strip().lower() for value in kinds_value}
        if not kinds or not kinds <= {"ontop", "bridge", "hollow", "other"}:
            raise ValueError("site_kinds 只能包含 ontop、bridge、hollow、other")
        sites = [site for site in sites if site["kind"] in kinds]

    ids_value = request.get("site_ids")
    if ids_value is not None:
        if isinstance(ids_value, (str, bytes)) or not isinstance(ids_value, Sequence):
            raise ValueError("site_ids 必须是 opaque site_id 序列")
        requested_ids = [str(value) for value in ids_value]
        if len(requested_ids) != len(set(requested_ids)):
            raise ValueError("site_ids 含重复 opaque identity")
        known_ids = {site["site_id"] for site in sites}
        unknown_ids = sorted(set(requested_ids) - known_ids)
        if unknown_ids:
            raise ValueError("site_ids 含当前 slab/site request 未枚举出的 opaque identity")
        sites = [site for site in sites if site["site_id"] in set(requested_ids)]
    if not sites:
        raise ValueError("筛选后没有几何位点候选")
    selected_ids = {site["site_id"] for site in sites}
    equivalence_groups = [
        {**group, "site_ids": [site_id for site_id in group["site_ids"] if site_id in selected_ids]}
        for group in equivalence_groups
        if any(site_id in selected_ids for site_id in group["site_ids"])
    ]
    plans = _coverage_plan(sites, equivalence_groups, coverage)
    generated: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    if adsorbate_poscar is not None:
        generated, rejections = _generate_adsorbate_candidates(
            slab_poscar,
            parsed,
            cell,
            sites,
            plans,
            request,
            adsorbate_poscar,
        )
    parameters = {
        "sides": sides,
        "layer_tolerance": layer_tolerance,
        "coverage": coverage,
        "site_kinds": sorted({site["kind"] for site in sites}),
        "binding_atom": request.get("binding_atom"),
        "orientation": request.get("orientation", "normal"),
        "height": request.get("height", 2.0),
        "min_distance": request.get("min_distance", DEFAULT_MIN_DISTANCE),
        "rotations": _normalize_rotations(request.get("rotations", [0.0])),
    }
    return {
        "sites": sites,
        "equivalence_groups": equivalence_groups,
        "coverage_plan": plans,
        "generated": generated,
        "rejections": rejections,
        "parameters": parameters,
        "provenance": {
            "schema": "vcstudio.adsorption-site-provenance/v1",
            "builder": BUILDER_NAME,
            "builder_version": BUILDER_VERSION,
            "raw_structure_hash": _raw_structure_hash(slab_poscar),
            "input_structure_hash": slab_hash,
            "parameters": parameters,
            "capability": "geometric_site_candidates_with_periodic_collision_checks",
        },
        "limitations": [
            "sites_are_geometric_candidates_not_catalytic_activity_claims",
            "equivalence_groups_are_deterministic_local_geometry_approximations",
            "coverage_is_discrete_within_each_candidate_equivalence_group",
            "no_surface_reconstruction_or_adsorption_energy_ranking",
        ],
        "scientific_status": "geometric_candidate",
    }
