"""Canonical identities for VASP text whose formatting is not semantic."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib


def canonical_vasp_token(token: str) -> str:
    """Normalize a numeric token exactly; case-fold non-numeric keywords."""
    raw = str(token).strip()
    try:
        number = Decimal(raw.replace('D', 'E').replace('d', 'e'))
    except InvalidOperation:
        return raw.upper()
    if not number.is_finite():
        return raw.upper()
    if number.is_zero():
        return '0'
    rendered = format(number.normalize(), 'f')
    if '.' in rendered:
        rendered = rendered.rstrip('0').rstrip('.')
    return rendered


def canonical_kpoints_effective_text(text: str) -> str:
    """Return a formatting-insensitive identity for effective KPOINTS lines.

    The first line is a free-form comment.  Remaining keywords are
    case-insensitive and numeric spellings such as ``0.5``/``0.500000`` (and
    signed zero) are equivalent.  Inline ``!``/``#`` comments are ignored.
    """
    records = []
    for raw_line in str(text or '').splitlines()[1:]:
        effective = raw_line.split('!', 1)[0].split('#', 1)[0].strip()
        if not effective:
            continue
        records.append(' '.join(
            canonical_vasp_token(token) for token in effective.split()))
    return '\n'.join(records)


def canonical_kpoints_sha256(text: str) -> str:
    effective = canonical_kpoints_effective_text(text)
    return hashlib.sha256(effective.encode('utf-8')).hexdigest()


__all__ = [
    'canonical_kpoints_effective_text', 'canonical_kpoints_sha256',
    'canonical_vasp_token',
]
