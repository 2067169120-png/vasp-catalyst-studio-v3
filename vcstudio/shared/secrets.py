"""集群密码的安全存取:走 keyring(Windows 凭据库)。绝不明文落 yaml。

keyring 不可用(未装/无后端)时全部安全降级:set 返回 False、get 返回 None,
由调用方转为"每次现输"。中文注释允许,英文标识符。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

try:
    import keyring  # type: ignore
except Exception:  # pragma: no cover - 环境相关
    keyring = None  # type: ignore

SERVICE = 'vcstudio-cluster'


# One deny-by-default credential classifier is available to persisted and
# public-data boundaries.  Callers may additionally classify bare sensitive
# field names when walking dictionary keys; prose fields normally use value
# matching only so ordinary sentences containing words such as "token" remain
# usable.
_CREDENTIAL_PATTERNS = (
    ('GitHub PAT', re.compile(
        r'\b(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9_-]{12,})')),
    ('GitLab PAT', re.compile(r'\bglpat-[A-Za-z0-9_-]{8,}')),
    ('Hugging Face token', re.compile(r'\bhf_[A-Za-z0-9]{12,}')),
    ('OpenAI token', re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')),
    ('Stripe token', re.compile(
        r'\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|whsec_[A-Za-z0-9]{8,})')),
    ('Slack token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{8,}', re.I)),
    ('AWS access key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('Google API key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('Google OAuth token', re.compile(r'\bya29\.[0-9A-Za-z_-]{16,}\b')),
    ('JSON Web Token', re.compile(
        r'\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\b')),
    ('SendGrid token', re.compile(
        r'\bSG\.[0-9A-Za-z_-]{12,}\.[0-9A-Za-z_-]{12,}\b')),
    ('npm token', re.compile(r'\bnpm_[0-9A-Za-z]{16,}\b')),
    ('PyPI token', re.compile(r'\bpypi-[0-9A-Za-z_-]{16,}\b')),
    ('Bearer credential', re.compile(r'\bBearer\s+[^\s,;]+', re.I)),
    ('private key', re.compile(
        r'-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----', re.I)),
    ('credential assignment', re.compile(
        r'\b(?:password|passwd|pwd|secret|token|credential|authorization|'
        r'api[_ -]?key|access[_ -]?key|account[_ -]?key|shared[_ -]?access[_ -]?key|'
        r'client[_ -]?secret|connection[_ -]?string|sas[_ -]?token)'
        r'\s*[:=]\s*[\"\']?[^\s,;\"\'}]+',
        re.I)),
    # Userinfo is credential-shaped even without a colon and regardless of the
    # URI scheme (ssh, ftp, postgres, custom transports, and so on).
    ('URI userinfo', re.compile(
        r'\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+(?::[^\s/@]*)?@[^\s/]+')),
)
_SENSITIVE_FIELD = re.compile(
    r'(?i)(?:pass(?:word|wd|phrase)|secret|credential|authorization|cookie|'
    r'api[_ -]?key|access[_ -]?key|account[_ -]?key|shared[_ -]?access[_ -]?key|'
    r'private[_ -]?key|client[_ -]?secret|connection[_ -]?string|sas[_ -]?token|'
    r'(?<![A-Za-z0-9_])'
    r'token(?![A-Za-z0-9_]))')


def classify_credential(text: str, *, include_field_names: bool = False) -> str | None:
    """Return a stable credential category, or ``None`` when no value matches."""

    value = str(text or '')
    if include_field_names and _SENSITIVE_FIELD.search(value):
        return 'credential field'
    for description, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(value):
            return description
    return None


def contains_credential(text: str, *, include_field_names: bool = False) -> bool:
    """Whether *text* contains credential-shaped material."""

    return classify_credential(text, include_field_names=include_field_names) is not None


def redact_credentials(text: str, *, replacement: str = '[redacted-secret]') -> str:
    """Replace every credential-shaped value in text with a fixed marker."""

    value = str(text or '')
    for _description, pattern in _CREDENTIAL_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def classify_credential_structure(value: Any) -> str | None:
    """Classify credentials in nested public/persisted data, including field names.

    Binary attachment bodies are deliberately not decoded.  Callers must keep
    those bytes local and separately bound by their content digest.
    """

    if isinstance(value, str):
        return classify_credential(value)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                hit = classify_credential(key, include_field_names=True)
                if hit:
                    return hit
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
                                replacement: str = '[redacted-secret]') -> Any:
    """Recursively redact credential values and values under sensitive keys."""

    if isinstance(value, str):
        return redact_credentials(value, replacement=replacement)
    if isinstance(value, Mapping):
        public = {}
        for key, child in value.items():
            public_key = str(key)
            if classify_credential(public_key, include_field_names=True):
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


def available() -> bool:
    """keyring 后端是否可用。"""
    return keyring is not None


def set_password(profile: str, password: str) -> bool:
    """存密码到凭据库,键=profile 名。成功 True;keyring 不可用/失败 False。"""
    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE, profile, password)
        return True
    except Exception:
        return False


def get_password(profile: str) -> str | None:
    """取密码;无/不可用/异常 → None。"""
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE, profile)
    except Exception:
        return None


def delete_password(profile: str) -> None:
    """删密码;不可用或不存在都静默。"""
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, profile)
    except Exception:
        pass
