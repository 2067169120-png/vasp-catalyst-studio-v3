"""Project-wide credential shape classifier.

This module only classifies key names and string values.  Callers keep their
own policy for rejection versus redaction and for filesystem paths.
"""
from __future__ import annotations

import re


_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:github_pat_|gh[opusr]_)[A-Za-z0-9_-]{8,}"
    r"|\b(?:glpat-|hf_|sk_live_|pk_live_)[A-Za-z0-9_-]{8,}"
    r"|\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\bBearer\s+\S+"
    r"|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----"
    r")"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[\"']?(?:password|passwd|pwd|secret|token|credential|"
    r"credentials|auth|authorization|cookie|api[-_ ]?key|access[-_ ]?key|"
    r"private[-_ ]?key|client[-_ ]?secret|access[-_ ]?token|"
    r"refresh[-_ ]?token|aws[-_ ]?(?:access[-_ ]?key[-_ ]?id|"
    r"secret[-_ ]?access[-_ ]?key))[\"']?\s*[:=]\s*[\"']?[^\s,;&}\]]+"
)
_URL_USERINFO_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9+.-])[A-Z][A-Z0-9+.-]*://"
    r"[^\s/@]+(?::[^\s/@]*)?@[^\s/]+"
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
    "awssecretaccesskey", "aws_secret_access_key",
})


def looks_like_credential(value: object) -> bool:
    """Return whether *value* contains a recognized credential shape."""

    text = str(value or "")
    return bool(
        _CREDENTIAL_VALUE_RE.search(text)
        or _CREDENTIAL_ASSIGNMENT_RE.search(text)
        or _URL_USERINFO_RE.search(text)
    )


def is_sensitive_key(value: object) -> bool:
    """Return whether a mapping key names credential-bearing data."""

    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    normalised = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
    if normalised in _SENSITIVE_KEY_NAMES:
        return True
    if set(normalised.split("_")) & _SENSITIVE_KEY_WORDS:
        return True
    compact = normalised.replace("_", "")
    if any(compound in compact for compound in (
        "apikey", "accesskey", "secretkey", "privatekey", "clientsecret",
        "accesstoken", "refreshtoken", "sshkey", "signingkey",
        "encryptionkey", "awssecretaccesskey",
    )):
        return True
    return any(compact.endswith(word) for word in (
        "password", "passwd", "secret", "token", "credential", "credentials",
        "authorization", "cookie", "auth",
    ))


__all__ = ["is_sensitive_key", "looks_like_credential"]
