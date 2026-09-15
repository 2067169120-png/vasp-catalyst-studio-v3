"""Project-wide credential and local-path classification primitives.

Credential recognition lives here so persisted-data guards and public DTO
redactors cannot silently diverge. Callers still choose whether a match is
rejected or replaced. The local-path helpers provide the same reusable
contract for browser-facing text without treating remote URLs as local paths.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential assignment", re.compile(
        r"(?i)(?<![A-Za-z0-9_])[\"']?(?:password|passwd|pwd|secret|token|"
        r"credential|credentials|auth|authorization|cookie|api[-_ ]?key|"
        r"access[-_ ]?key|account[-_ ]?key|shared[-_ ]?access[-_ ]?key|"
        r"private[-_ ]?key|client[-_ ]?secret|access[-_ ]?token|"
        r"refresh[-_ ]?token|connection[-_ ]?string|sas[-_ ]?token|"
        r"aws[-_ ]?(?:access[-_ ]?key[-_ ]?id|secret[-_ ]?access[-_ ]?key))"
        r"[\"']?\s*[:=]\s*(?:[\"'][^\"'\r\n]+[\"']|[^\s,;&}\]\"']+)"
    )),
    ("GitHub PAT", re.compile(
        r"\b(?:github_pat_|gh[opusr]_)[A-Za-z0-9_-]{8,}")),
    ("GitLab PAT", re.compile(r"\bglpat-[A-Za-z0-9_-]{8,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9_-]{8,}")),
    ("OpenAI token", re.compile(
        r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}")),
    ("Stripe token", re.compile(
        r"\b(?:(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9_-]{8,}|"
        r"whsec_[A-Za-z0-9_-]{8,})")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}", re.I)),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_-]{16,}\b")),
    ("JSON Web Token", re.compile(
        r"\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\."
        r"[0-9A-Za-z_-]{8,}\b")),
    ("SendGrid token", re.compile(
        r"\bSG\.[0-9A-Za-z_-]{12,}\.[0-9A-Za-z_-]{12,}\b")),
    ("npm token", re.compile(r"\bnpm_[0-9A-Za-z]{16,}\b")),
    ("PyPI token", re.compile(r"\bpypi-[0-9A-Za-z_-]{16,}\b")),
    ("Bearer credential", re.compile(r"\bBearer\s+[^\s,;]+", re.I)),
    ("private key", re.compile(
        r"-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----", re.I)),
    ("URI userinfo", re.compile(
        r"(?i)(?<![A-Za-z0-9+.-])[A-Z][A-Z0-9+.-]*://"
        r"[^\s/@]+(?::[^\s/@]*)?@[^\s/]+")),
)

_SENSITIVE_KEY_WORDS = frozenset({
    "password", "passwd", "secret", "token", "credential", "credentials",
    "auth", "authorization", "cookie", "cookies", "pwd",
})
_SENSITIVE_KEY_NAMES = frozenset({
    "key", "apikey", "api_key", "accesskey", "access_key", "privatekey",
    "private_key", "secretkey", "secret_key", "clientsecret", "client_secret",
    "accesstoken", "access_token", "refreshtoken", "refresh_token", "sshkey",
    "ssh_key", "signingkey", "signing_key", "encryptionkey", "encryption_key",
    "awssecretaccesskey", "aws_secret_access_key", "connectionstring",
    "connection_string", "sastoken", "sas_token", "accountkey", "account_key",
    "sharedaccesskey", "shared_access_key",
})
_LEGACY_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(?:pass(?:word|wd|phrase)|secret|credential|authorization|cookie|"
    r"api[_ -]?key|access[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"account[_ -]?key|"
    r"shared[_ -]?access[_ -]?key|private[_ -]?key|client[_ -]?secret|"
    r"connection[_ -]?string|sas[_ -]?token|(?<![A-Za-z0-9_])"
    r"token(?![A-Za-z0-9_]))"
)

# Assignment punctuation is deliberately accepted immediately before a path.
# URL authority/path separators remain excluded, so https:// and s3:// values
# are not classified as local paths.
_ASSIGNED_LOCAL_PATH_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>(?<![A-Za-z0-9_])[A-Z_][A-Z0-9_.-]*\s*[:=]\s*)"
    r"(?:"
    r"\"(?=(?:file:(?://+|\\+)|[a-z]:[\\/]|\\\\|/(?!/)))[^\"\r\n]*\""
    r"|'(?=(?:file:(?://+|\\+)|[a-z]:[\\/]|\\\\|/(?!/)))[^'\r\n]*'"
    r"|(?=(?:file:(?://+|\\+)|[a-z]:[\\/]|\\\\|/(?!/)))"
    r"[^,;，；\r\n]+"
    r")"
)
_LOCAL_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\bfile:(?://+|\\+)[^\s,;，；\"'<>\)\]\}]+"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:[a-z]:[\\/]|\\\\)"
        r"[^\s,;，；\"'<>\)\]\}]+"
    ),
    re.compile(
        r"(?<![A-Za-z0-9/])/(?!/)[^\s,;，；\"'<>\)\]\}]+"
    ),
)


def _normalise_key(value: object) -> str:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")


def is_sensitive_key(value: object) -> bool:
    """Return whether a mapping key names credential-bearing data."""

    normalised = _normalise_key(value)
    if normalised in _SENSITIVE_KEY_NAMES:
        return True
    if set(normalised.split("_")) & _SENSITIVE_KEY_WORDS:
        return True
    compact = normalised.replace("_", "")
    if any(compound in compact for compound in (
        "apikey", "accesskey", "secretkey", "privatekey", "clientsecret",
        "accesstoken", "refreshtoken", "sshkey", "signingkey",
        "encryptionkey", "awssecretaccesskey", "connectionstring", "sastoken",
        "accountkey", "sharedaccesskey",
    )):
        return True
    return any(compact.endswith(word) for word in (
        "password", "passwd", "secret", "token", "credential", "credentials",
        "authorization", "cookie", "auth",
    ))


def classify_credential(value: object, *, include_field_names: bool = False) \
        -> str | None:
    """Return a stable credential category, or ``None`` when no value matches.

    ``include_field_names`` retains the historic campaign-ledger prose scan.
    Structured mappings use :func:`is_sensitive_key` for their actual keys.
    """

    text = str(value or "")
    if include_field_names and _LEGACY_SENSITIVE_FIELD_RE.search(text):
        return "credential field"
    for description, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return description
    return None


def looks_like_credential(value: object) -> bool:
    """Return whether *value* contains a recognized credential shape."""

    return classify_credential(value) is not None


def redact_credentials(value: object, *, replacement: str = "[redacted-secret]") \
        -> str:
    """Replace all classified credential substrings with a fixed marker."""

    redacted = str(value or "")
    for _description, pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def classify_credential_structure(value: Any) -> str | None:
    """Classify credentials in nested data, including sensitive mapping keys."""

    if isinstance(value, str):
        return classify_credential(value)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if is_sensitive_key(key):
                return "credential field"
            key_hit = classify_credential(key)
            if key_hit:
                return key_hit
            hit = classify_credential_structure(child)
            if hit:
                return hit
        return None
    if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, memoryview)):
        for child in value:
            hit = classify_credential_structure(child)
            if hit:
                return hit
    return None


def redact_credential_structure(value: Any, *,
                                replacement: str = "[redacted-secret]") -> Any:
    """Recursively redact classified values and values under sensitive keys."""

    if isinstance(value, str):
        return redact_credentials(value, replacement=replacement)
    if isinstance(value, Mapping):
        public = {}
        for key, child in value.items():
            public_key = str(key)
            if classify_credential(key):
                public[replacement] = replacement
            elif is_sensitive_key(key):
                public[public_key] = replacement
            else:
                public[public_key] = redact_credential_structure(
                    child, replacement=replacement)
        return public
    if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray, memoryview)):
        return [
            redact_credential_structure(child, replacement=replacement)
            for child in value
        ]
    return value


def contains_local_path(value: object) -> bool:
    """Return whether text contains a local absolute path or ``file:`` URI."""

    text = str(value or "")
    return any(pattern.search(text) for pattern in _LOCAL_PATH_PATTERNS)


def redact_local_paths(value: object, *,
                       replacement: str = "[redacted-local-path]") -> str:
    """Replace local paths anywhere in text while preserving assignment keys."""

    redacted = str(value or "")
    redacted = _ASSIGNED_LOCAL_PATH_RE.sub(
        lambda match: f"{match.group('prefix')}{replacement}", redacted)
    for pattern in _LOCAL_PATH_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


__all__ = [
    "classify_credential",
    "classify_credential_structure",
    "contains_local_path",
    "is_sensitive_key",
    "looks_like_credential",
    "redact_credential_structure",
    "redact_credentials",
    "redact_local_paths",
]
