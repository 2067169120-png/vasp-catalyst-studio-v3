from __future__ import annotations

import pytest

from vcstudio.shared.credential_classifier import is_sensitive_key, looks_like_credential


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
