"""结构来源会话：本地 CIF/POSCAR 与外部网关的最小、失效关闭边界。

本模块刻意不实现网络、缓存或凭据存储。远端数据只能来自调用方注入的
``ExternalStructureGateway``（或同名 callable 映射）；因此 Materials Project、
OPTIMADE 等具体 provider 的网络与许可策略仍由 External Reference Gateway 独占。

公开搜索结果只包含 opaque token、来源 ID、化学式、许可/引用、方法元数据和
原始结构 SHA-256。文件路径与原始结构只保留在进程内；用户进入 ``preview`` 后才
得到固定标题的规范化 POSCAR，显式 ``confirm`` 后再以单次 confirmation token
交给服务器侧 ``resolve_confirmed`` 消费。
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import shlex
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.structure_view import parse_positions, structure_view


PARSER_VERSION = "vcstudio.structure-source/v1"
DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_MAX_TOKENS = 128
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_ATOMS = 20_000
MAX_GATEWAY_RESULTS = 100

_RESULT_FIELDS = frozenset({
    "token", "source_id", "formula", "license", "citation", "method",
    "raw_structure_sha256",
})
_GATEWAY_PREVIEW_FIELDS = frozenset({
    "token", "source_id", "formula", "raw_structure_sha256", "poscar",
})
_CAPABILITY_FIELDS = frozenset({
    "provider", "label", "modes", "formats", "network", "enabled",
})
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{7,255}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}\Z")
_SAFE_METADATA_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_FORMULA_RE = re.compile(r"[A-Za-z0-9().+\-\s\u00b7]{1,128}\Z")
_HEX_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FILESYSTEM_PATH_RE = re.compile(
    r"(?i)(?:^[A-Z]:[\\/]|^\\\\|^/|^~[\\/]|^\.\.?[\\/]|^file:)")
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|credential|api[_ -]?key|access[_ -]?token)\s*[:=]")
_CIF_NUMBER_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?(?:\(\d+\))?\Z")
_BAD_METADATA_KEYS = frozenset({
    "path", "paths", "filepath", "file_path", "directory", "dir",
    "token", "password", "passwd", "secret", "credential", "credentials",
    "api_key", "apikey", "access_token", "refresh_token", "raw_structure",
})

# 元素符号只用于拒绝拼写错误和伪标签，不包含任何第三方数据。
_ELEMENTS = frozenset((
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg",
    "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr",
    "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd",
    "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf",
    "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po",
    "At", "Rn", "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm",
    "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr", "Rf", "Db", "Sg", "Bh", "Hs",
    "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
))


class StructureSourceError(RuntimeError):
    """结构来源合同或运行时错误的基类。"""


class StructureSourceValidationError(StructureSourceError, ValueError):
    """输入或注入网关 DTO 不满足 fail-closed 合同。"""


class StructureSourceTokenError(StructureSourceError, ValueError):
    """来源/确认 token 无效、过期或已消费。"""


class StructureSourceChangedError(StructureSourceError):
    """本地来源在选择后发生变化。"""


@runtime_checkable
class ExternalStructureGateway(Protocol):
    """仅消费 path-free DTO 的注入式外部网关合同。

    实现方可以执行网络工作，但本模块不会创建网络客户端、读取凭据或缓存响应。
    ``StructureSourceSession`` 也接受包含三个同名 callable 的 Mapping，方便 API 层
    将既有 gateway 函数注入而无需适配继承体系。
    """

    def capabilities(self) -> Sequence[Mapping[str, Any]]: ...

    def search(self, provider: str, query: str) -> Sequence[Mapping[str, Any]]: ...

    def preview(self, token: str) -> Mapping[str, Any]: ...


@runtime_checkable
class StructureSourceProvider(Protocol):
    """本地 provider 的窄抽象；远端 provider 由外部网关统一代理。"""

    provider_id: str

    def capability(self) -> Mapping[str, Any]: ...

    def select(self, path: str | os.PathLike[str]) -> "LocalStructureSelection": ...


@dataclass(frozen=True)
class ParsedStructure:
    """规范化、可确定性散列的笛卡尔结构。"""

    elements: tuple[str, ...]
    counts: tuple[int, ...]
    cell: tuple[tuple[float, float, float], ...]
    cartesian_coords: tuple[tuple[float, float, float], ...]
    canonical_poscar: str
    formula: str

    @property
    def structure_sha256(self) -> str:
        return hashlib.sha256(self.canonical_poscar.encode("utf-8")).hexdigest()

    def public_structure(self) -> dict[str, Any]:
        return {
            "format": "poscar",
            "poscar": self.canonical_poscar,
            "elements": list(self.elements),
            "counts": list(self.counts),
            "cell": [list(vector) for vector in self.cell],
            "cartesian_coords": [list(coord) for coord in self.cartesian_coords],
        }


@dataclass(frozen=True)
class LocalStructureSelection:
    path: Path
    raw_bytes: bytes
    raw_source: str
    source_format: str
    raw_structure_sha256: str
    parsed: ParsedStructure


@dataclass(frozen=True)
class _ResolvedStructure:
    raw_source: str
    source_format: str
    parsed: ParsedStructure


@dataclass
class _SourceRecord:
    token: str
    result: dict[str, Any]
    provenance: dict[str, Any]
    created_at: float
    expires_at: float
    kind: str
    local: LocalStructureSelection | None = None
    query: str | None = None
    resolved: _ResolvedStructure | None = None
    confirmation_token: str | None = None


@dataclass(frozen=True)
class _ConfirmationRecord:
    token: str
    source: _SourceRecord
    resolved: _ResolvedStructure
    created_at: float
    expires_at: float


def _canonical_zero(value: float) -> float:
    return 0.0 if abs(value) < 1e-12 else float(value)


def _format_number(value: float) -> str:
    value = _canonical_zero(value)
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _determinant(cell: Sequence[Sequence[float]]) -> float:
    (a, b, c), (d, e, f), (g, h, i) = cell
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _element(value: str, *, label: bool = False) -> str:
    text = str(value or "").strip()
    if label:
        match = re.match(r"([A-Z][a-z]?)", text)
    else:
        match = re.fullmatch(r"([A-Z][a-z]?)(?:\d*[+-])?", text)
    if match is None or match.group(1) not in _ELEMENTS:
        raise StructureSourceValidationError(f"invalid chemical element symbol: {text!r}")
    return match.group(1)


def _build_parsed_structure(
    atom_elements: Sequence[str],
    atom_coords: Sequence[Sequence[float]],
    cell: Sequence[Sequence[float]],
) -> ParsedStructure:
    if not atom_elements or len(atom_elements) != len(atom_coords):
        raise StructureSourceValidationError("structure must contain matching atoms and coordinates")
    if len(atom_elements) > MAX_ATOMS:
        raise StructureSourceValidationError(f"structure exceeds {MAX_ATOMS} atoms")
    if len(cell) != 3 or any(len(vector) != 3 for vector in cell):
        raise StructureSourceValidationError("structure cell must be a finite 3x3 matrix")

    normalized_cell = tuple(
        tuple(_canonical_zero(float(component)) for component in vector)
        for vector in cell
    )
    if not all(math.isfinite(value) for vector in normalized_cell for value in vector):
        raise StructureSourceValidationError("structure cell must contain finite values")
    if abs(_determinant(normalized_cell)) <= 1e-12:
        raise StructureSourceValidationError("structure cell is degenerate")

    grouped: dict[str, list[tuple[float, float, float]]] = {}
    for raw_element, raw_coord in zip(atom_elements, atom_coords):
        element = _element(raw_element)
        if len(raw_coord) != 3:
            raise StructureSourceValidationError("each atomic coordinate must have three values")
        coord = tuple(_canonical_zero(float(value)) for value in raw_coord)
        if not all(math.isfinite(value) for value in coord):
            raise StructureSourceValidationError("atomic coordinates must be finite")
        grouped.setdefault(element, []).append(coord)

    elements = tuple(grouped)
    counts = tuple(len(grouped[element]) for element in elements)
    coords = tuple(coord for element in elements for coord in grouped[element])
    formula = "".join(
        element + (str(count) if count != 1 else "")
        for element, count in zip(elements, counts)
    )
    lines = ["vcstudio structure source", "1.0"]
    lines.extend(" ".join(_format_number(value) for value in vector) for vector in normalized_cell)
    lines.extend((" ".join(elements), " ".join(str(count) for count in counts), "Cartesian"))
    lines.extend(" ".join(_format_number(value) for value in coord) for coord in coords)
    canonical_poscar = "\n".join(lines) + "\n"
    return ParsedStructure(
        elements=elements,
        counts=counts,
        cell=normalized_cell,
        cartesian_coords=coords,
        canonical_poscar=canonical_poscar,
        formula=formula,
    )


def _parse_poscar(content: str) -> ParsedStructure:
    elements, counts = parse_poscar_species(content)
    if not elements or not counts or len(elements) != len(counts):
        raise StructureSourceValidationError(
            "POSCAR must use VASP5 element and count lines")
    if any(isinstance(count, bool) or count <= 0 for count in counts):
        raise StructureSourceValidationError("POSCAR atom counts must be positive integers")
    if sum(counts) > MAX_ATOMS:
        raise StructureSourceValidationError(f"structure exceeds {MAX_ATOMS} atoms")
    for element in elements:
        _element(element)
    try:
        parsed = parse_positions(content)
    except (ValueError, IndexError, TypeError) as exc:
        raise StructureSourceValidationError(f"invalid POSCAR structure: {exc}") from exc
    return _build_parsed_structure(parsed["elements"], parsed["coords"], parsed["cell"])


def _tokenize_cif(content: str) -> list[str]:
    tokens: list[str] = []
    for line in content.splitlines():
        # 最小 parser 不猜测 CIF 的分号多行文本边界。
        if line.startswith(";"):
            raise StructureSourceValidationError("CIF semicolon text blocks are unsupported")
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens.extend(list(lexer))
        except ValueError as exc:
            raise StructureSourceValidationError("CIF contains invalid quoted text") from exc
    return tokens


def _is_cif_control(token: str) -> bool:
    lower = token.lower()
    return (
        token.startswith("_")
        or lower == "loop_"
        or lower == "stop_"
        or lower == "global_"
        or lower.startswith("data_")
        or lower.startswith("save_")
    )


def _parse_cif_tables(content: str) -> tuple[dict[str, str], list[tuple[list[str], list[str]]]]:
    tokens = _tokenize_cif(content)
    scalars: dict[str, str] = {}
    loops: list[tuple[list[str], list[str]]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower == "loop_":
            index += 1
            headers: list[str] = []
            while index < len(tokens) and tokens[index].startswith("_"):
                headers.append(tokens[index].lower())
                index += 1
            if not headers:
                raise StructureSourceValidationError("CIF loop_ must declare column names")
            atom_fractional = {
                "_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z",
            }.intersection(headers)
            if atom_fractional and atom_fractional != {
                "_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z",
            }:
                raise StructureSourceValidationError(
                    "CIF atom fractional coordinate loop is incomplete")
            values: list[str] = []
            while index < len(tokens) and not _is_cif_control(tokens[index]):
                values.append(tokens[index])
                index += 1
            if not values or len(values) % len(headers) != 0:
                raise StructureSourceValidationError("CIF loop row length does not match its columns")
            loops.append((headers, values))
            continue
        if token.startswith("_"):
            if index + 1 >= len(tokens) or _is_cif_control(tokens[index + 1]):
                raise StructureSourceValidationError(f"CIF scalar {token} has no value")
            key = lower
            value = tokens[index + 1]
            if key in scalars and scalars[key] != value:
                raise StructureSourceValidationError(f"CIF scalar {token} is ambiguous")
            scalars[key] = value
            index += 2
            continue
        if (
            lower.startswith("data_") or lower.startswith("save_")
            or lower in {"stop_", "global_"}
        ):
            index += 1
            continue
        raise StructureSourceValidationError(f"unsupported CIF token outside a field: {token!r}")
    return scalars, loops


def _cif_number(value: str, *, field: str) -> float:
    text = str(value or "").strip()
    if not _CIF_NUMBER_RE.fullmatch(text):
        raise StructureSourceValidationError(f"CIF {field} must be a finite numeric value")
    base = re.sub(r"\(\d+\)\Z", "", text)
    number = float(base)
    if not math.isfinite(number):
        raise StructureSourceValidationError(f"CIF {field} must be finite")
    return number


def _cif_cell(scalars: Mapping[str, str]) -> tuple[tuple[float, float, float], ...]:
    required = (
        "_cell_length_a", "_cell_length_b", "_cell_length_c",
        "_cell_angle_alpha", "_cell_angle_beta", "_cell_angle_gamma",
    )
    missing = [name for name in required if name not in scalars]
    if missing:
        raise StructureSourceValidationError("CIF cell is incomplete")
    a, b, c = (
        _cif_number(scalars[name], field=name) for name in required[:3]
    )
    alpha, beta, gamma = (
        _cif_number(scalars[name], field=name) for name in required[3:]
    )
    if min(a, b, c) <= 0:
        raise StructureSourceValidationError("CIF cell lengths must be positive")
    if not all(0.0 < angle < 180.0 for angle in (alpha, beta, gamma)):
        raise StructureSourceValidationError("CIF cell angles must be between 0 and 180 degrees")

    alpha_r, beta_r, gamma_r = map(math.radians, (alpha, beta, gamma))
    sin_gamma = math.sin(gamma_r)
    if abs(sin_gamma) <= 1e-12:
        raise StructureSourceValidationError("CIF cell angles produce a degenerate cell")
    c_x = c * math.cos(beta_r)
    c_y = c * (
        math.cos(alpha_r) - math.cos(beta_r) * math.cos(gamma_r)
    ) / sin_gamma
    c_z_sq = c * c - c_x * c_x - c_y * c_y
    if c_z_sq <= 1e-12:
        raise StructureSourceValidationError("CIF cell angles produce a degenerate cell")
    return (
        (_canonical_zero(a), 0.0, 0.0),
        (_canonical_zero(b * math.cos(gamma_r)), _canonical_zero(b * sin_gamma), 0.0),
        (_canonical_zero(c_x), _canonical_zero(c_y), _canonical_zero(math.sqrt(c_z_sq))),
    )


def _validate_cif_symmetry(
    scalars: Mapping[str, str], loops: Sequence[tuple[list[str], list[str]]],
) -> None:
    """受限 parser 不展开对称性，因此只接受显式 P1/恒等操作。"""
    number_tags = ("_space_group_it_number", "_symmetry_int_tables_number")
    for tag in number_tags:
        if tag in scalars and _cif_number(scalars[tag], field=tag) != 1.0:
            raise StructureSourceValidationError(
                "CIF non-P1 symmetry requires expansion and is unsupported")
    name_tags = ("_space_group_name_h-m_alt", "_symmetry_space_group_name_h-m")
    for tag in name_tags:
        if tag in scalars:
            normalized = re.sub(r"[\s_]", "", scalars[tag]).upper()
            if normalized != "P1":
                raise StructureSourceValidationError(
                    "CIF non-P1 symmetry requires expansion and is unsupported")

    operation_tags = {
        "_space_group_symop_operation_xyz", "_symmetry_equiv_pos_as_xyz",
    }
    operations: list[str] = []
    for tag in operation_tags:
        if tag in scalars:
            operations.append(scalars[tag])
    for headers, values in loops:
        matched = operation_tags.intersection(headers)
        for tag in matched:
            width = len(headers)
            position = headers.index(tag)
            operations.extend(values[offset + position] for offset in range(0, len(values), width))
    if operations:
        normalized = [re.sub(r"\s", "", operation).lower() for operation in operations]
        if len(normalized) != 1 or normalized[0].lstrip("+") not in {"x,y,z"}:
            raise StructureSourceValidationError(
                "CIF non-P1 symmetry requires expansion and is unsupported")


def _parse_cif(content: str) -> ParsedStructure:
    scalars, loops = _parse_cif_tables(content)
    _validate_cif_symmetry(scalars, loops)
    cell = _cif_cell(scalars)
    coordinate_tags = {
        "_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z",
    }
    complete: list[tuple[list[str], list[str]]] = []
    for headers, values in loops:
        header_set = set(headers)
        present = coordinate_tags.intersection(header_set)
        if present and (
            present != coordinate_tags
            or not ({"_atom_site_type_symbol", "_atom_site_label"} & header_set)
        ):
            raise StructureSourceValidationError(
                "CIF atom fractional coordinate loop is incomplete")
        if present == coordinate_tags:
            complete.append((headers, values))
    if len(complete) != 1:
        raise StructureSourceValidationError(
            "CIF must contain exactly one complete atom fractional coordinate loop")

    headers, values = complete[0]
    width = len(headers)
    rows = [values[offset:offset + width] for offset in range(0, len(values), width)]
    if len(rows) > MAX_ATOMS:
        raise StructureSourceValidationError(f"structure exceeds {MAX_ATOMS} atoms")
    column = {name: index for index, name in enumerate(headers)}
    symbol_column = column.get("_atom_site_type_symbol")
    label_column = column.get("_atom_site_label")
    occupancy_column = column.get("_atom_site_occupancy")
    atom_elements: list[str] = []
    fractional: list[tuple[float, float, float]] = []
    for row in rows:
        symbol = row[symbol_column] if symbol_column is not None else ""
        if symbol in {"", ".", "?"}:
            if label_column is None:
                raise StructureSourceValidationError("CIF atom has no element symbol")
            element = _element(row[label_column], label=True)
        else:
            element = _element(symbol)
        if occupancy_column is not None:
            occupancy = _cif_number(row[occupancy_column], field="atom occupancy")
            if not math.isclose(occupancy, 1.0, rel_tol=0.0, abs_tol=1e-9):
                raise StructureSourceValidationError(
                    "CIF partial occupancy is unsupported; explicit ordered atoms are required")
        xyz = tuple(
            _cif_number(row[column[tag]], field=tag)
            for tag in ("_atom_site_fract_x", "_atom_site_fract_y", "_atom_site_fract_z")
        )
        atom_elements.append(element)
        fractional.append(xyz)

    cartesian = [
        (
            x * cell[0][0] + y * cell[1][0] + z * cell[2][0],
            x * cell[0][1] + y * cell[1][1] + z * cell[2][1],
            x * cell[0][2] + y * cell[1][2] + z * cell[2][2],
        )
        for x, y, z in fractional
    ]
    return _build_parsed_structure(atom_elements, cartesian, cell)


def parse_structure_content(content: str, source_format: str) -> ParsedStructure:
    """解析本地结构内容并返回规范化结构；不执行 IO。"""
    if not isinstance(content, str) or not content.strip():
        raise StructureSourceValidationError("structure content must be non-empty text")
    normalized_format = str(source_format or "").strip().lower()
    if normalized_format == "poscar":
        return _parse_poscar(content)
    if normalized_format == "cif":
        return _parse_cif(content)
    raise StructureSourceValidationError("source_format must be 'poscar' or 'cif'")


def _source_format(path: Path, content: str) -> str:
    name = path.name.lower()
    if path.suffix.lower() == ".cif":
        return "cif"
    if name in {"poscar", "contcar"} or path.suffix.lower() in {".poscar", ".vasp"}:
        return "poscar"
    lowered = content.lower()
    if "_cell_length_a" in lowered or re.search(r"(?m)^\s*data_", lowered):
        return "cif"
    return "poscar"


def _read_local(path: Path) -> tuple[bytes, str]:
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_SOURCE_BYTES:
            raise StructureSourceValidationError(
                f"local structure must be between 1 and {MAX_SOURCE_BYTES} bytes")
        raw = path.read_bytes()
    except StructureSourceValidationError:
        raise
    except OSError as exc:
        raise StructureSourceError("local structure cannot be read") from exc
    if len(raw) != size:
        raise StructureSourceChangedError("local structure changed while it was being read")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StructureSourceValidationError("local structure must be UTF-8 text") from exc
    return raw, text


class LocalStructureProvider:
    """只接受服务器侧已选择路径的本地 provider。"""

    provider_id = "local"

    def capability(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "label": {"zh": "本地结构文件", "en": "Local structure file"},
            "modes": ["select", "preview"],
            "formats": ["cif", "poscar"],
            "network": False,
            "enabled": True,
        }

    def select(self, path: str | os.PathLike[str]) -> LocalStructureSelection:
        source_path = Path(os.fspath(path))
        raw, text = _read_local(source_path)
        source_format = _source_format(source_path, text)
        parsed = parse_structure_content(text, source_format)
        return LocalStructureSelection(
            path=source_path,
            raw_bytes=raw,
            raw_source=text,
            source_format=source_format,
            raw_structure_sha256=hashlib.sha256(raw).hexdigest(),
            parsed=parsed,
        )


def _safe_text(value: Any, *, field: str, maximum: int = 2048) -> str:
    if not isinstance(value, str):
        raise StructureSourceValidationError(f"{field} must be text")
    text = value.strip()
    if not text or len(text) > maximum or _CONTROL_RE.search(text):
        raise StructureSourceValidationError(f"{field} is empty, too long, or contains controls")
    if _FILESYSTEM_PATH_RE.search(text):
        raise StructureSourceValidationError(f"{field} must not contain a filesystem path")
    if _CREDENTIAL_RE.search(text):
        raise StructureSourceValidationError(f"{field} must not contain a credential")
    return text


def _safe_identifier(value: Any, *, field: str) -> str:
    text = _safe_text(value, field=field, maximum=160)
    if not _SAFE_ID_RE.fullmatch(text):
        raise StructureSourceValidationError(f"{field} must be a safe path-free identifier")
    return text


def _safe_token(value: Any, *, field: str = "token") -> str:
    if not isinstance(value, str) or not _SAFE_TOKEN_RE.fullmatch(value):
        raise StructureSourceValidationError(f"{field} must be a safe opaque token")
    return value


def _safe_formula(value: Any) -> str:
    text = _safe_text(value, field="formula", maximum=128)
    if not _FORMULA_RE.fullmatch(text):
        raise StructureSourceValidationError("formula contains unsupported characters")
    return text


def _safe_sha256(value: Any, *, field: str = "raw_structure_sha256") -> str:
    if not isinstance(value, str) or not _HEX_SHA256_RE.fullmatch(value):
        raise StructureSourceValidationError(f"{field} must be a SHA-256 digest")
    return value.lower()


def _safe_url(value: Any, *, field: str) -> str:
    text = _safe_text(value, field=field, maximum=2048)
    if not text.startswith("https://"):
        raise StructureSourceValidationError(f"{field} must use https")
    return text


def _license(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"name": _safe_text(value, field="license.name", maximum=160)}
    if not isinstance(value, Mapping):
        raise StructureSourceValidationError("license must be text or an object")
    allowed = {"name", "spdx_id", "url"}
    if not set(value).issubset(allowed) or "name" not in value:
        raise StructureSourceValidationError("license object has unexpected or missing fields")
    result = {"name": _safe_text(value["name"], field="license.name", maximum=160)}
    if value.get("spdx_id") is not None:
        result["spdx_id"] = _safe_identifier(value["spdx_id"], field="license.spdx_id")
    if value.get("url") is not None:
        result["url"] = _safe_url(value["url"], field="license.url")
    return result


def _citation(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"text": _safe_text(value, field="citation.text")}
    if not isinstance(value, Mapping):
        raise StructureSourceValidationError("citation must be text or an object")
    allowed = {"text", "doi", "url"}
    if not set(value).issubset(allowed) or "text" not in value:
        raise StructureSourceValidationError("citation object has unexpected or missing fields")
    result = {"text": _safe_text(value["text"], field="citation.text")}
    if value.get("doi") is not None:
        result["doi"] = _safe_text(value["doi"], field="citation.doi", maximum=256)
    if value.get("url") is not None:
        result["url"] = _safe_url(value["url"], field="citation.url")
    return result


def _metadata(value: Any, *, field: str = "method", depth: int = 0) -> Any:
    if depth > 4:
        raise StructureSourceValidationError(f"{field} exceeds the metadata depth limit")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructureSourceValidationError(f"{field} must contain finite numbers")
        return value
    if isinstance(value, str):
        return _safe_text(value, field=field)
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise StructureSourceValidationError(f"{field} contains too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _SAFE_METADATA_KEY_RE.fullmatch(key):
                raise StructureSourceValidationError(f"{field} contains an invalid metadata key")
            normalized = key.lower().replace("-", "_")
            if (
                normalized in _BAD_METADATA_KEYS
                or normalized.endswith("_path")
                or normalized.endswith("_file")
            ):
                raise StructureSourceValidationError(
                    f"{field}.{key} is not allowed at the public boundary")
            result[key] = _metadata(item, field=f"{field}.{key}", depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 64:
            raise StructureSourceValidationError(f"{field} contains too many values")
        return [_metadata(item, field=f"{field}[]", depth=depth + 1) for item in value]
    raise StructureSourceValidationError(f"{field} contains an unsupported metadata value")


def validate_source_result(
    value: Mapping[str, Any], *, expected_provider: str | None = None,
) -> dict[str, Any]:
    """验证并复制外部网关搜索结果；未知字段一律拒绝。"""
    if not isinstance(value, Mapping):
        raise StructureSourceValidationError("source result must be an object")
    fields = set(value)
    if fields != _RESULT_FIELDS:
        unexpected = sorted(fields - _RESULT_FIELDS)
        missing = sorted(_RESULT_FIELDS - fields)
        raise StructureSourceValidationError(
            f"source result has unexpected fields {unexpected} or missing fields {missing}")
    method = _metadata(value["method"])
    if not isinstance(method, dict):
        raise StructureSourceValidationError("method must be an object")
    provider = _safe_identifier(method.get("provider"), field="method.provider")
    if expected_provider is not None and provider != expected_provider:
        raise StructureSourceValidationError("method.provider does not match the selected provider")
    method["provider"] = provider
    return {
        "token": _safe_token(value["token"]),
        "source_id": _safe_identifier(value["source_id"], field="source_id"),
        "formula": _safe_formula(value["formula"]),
        "license": _license(value["license"]),
        "citation": _citation(value["citation"]),
        "method": method,
        "raw_structure_sha256": _safe_sha256(value["raw_structure_sha256"]),
    }


def _capability(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CAPABILITY_FIELDS:
        raise StructureSourceValidationError("gateway capability has unexpected or missing fields")
    provider = _safe_identifier(value["provider"], field="capability.provider")
    label = value["label"]
    if not isinstance(label, Mapping) or set(label) != {"zh", "en"}:
        raise StructureSourceValidationError("capability.label must contain exactly zh and en")
    modes = value["modes"]
    formats = value["formats"]
    if (
        isinstance(modes, (str, bytes)) or not isinstance(modes, Sequence)
        or not 1 <= len(modes) <= 8
    ):
        raise StructureSourceValidationError("capability.modes must be a bounded array")
    if (
        isinstance(formats, (str, bytes)) or not isinstance(formats, Sequence)
        or not 1 <= len(formats) <= 16
    ):
        raise StructureSourceValidationError("capability.formats must be a bounded array")
    normalized_modes = [_safe_identifier(item, field="capability.mode") for item in modes]
    normalized_formats = [_safe_identifier(item, field="capability.format") for item in formats]
    if not isinstance(value["network"], bool) or not isinstance(value["enabled"], bool):
        raise StructureSourceValidationError("capability network/enabled flags must be booleans")
    return {
        "provider": provider,
        "label": {
            "zh": _safe_text(label["zh"], field="capability.label.zh", maximum=120),
            "en": _safe_text(label["en"], field="capability.label.en", maximum=120),
        },
        "modes": normalized_modes,
        "formats": normalized_formats,
        "network": value["network"],
        "enabled": value["enabled"],
    }


def _utc_timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if not isinstance(value, datetime):
        raise StructureSourceError("utc_now must return datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StructureSourceSession:
    """单进程、单用户会话的来源 token 门面。

    source token 和 confirmation token 都有 TTL 与容量上限。确认在尚未消费时
    幂等；``resolve_confirmed`` 只成功一次。该类不落盘，也不输出本地路径。
    """

    def __init__(
        self,
        gateway: ExternalStructureGateway | Mapping[str, Callable[..., Any]] | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
        local_provider: StructureSourceProvider | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not math.isfinite(float(ttl_seconds)):
            raise ValueError("ttl_seconds must be a finite positive number")
        if float(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer")
        self._gateway = gateway
        self._ttl = float(ttl_seconds)
        self._max_tokens = max_tokens
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._local = local_provider or LocalStructureProvider()
        self._lock = threading.RLock()
        self._sources: dict[str, _SourceRecord] = {}
        self._confirmations: dict[str, _ConfirmationRecord] = {}

    def _gateway_method(self, name: str) -> Callable[..., Any]:
        if self._gateway is None:
            raise StructureSourceError("external structure gateway is unavailable")
        candidate = (
            self._gateway.get(name)
            if isinstance(self._gateway, Mapping)
            else getattr(self._gateway, name, None)
        )
        if not callable(candidate):
            raise StructureSourceError("external structure gateway is unavailable")
        return candidate

    def _prune_locked(self, now: float) -> None:
        for token, record in list(self._sources.items()):
            if record.expires_at <= now:
                self._sources.pop(token, None)
        for token, record in list(self._confirmations.items()):
            if record.expires_at <= now:
                self._confirmations.pop(token, None)

    @staticmethod
    def _evict_oldest(items: dict[str, Any], maximum: int) -> None:
        while len(items) >= maximum:
            oldest = min(items, key=lambda token: items[token].created_at)
            items.pop(oldest, None)

    def _new_token(self, prefix: str, existing: Mapping[str, Any]) -> str:
        token = prefix + secrets.token_urlsafe(24)
        while token in existing:
            token = prefix + secrets.token_urlsafe(24)
        return token

    def _record(self, token: str) -> _SourceRecord:
        try:
            token = _safe_token(token)
        except StructureSourceValidationError as exc:
            raise StructureSourceTokenError("source token is invalid or expired") from exc
        with self._lock:
            now = float(self._clock())
            self._prune_locked(now)
            record = self._sources.get(token)
        if record is None:
            raise StructureSourceTokenError("source token is invalid or expired")
        return record

    def capabilities(self) -> list[dict[str, Any]]:
        local = _capability(self._local.capability())
        if self._gateway is None:
            return [local]
        try:
            raw = self._gateway_method("capabilities")()
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise StructureSourceValidationError("gateway capabilities must be an array")
            external = [_capability(item) for item in raw]
            if any(item["provider"] == "local" for item in external):
                raise StructureSourceValidationError("gateway must not shadow the local provider")
            providers = [item["provider"] for item in external]
            if len(set(providers)) != len(providers):
                raise StructureSourceValidationError("gateway provider capabilities must be unique")
        except Exception:  # noqa: BLE001 - 外部故障不得破坏本地文件流程
            return [local]
        return [local, *external]

    def select_local(self, path: str | os.PathLike[str]) -> list[dict[str, Any]]:
        selected = self._local.select(path)
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            self._evict_oldest(self._sources, self._max_tokens)
            token = self._new_token("source.local.", self._sources)
            result = validate_source_result({
                "token": token,
                "source_id": f"local-{selected.raw_structure_sha256[:20]}",
                "formula": selected.parsed.formula,
                "license": {
                    "name": "user-provided; rights not inferred",
                },
                "citation": {
                    "text": "Local user-provided structure; no external citation inferred",
                },
                "method": {
                    "provider": "local",
                    "format": selected.source_format,
                    "parser": PARSER_VERSION,
                    "network": False,
                },
                "raw_structure_sha256": selected.raw_structure_sha256,
            }, expected_provider="local")
            provenance = {
                "provider": "local",
                "database_id": result["source_id"],
                "query": None,
                "retrieved_at_utc": _utc_timestamp(self._utc_now),
                "license": deepcopy(result["license"]),
                "citation": deepcopy(result["citation"]),
                "raw_structure_sha256": result["raw_structure_sha256"],
                "method": deepcopy(result["method"]),
            }
            self._sources[token] = _SourceRecord(
                token=token,
                result=result,
                provenance=provenance,
                created_at=now,
                expires_at=now + self._ttl,
                kind="local",
                local=selected,
            )
        return [deepcopy(result)]

    def search(self, provider: str, query: str) -> list[dict[str, Any]]:
        provider = _safe_identifier(provider, field="provider")
        if provider == "local":
            raise StructureSourceValidationError("local provider requires select_local(path)")
        query = _safe_text(query, field="query", maximum=512)
        try:
            raw = self._gateway_method("search")(provider, query)
        except StructureSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一成不泄露 URL/凭据的错误
            raise StructureSourceError("external structure gateway is unavailable") from exc
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise StructureSourceValidationError("gateway search response must be an array")
        if len(raw) > MAX_GATEWAY_RESULTS or len(raw) > self._max_tokens:
            raise StructureSourceValidationError("gateway returned too many source results")
        results = [validate_source_result(item, expected_provider=provider) for item in raw]
        tokens = [item["token"] for item in results]
        if len(tokens) != len(set(tokens)):
            raise StructureSourceValidationError("gateway result tokens must be unique")

        now = float(self._clock())
        retrieved_at = _utc_timestamp(self._utc_now)
        with self._lock:
            self._prune_locked(now)
            collision = set(tokens).intersection(self._sources)
            if collision:
                raise StructureSourceValidationError("gateway reused an active opaque result token")
            while len(self._sources) + len(results) > self._max_tokens:
                oldest = min(self._sources, key=lambda token: self._sources[token].created_at)
                self._sources.pop(oldest, None)
            for result in results:
                method = result["method"]
                provenance = {
                    "provider": method["provider"],
                    "database_id": result["source_id"],
                    "query": query,
                    "retrieved_at_utc": retrieved_at,
                    "license": deepcopy(result["license"]),
                    "citation": deepcopy(result["citation"]),
                    "raw_structure_sha256": result["raw_structure_sha256"],
                    "method": deepcopy(method),
                }
                self._sources[result["token"]] = _SourceRecord(
                    token=result["token"],
                    result=result,
                    provenance=provenance,
                    created_at=now,
                    expires_at=now + self._ttl,
                    kind="gateway",
                    query=query,
                )
        return deepcopy(results)

    @staticmethod
    def _verify_local(record: _SourceRecord) -> _ResolvedStructure:
        selected = record.local
        if selected is None:
            raise StructureSourceError("local source record is incomplete")
        try:
            raw, text = _read_local(selected.path)
        except (StructureSourceError, StructureSourceValidationError) as exc:
            raise StructureSourceChangedError(
                "local structure changed or became unavailable after selection") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != selected.raw_structure_sha256:
            raise StructureSourceChangedError("local structure changed after selection")
        return _ResolvedStructure(
            raw_source=text,
            source_format=selected.source_format,
            parsed=selected.parsed,
        )

    def _gateway_preview(self, record: _SourceRecord) -> _ResolvedStructure:
        try:
            raw = self._gateway_method("preview")(record.token)
        except StructureSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不传播外部 URL/凭据
            raise StructureSourceError("external structure gateway is unavailable") from exc
        if not isinstance(raw, Mapping) or set(raw) != _GATEWAY_PREVIEW_FIELDS:
            raise StructureSourceValidationError(
                "gateway preview has unexpected or missing fields")
        token = _safe_token(raw["token"])
        source_id = _safe_identifier(raw["source_id"], field="source_id")
        formula = _safe_formula(raw["formula"])
        digest = _safe_sha256(raw["raw_structure_sha256"])
        if (
            token != record.token
            or source_id != record.result["source_id"]
            or formula != record.result["formula"]
            or digest != record.result["raw_structure_sha256"]
        ):
            raise StructureSourceValidationError(
                "gateway preview identity does not match its search result")
        poscar = _safe_text(raw["poscar"], field="gateway preview POSCAR", maximum=MAX_SOURCE_BYTES)
        parsed = parse_structure_content(poscar, "poscar")
        return _ResolvedStructure(
            # 外部 preview 只接收 path-free POSCAR；不把未知远端原件冒充为已保存原件。
            raw_source=parsed.canonical_poscar,
            source_format="poscar",
            parsed=parsed,
        )

    def _resolve_for_preview(self, record: _SourceRecord) -> _ResolvedStructure:
        if record.kind == "local":
            return self._verify_local(record)
        if record.resolved is not None:
            return record.resolved
        resolved = self._gateway_preview(record)
        with self._lock:
            current = self._sources.get(record.token)
            if current is record and current.resolved is None:
                current.resolved = resolved
            elif current is None:
                raise StructureSourceTokenError("source token is invalid or expired")
            else:
                resolved = current.resolved or resolved
        return resolved

    @staticmethod
    def _preview_payload(record: _SourceRecord, resolved: _ResolvedStructure) -> dict[str, Any]:
        parsed = resolved.parsed
        return {
            "schema": "vcstudio.structure-source-preview/v1",
            "token": record.token,
            "source_id": record.result["source_id"],
            "formula": record.result["formula"],
            "raw_structure_sha256": record.result["raw_structure_sha256"],
            "structure_sha256": parsed.structure_sha256,
            "structure": parsed.public_structure(),
            "view": structure_view(parsed.canonical_poscar),
            "provenance": deepcopy(record.provenance),
        }

    def preview(self, token: str) -> dict[str, Any]:
        record = self._record(token)
        resolved = self._resolve_for_preview(record)
        return self._preview_payload(record, resolved)

    @staticmethod
    def _confirmation_payload(record: _SourceRecord, confirmation_token: str) -> dict[str, Any]:
        if record.resolved is None:
            raise StructureSourceError("confirmed source has no resolved structure")
        return {
            "schema": "vcstudio.structure-source-confirmation/v1",
            "confirmed": True,
            "confirmation_token": confirmation_token,
            "source_token": record.token,
            "source": deepcopy(record.result),
            "source_id": record.result["source_id"],
            "formula": record.result["formula"],
            "raw_structure_sha256": record.result["raw_structure_sha256"],
            "structure_sha256": record.resolved.parsed.structure_sha256,
        }

    def confirm(self, token: str) -> dict[str, Any]:
        record = self._record(token)
        with self._lock:
            if record.confirmation_token is not None:
                existing = self._confirmations.get(record.confirmation_token)
                if existing is None:
                    raise StructureSourceTokenError(
                        "confirmation token is invalid, expired, or consumed")
                return self._confirmation_payload(record, record.confirmation_token)

        resolved = self._resolve_for_preview(record)
        # local 必须在确认点再次重读；gateway 使用本会话内已冻结的 preview。
        if record.kind == "local":
            resolved = self._verify_local(record)
        with self._lock:
            now = float(self._clock())
            self._prune_locked(now)
            if self._sources.get(record.token) is not record:
                raise StructureSourceTokenError("source token is invalid or expired")
            if record.confirmation_token is None:
                self._evict_oldest(self._confirmations, self._max_tokens)
                confirmation_token = self._new_token("confirm.", self._confirmations)
                record.resolved = resolved
                record.confirmation_token = confirmation_token
                self._confirmations[confirmation_token] = _ConfirmationRecord(
                    token=confirmation_token,
                    source=record,
                    resolved=resolved,
                    created_at=now,
                    expires_at=now + self._ttl,
                )
            else:
                confirmation_token = record.confirmation_token
        return self._confirmation_payload(record, confirmation_token)

    def resolve_confirmed(self, token: str) -> dict[str, Any]:
        try:
            token = _safe_token(token, field="confirmation_token")
        except StructureSourceValidationError as exc:
            raise StructureSourceTokenError(
                "confirmation token is invalid, expired, or consumed") from exc
        with self._lock:
            now = float(self._clock())
            self._prune_locked(now)
            confirmation = self._confirmations.get(token)
        if confirmation is None:
            raise StructureSourceTokenError(
                "confirmation token is invalid, expired, or consumed")

        record = confirmation.source
        resolved = confirmation.resolved
        if record.kind == "local":
            resolved = self._verify_local(record)
            if resolved.parsed.structure_sha256 != confirmation.resolved.parsed.structure_sha256:
                raise StructureSourceChangedError("local structure changed after confirmation")

        with self._lock:
            current = self._confirmations.pop(token, None)
            if current is not confirmation:
                raise StructureSourceTokenError(
                    "confirmation token is invalid, expired, or consumed")
            if self._sources.get(record.token) is record:
                self._sources.pop(record.token, None)
        parsed = resolved.parsed
        return {
            "schema": "vcstudio.structure-source-resolved/v1",
            "confirmation_token": token,
            "source_token": record.token,
            "source_id": record.result["source_id"],
            "formula": record.result["formula"],
            "source_format": resolved.source_format,
            "raw_source": resolved.raw_source,
            "poscar": parsed.canonical_poscar,
            "raw_structure_sha256": record.result["raw_structure_sha256"],
            "structure_sha256": parsed.structure_sha256,
            "structure": parsed.public_structure(),
            "provenance": deepcopy(record.provenance),
        }


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TTL_SECONDS",
    "ExternalStructureGateway",
    "LocalStructureProvider",
    "ParsedStructure",
    "StructureSourceChangedError",
    "StructureSourceError",
    "StructureSourceProvider",
    "StructureSourceSession",
    "StructureSourceTokenError",
    "StructureSourceValidationError",
    "parse_structure_content",
    "validate_source_result",
]
