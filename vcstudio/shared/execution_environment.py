"""Validated execution-environment bindings from explicit trusted evidence."""
from __future__ import annotations

import re
from typing import Any, Mapping


EXECUTION_ENVIRONMENT_SCHEMA = "vcstudio.execution-environment/v1"
EXECUTION_ENVIRONMENT_AUTHORITY = "vcstudio.execution-environment-authority/v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_execution_environment(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("execution environment must be a mapping")
    schema = str(value.get("schema") or "").strip()
    authority = str(value.get("authority") or "").strip()
    engine = str(value.get("engine") or "vasp").strip().lower()
    version = str(value.get("vasp_version") or "").strip()
    build_identity = str(value.get("build_identity") or "").strip()
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    evidence_kind = str(evidence.get("kind") or "").strip()
    evidence_sha256 = str(evidence.get("sha256") or "").strip().lower()
    if schema != EXECUTION_ENVIRONMENT_SCHEMA:
        raise ValueError("execution environment schema is not authoritative")
    if authority != EXECUTION_ENVIRONMENT_AUTHORITY:
        raise ValueError("execution environment authority is missing")
    if engine != "vasp" or not version or not build_identity:
        raise ValueError("VASP version/build identity is incomplete")
    if not evidence_kind or not _HEX64_RE.fullmatch(evidence_sha256):
        raise ValueError("execution environment evidence is incomplete")
    result = {
        "schema": schema, "authority": authority, "engine": engine,
        "vasp_version": version, "build_identity": build_identity,
        "evidence": {"kind": evidence_kind, "sha256": evidence_sha256},
    }
    return result


def from_cluster_profile(profile: Any) -> dict[str, Any] | None:
    version = str(getattr(profile, "vasp_version", "") or "").strip()
    build_identity = str(getattr(profile, "vasp_build_identity", "") or "").strip()
    evidence_sha256 = str(
        getattr(profile, "vasp_build_evidence_sha256", "") or "").strip().lower()
    if not any((version, build_identity, evidence_sha256)):
        return None
    return validate_execution_environment({
        "schema": EXECUTION_ENVIRONMENT_SCHEMA,
        "authority": EXECUTION_ENVIRONMENT_AUTHORITY,
        "engine": "vasp",
        "vasp_version": version,
        "build_identity": build_identity,
        "evidence": {
            "kind": "cluster-profile-attestation",
            "sha256": evidence_sha256,
        },
    })


__all__ = [
    "EXECUTION_ENVIRONMENT_AUTHORITY", "EXECUTION_ENVIRONMENT_SCHEMA",
    "from_cluster_profile", "validate_execution_environment",
]
