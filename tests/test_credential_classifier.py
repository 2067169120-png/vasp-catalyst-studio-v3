from __future__ import annotations

import pytest

from vcstudio.shared.credential_classifier import (
    classify_credential,
    classify_credential_structure,
    contains_local_path,
    is_sensitive_key,
    looks_like_credential,
    redact_credential_structure,
    redact_credentials,
    redact_local_paths,
)
from vcstudio.shared.secrets import (
    classify_credential as classify_credential_adapter,
    redact_credentials as redact_credentials_adapter,
)


@pytest.mark.parametrize("value", [
    "AKIA1234567890ABCDEF",
    "ASIA1234567890ABCDEF",
    "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "glpat-abcdefghijklmnopqrstuvwxyz",
    "hf_abcdefghijklmnopqrstuvwxyz",
    "sk_live_abcdefghijklmnopqrstuvwxyz",
    "pk_live_abcdefghijklmnopqrstuvwxyz",
    "github_pat_abcdefghijklmnopqrstuvwxyz",
    "ghp_abcdefghijklmnopqrstuvwxyz",
    "sk-proj-abcdefghijklmnopqrstuvwxyz",
    "AIza" + "A" * 35,
    "ya29." + "a" * 24,
    "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
    "Bearer abc.def.ghi",
    "client_secret=client-value",
    "access_token=access-value",
    "refresh_token=refresh-value",
    '{"client_secret":"client-value"}',
    "{'access_token':'access-value'}",
    '{"aws_secret_access_key":"example-secret"}',
    "ssh://user:password@example.invalid/repository",
    "x://user:password@example.invalid/resource",
    f"{'a' * 80}://user:password@example.invalid/resource",
])
def test_project_wide_classifier_recognizes_supported_credential_shapes(value):
    assert looks_like_credential(value)


@pytest.mark.parametrize("key", [
    "client_secret",
    "clientSecret",
    "access_token",
    "refresh_token",
    "aws_secret_access_key",
    "github_api_key",
])
def test_project_wide_classifier_recognizes_sensitive_keys(key):
    assert is_sensitive_key(key)


@pytest.mark.parametrize("value", [
    "evidence-001",
    "https://docs.example.invalid/reference",
    "recipe_default",
])
def test_project_wide_classifier_preserves_safe_public_values(value):
    assert not looks_like_credential(value)


@pytest.mark.parametrize("value", [
    "access_token=access-value",
    "refresh_token:refresh-value",
    "AIza" + "D" * 35,
    "ya29." + "e" * 24,
    "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
    "custom://alice:supersecret@example.invalid/resource",
])
def test_central_classifier_drives_compatibility_adapter_and_redaction(value):
    category = classify_credential(value)
    assert category is not None
    assert classify_credential_adapter(value) == category
    assert value not in redact_credentials(f"prefix {value} suffix")
    assert value not in redact_credentials_adapter(f"prefix {value} suffix")


def test_structured_classifier_rejects_and_redacts_sensitive_fields_and_keys():
    secret = "access-value"
    value = {
        "nested": {"access_token": secret},
        "items": [{"refreshToken": "refresh-value"}],
    }
    assert classify_credential_structure(value) == "credential field"
    public = redact_credential_structure(value)
    assert public["nested"]["access_token"] == "[redacted-secret]"
    assert public["items"][0]["refreshToken"] == "[redacted-secret]"
    assert secret not in str(public)


@pytest.mark.parametrize("key", [
    "access_token", "accessToken", "refresh_token", "refreshToken",
])
def test_compatibility_adapter_recognizes_new_central_sensitive_fields(key):
    assert classify_credential_adapter(
        key, include_field_names=True) == "credential field"


@pytest.mark.parametrize(("value", "expected"), [
    ("path=/home/alice/private/OUTCAR",
     "path=[redacted-local-path]"),
    (r"path=C:\Users\alice\private\OUTCAR",
     "path=[redacted-local-path]"),
    ("root:/srv/research/private/job.yaml",
     "root:[redacted-local-path]"),
    (r"artifact=C:\Users\alice\Private Project\result.pdf",
     "artifact=[redacted-local-path]"),
    ("source=file:///home/alice/private/POSCAR",
     "source=[redacted-local-path]"),
])
def test_local_path_contract_redacts_assignments_anywhere(value, expected):
    assert contains_local_path(value)
    assert redact_local_paths(value) == expected


@pytest.mark.parametrize("value", [
    "https://example.invalid/research/OUTCAR",
    "s3://public-bucket/research/OUTCAR",
    "doi:10.1000/example",
])
def test_local_path_contract_preserves_remote_and_opaque_references(value):
    assert not contains_local_path(value)
    assert redact_local_paths(value) == value
