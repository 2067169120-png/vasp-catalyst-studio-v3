"""集群密码的安全存取:走 keyring(Windows 凭据库)。绝不明文落 yaml。

keyring 不可用(未装/无后端)时全部安全降级:set 返回 False、get 返回 None,
由调用方转为"每次现输"。中文注释允许,英文标识符。
"""
from __future__ import annotations

from typing import Any

from vcstudio.shared.credential_classifier import (
    classify_credential as _classify_credential,
    classify_credential_structure as _classify_credential_structure,
    redact_credential_structure as _redact_credential_structure,
    redact_credentials as _redact_credentials,
)

try:
    import keyring  # type: ignore
except Exception:  # pragma: no cover - 环境相关
    keyring = None  # type: ignore

SERVICE = 'vcstudio-cluster'
EXTERNAL_REFERENCE_SERVICE = 'vcstudio-external-reference'


def classify_credential(text: str, *, include_field_names: bool = False) -> str | None:
    """Compatibility adapter for the project-wide credential classifier."""

    return _classify_credential(text, include_field_names=include_field_names)


def contains_credential(text: str, *, include_field_names: bool = False) -> bool:
    """Whether *text* contains credential-shaped material."""

    return classify_credential(text, include_field_names=include_field_names) is not None


def redact_credentials(text: str, *, replacement: str = '[redacted-secret]') -> str:
    """Compatibility adapter for central credential redaction."""

    return _redact_credentials(text, replacement=replacement)


def classify_credential_structure(value: Any) -> str | None:
    """Compatibility adapter for central structured classification."""

    return _classify_credential_structure(value)


def redact_credential_structure(value: Any, *,
                                replacement: str = '[redacted-secret]') -> Any:
    """Compatibility adapter for central structured redaction."""

    return _redact_credential_structure(value, replacement=replacement)


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


def set_external_reference_api_key(provider: str, api_key: str) -> bool:
    """把外部参考库 API key 写入独立 keyring 命名空间。"""
    if keyring is None:
        return False
    try:
        keyring.set_password(
            EXTERNAL_REFERENCE_SERVICE, str(provider), str(api_key))
        return True
    except Exception:
        return False


def get_external_reference_api_key(provider: str) -> str | None:
    """读取外部参考库 API key；绝不从环境变量或配置文件回退。"""
    if keyring is None:
        return None
    try:
        return keyring.get_password(
            EXTERNAL_REFERENCE_SERVICE, str(provider))
    except Exception:
        return None


def delete_external_reference_api_key(provider: str) -> None:
    """删除外部参考库 API key；不存在或后端不可用时静默。"""
    if keyring is None:
        return
    try:
        keyring.delete_password(
            EXTERNAL_REFERENCE_SERVICE, str(provider))
    except Exception:
        pass
