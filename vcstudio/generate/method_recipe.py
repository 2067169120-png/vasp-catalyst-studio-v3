"""Versioned Method Recipe authority for already-made builder decisions.

This module does not choose INCAR values, pseudopotentials or k-point policy.
The established builders remain the decision makers.  It only canonicalises
their explicit decision record and issues the semantic binding consumed by the
strict scientific fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


METHOD_RECIPE_SCHEMA = "vcstudio.method-recipe/v1"
METHOD_RECIPE_AUTHORITY = "vcstudio.method-recipe-authority/v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def bind_method_recipe(decisions: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(decisions, Mapping) or not decisions:
        raise ValueError("method recipe decisions must be a non-empty mapping")
    payload = {
        "schema": METHOD_RECIPE_SCHEMA,
        "authority": METHOD_RECIPE_AUTHORITY,
        "decisions": dict(decisions),
    }
    return {
        "schema": METHOD_RECIPE_SCHEMA,
        "authority": METHOD_RECIPE_AUTHORITY,
        "semantic_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        "decisions": dict(decisions),
    }


def bind_semantic_recipe(semantic_sha256: str, *,
                         metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bind a canonical semantic hash issued by the Method Recipe authority.

    The caller owns the scientific recipe decisions.  This adapter deliberately
    does not reproduce or reinterpret those decisions; it only carries their
    versioned semantic identity into the job manifest.
    """
    digest = str(semantic_sha256 or "").strip().lower()
    if not _HEX64_RE.fullmatch(digest):
        raise ValueError("method recipe semantic SHA256 is invalid")
    result: dict[str, Any] = {
        "schema": METHOD_RECIPE_SCHEMA,
        "authority": METHOD_RECIPE_AUTHORITY,
        "semantic_sha256": digest,
    }
    if metadata:
        result["metadata"] = dict(metadata)
    return result


def validate_method_recipe(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("method recipe must be a mapping")
    schema = str(value.get("schema") or "").strip()
    authority = str(value.get("authority") or "").strip()
    digest = str(value.get("semantic_sha256") or "").strip().lower()
    if schema != METHOD_RECIPE_SCHEMA or authority != METHOD_RECIPE_AUTHORITY:
        raise ValueError("method recipe authority/schema is invalid")
    if not _HEX64_RE.fullmatch(digest):
        raise ValueError("method recipe semantic SHA256 is invalid")
    result = dict(value)
    result.update({
        "schema": METHOD_RECIPE_SCHEMA,
        "authority": METHOD_RECIPE_AUTHORITY,
        "semantic_sha256": digest,
    })
    return result


def builder_recipe(*, builder: str, task_type: str, calc_type: str,
                   validate: bool, completions: Mapping[str, Any],
                   kpoints_source: str, extra: Mapping[str, Any] | None = None
                   ) -> dict[str, Any]:
    decisions = {
        "builder": str(builder),
        "engine": "vasp",
        "task_type": str(task_type),
        "calc_type": str(calc_type),
        "validation_enabled": bool(validate),
        "completions": dict(completions or {}),
        "kpoints_source": str(kpoints_source),
    }
    if extra:
        decisions["extra"] = dict(extra)
    return bind_method_recipe(decisions)


__all__ = [
    "METHOD_RECIPE_AUTHORITY", "METHOD_RECIPE_SCHEMA", "bind_method_recipe",
    "bind_semantic_recipe", "builder_recipe", "validate_method_recipe",
]
