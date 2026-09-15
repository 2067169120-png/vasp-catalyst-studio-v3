"""Role-aware method-comparability policy for adsorption-energy operands.

This module deliberately does not decide whether an individual VASP job is
valid or submit-ready.  Local input validation owns that decision.  Here we
only compare two already-normalized method plans and report whether their
energies can be combined automatically.

The policy is intentionally independent from the web API so it can be reused
by preparation and result-time gates without importing GUI code.
"""
from __future__ import annotations

import math
import re


_RELATIONS = frozenset({'periodic_delta', 'molecular_reference'})


def _deduplicate(items):
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def _logical(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().strip('.').upper()
    if token in {'T', 'TRUE', '1'}:
        return True
    if token in {'F', 'FALSE', '0'}:
        return False
    return None


def _choice(value):
    if value is None:
        # METAGGA is disabled by default in VASP.  Omission is therefore the
        # same effective method as an explicit false/none sentinel, not missing
        # evidence.
        return 'F'
    logical = _logical(value)
    if logical is not None:
        return 'T' if logical else 'F'
    token = str(value).strip().strip('"').strip("'").upper()
    if token in {'NONE', '--'}:
        return 'F'
    return token or None


def _functional(value):
    if value is None:
        return None
    token = ' '.join(str(value).split()).upper()
    return token or None


def _kpoints(value):
    """Canonicalize the effective KPOINTS scheme without using comments."""
    if not isinstance(value, dict):
        return None
    scheme = str(value.get('scheme') or value.get('mode') or '').strip().upper()
    grid = value.get('grid')
    shift = value.get('shift')
    if isinstance(grid, (list, tuple)) and len(grid) == 3:
        try:
            grid = tuple(int(item) for item in grid)
        except (TypeError, ValueError):
            return None
    else:
        grid = None
    if isinstance(shift, (list, tuple)) and len(shift) == 3:
        try:
            shift = tuple(round(float(item), 12) for item in shift)
        except (TypeError, ValueError):
            return None
    else:
        shift = None
    raw_digest = str(value.get('raw_sha256') or '').strip().lower() or None
    raw = str(value.get('raw') or '').strip() or None
    explicit_identity = raw_digest or raw
    return (scheme, grid, shift, explicit_identity) if scheme else None


def _same(left, right, *, tolerance=1e-10):
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    return left == right


def _vector(value, *, integer=False):
    """Expand a finite VASP vector, including ``n*value`` notation."""
    if isinstance(value, (list, tuple)):
        tokens = list(value)
    elif value is None or isinstance(value, bool):
        return None
    else:
        tokens = str(value).replace(',', ' ').split()
    result = []
    for token in tokens:
        repeated = re.fullmatch(r'(\d+)\*([^*]+)', str(token).strip())
        count, raw = (int(repeated.group(1)), repeated.group(2)) if repeated else (1, token)
        number = _number(raw)
        if number is None or (integer and number != int(number)):
            return None
        result.extend([int(number) if integer else number] * count)
    return result or None


def _orders(plan):
    """Return unique, viable element orders without inventing an ordering."""
    raw_orders = (plan or {}).get('element_orders') or []
    if raw_orders and all(isinstance(item, str) for item in raw_orders):
        raw_orders = [raw_orders]
    orders = []
    for raw in raw_orders:
        if not isinstance(raw, (list, tuple)):
            continue
        order = tuple(str(element).strip() for element in raw)
        if not order or any(not element for element in order) or len(set(order)) != len(order):
            continue
        if order not in orders:
            orders.append(order)
    return orders


def _titel_element(titel):
    """Extract an element from a normal VASP TITEL without guessing variants."""
    for token in str(titel or '').split()[1:]:
        base = token.split('_', 1)[0]
        if re.fullmatch(r'[A-Z][a-z]?', base):
            return base
    return None


def _titles(plan):
    raw = (plan or {}).get('potcar_titel')
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    titles = tuple(' '.join(str(item).split()) for item in raw)
    return titles if all(titles) else None


def _title_map(plan):
    """Map element to TITEL only when the available evidence is unambiguous."""
    titles = _titles(plan)
    if titles is None:
        return None
    candidates = []
    for order in _orders(plan):
        if len(order) != len(titles):
            continue
        mapped = dict(zip(order, titles))
        if mapped not in candidates:
            candidates.append(mapped)

    # Imported output signatures sometimes retain TITEL but not POSCAR order.
    # A normal TITEL is sufficient only when every element is explicit/unique.
    inferred = tuple(_titel_element(titel) for titel in titles)
    if all(inferred) and len(set(inferred)) == len(inferred):
        mapped = dict(zip(inferred, titles))
        if mapped not in candidates:
            candidates.append(mapped)
    return candidates[0] if len(candidates) == 1 else None


def _element_set(plan):
    candidates = {frozenset(order) for order in _orders(plan)}
    title_map = _title_map(plan)
    if title_map:
        candidates.add(frozenset(title_map))
    return set(next(iter(candidates))) if len(candidates) == 1 else None


def _vector_map(plan, key, *, integer=False):
    vector = _vector((plan or {}).get(key), integer=integer)
    if vector is None:
        return None
    candidates = []
    orders = list(_orders(plan))
    if not orders:
        titles = _titles(plan)
        if titles:
            inferred = tuple(_titel_element(titel) for titel in titles)
            if all(inferred) and len(set(inferred)) == len(inferred):
                orders.append(inferred)
    for order in orders:
        if len(order) != len(vector):
            continue
        mapped = dict(zip(order, vector))
        if mapped not in candidates:
            candidates.append(mapped)
    return candidates[0] if len(candidates) == 1 else None


def _effective_u(plan, shared_elements):
    """Return ``(values, unknown_elements, global_unknown)`` for shared species.

    An inactive LDAU flag means every shared element is effectively ``off``.
    With LDAU active, LDAUL=-1 is also effectively ``off`` and therefore does
    not require U/J/type values.  Active entries are represented by the full
    ``(LDAUTYPE, L, U, J)`` tuple rather than a raw order-dependent vector.
    """
    enabled = _logical((plan or {}).get('ldau'))
    if enabled is None:
        return {}, set(shared_elements), True
    if not enabled:
        return {element: ('off',) for element in shared_elements}, set(), False

    # VASP effective defaults: type=2, L=2 for every species, U=J=0.
    # Apply defaults only when a key is omitted; an explicitly malformed vector
    # remains unknown and is caught by the directory-local execution gate.
    l_map = ({element: 2 for element in shared_elements}
             if (plan or {}).get('ldaul') is None
             else _vector_map(plan, 'ldaul', integer=True))
    u_map = ({element: 0.0 for element in shared_elements}
             if (plan or {}).get('ldauu') is None
             else _vector_map(plan, 'ldauu'))
    j_map = ({element: 0.0 for element in shared_elements}
             if (plan or {}).get('ldauj') is None
             else _vector_map(plan, 'ldauj'))
    u_type = (_integer((plan or {}).get('ldautype'))
              if (plan or {}).get('ldautype') is not None else 2)
    values = {}
    unknown = set()
    for element in shared_elements:
        if (l_map is None or u_map is None or j_map is None
                or element not in l_map or element not in u_map
                or element not in j_map):
            unknown.add(element)
            continue
        orbital = l_map[element]
        u_value = float(u_map[element])
        j_value = float(j_map[element])
        effectively_zero = (
            abs(u_value - j_value) <= 1e-14 if u_type == 2
            else abs(u_value) <= 1e-14 and abs(j_value) <= 1e-14)
        if orbital == -1 or effectively_zero:
            values[element] = ('off',)
            continue
        if u_type is None:
            unknown.add(element)
            continue
        if u_type == 2:
            # Dudarev's total energy depends on Ueff=U-J, not the two raw
            # numbers independently.  Canonicalise equivalent parameter pairs.
            values[element] = ('on', u_type, orbital, u_value - j_value, 0.0)
        else:
            values[element] = ('on', u_type, orbital, u_value, j_value)
    return values, unknown, False


def effective_u_by_element(plan):
    """Return one complete effective-U map, or ``None`` when evidence is ambiguous."""
    elements = _element_set(plan)
    if elements is None:
        return None
    values, unknown, global_unknown = _effective_u(plan, elements)
    if global_unknown or unknown:
        return None
    result = {}
    for element, value in values.items():
        if value == ('off',):
            result[element] = {
                'enabled': False, 'type': None, 'l': -1, 'u': 0.0, 'j': 0.0,
            }
            continue
        _on, u_type, orbital, u_value, j_value = value
        result[element] = {
            'enabled': True, 'type': u_type, 'l': orbital,
            'u': float(u_value), 'j': float(j_value),
        }
    return result


def _fmt_u(value):
    if value == ('off',):
        return 'off'
    _on, u_type, orbital, u_value, j_value = value
    if u_type == 2:
        return f'type=2,l={orbital},Ueff={u_value:g}'
    return f'type={u_type},l={orbital},U={u_value:g},J={j_value:g}'


def _compare_scalar(left, right, key, display, normalizer, issues, warnings, *,
                    left_label, right_label):
    left_value = normalizer((left or {}).get(key))
    right_value = normalizer((right or {}).get(key))
    if left_value is None or right_value is None:
        missing = []
        if left_value is None:
            missing.append(left_label)
        if right_value is None:
            missing.append(right_label)
        warnings.append(f'{display} 证据不完整（{"、".join(missing)}）')
        return 0
    if not _same(left_value, right_value):
        issues.append(
            f'{display} 不一致：{left_label}={left_value!r}，'
            f'{right_label}={right_value!r}')
        return 0
    return 1


def compare_plans(left, right, *, left_label, right_label, relation):
    """Compare two method plans under a role-aware energy relation.

    Args:
        left, right: Plan dictionaries produced by the preparation layer.
        left_label, right_label: User-facing member names.
        relation: ``periodic_delta`` for clean-slab/config subtraction or
            ``molecular_reference`` for a gas-reference/config subtraction.

    Returns:
        ``{'issues', 'warnings', 'notes', 'checked_fields'}``.  Known
        Hamiltonian/basis conflicts are issues, incomplete evidence is a
        warning, and a valid role-dependent spin difference is only a note.
    """
    if relation not in _RELATIONS:
        raise ValueError(
            f'relation 必须是 {sorted(_RELATIONS)!r} 之一，收到 {relation!r}')
    left = dict(left or {})
    right = dict(right or {})
    left_label = str(left_label or 'left')
    right_label = str(right_label or 'right')
    issues, warnings, notes = [], [], []
    checked = 0

    for key, display, normalizer in (
            ('functional', '泛函', _functional),
            ('ivdw', '色散校正 IVDW', _integer),
            ('encut', 'ENCUT', _number),
            ('metagga', 'METAGGA', _choice),
            ('lhfcalc', 'LHFCALC', _logical)):
        checked += _compare_scalar(
            left, right, key, display, normalizer, issues, warnings,
            left_label=left_label, right_label=right_label)

    left_hybrid = _logical(left.get('lhfcalc'))
    right_hybrid = _logical(right.get('lhfcalc'))
    if left_hybrid is True and right_hybrid is True:
        for key, display in (('aexx', 'AEXX'), ('hfscreen', 'HFSCREEN')):
            checked += _compare_scalar(
                left, right, key, display, _number, issues, warnings,
                left_label=left_label, right_label=right_label)

    if relation == 'periodic_delta':
        checked += _compare_scalar(
            left, right, 'kpoints_scheme', 'KPOINTS', _kpoints,
            issues, warnings, left_label=left_label, right_label=right_label)

    # ISPIN validity belongs to the local input gate.  Here a legal difference
    # represents potentially different physical ground states, not a method
    # incompatibility by itself.
    left_spin = _integer(left.get('ispin'))
    right_spin = _integer(right.get('ispin'))
    if left_spin in {1, 2} and right_spin in {1, 2}:
        checked += 1
        if left_spin != right_spin:
            if relation == 'periodic_delta':
                notes.append(
                    f'{left_label} ISPIN={left_spin}，{right_label} ISPIN={right_spin}；'
                    '合法自旋差异不自动构成不兼容，可能对应吸附诱导磁性。若 clean slab '
                    '使用 ISPIN=1，建议补做 ISPIN=2 对照并核对能量与局域磁矩')
            else:
                notes.append(
                    f'{left_label} ISPIN={left_spin}，{right_label} ISPIN={right_spin}；'
                    '分子参考与周期体系是独立体系，应分别采用各自经验证的基态自旋')
    elif left_spin is None or right_spin is None:
        warnings.append('ISPIN 证据不完整；合法性由各成员本地输入门校验')
    else:
        notes.append('ISPIN 合法性由各成员本地输入门校验，本策略不将非法值转为方法冲突')

    left_elements = _element_set(left)
    right_elements = _element_set(right)
    shared_elements = ((left_elements & right_elements)
                       if left_elements is not None and right_elements is not None else None)

    left_titles = _title_map(left)
    right_titles = _title_map(right)
    if left_titles is None or right_titles is None:
        warnings.append('POTCAR 元素/TITEL 证据不完整，无法按共享元素核对')
    else:
        shared_titles = sorted(set(left_titles) & set(right_titles))
        if not shared_titles:
            notes.append('两侧没有共享元素，无 POTCAR TITEL 项需要比较')
        for element in shared_titles:
            if left_titles[element] != right_titles[element]:
                issues.append(
                    f'{element} 的 POTCAR TITEL 不一致：'
                    f'{left_label}={left_titles[element]!r}，'
                    f'{right_label}={right_titles[element]!r}')
            else:
                checked += 1

    if shared_elements is None:
        warnings.append('元素顺序证据不完整，无法按共享元素核对 DFT+U')
    elif not shared_elements:
        notes.append('两侧没有共享元素，无 DFT+U 项需要比较')
    else:
        left_u, left_unknown, _left_global_unknown = _effective_u(left, shared_elements)
        right_u, right_unknown, _right_global_unknown = _effective_u(right, shared_elements)
        for element in sorted(shared_elements):
            if element in left_unknown or element in right_unknown:
                missing = []
                if element in left_unknown:
                    missing.append(left_label)
                if element in right_unknown:
                    missing.append(right_label)
                warnings.append(
                    f'{element} 的 DFT+U 有效参数映射不完整（{"、".join(missing)}）')
                continue
            left_value = left_u[element]
            right_value = right_u[element]
            if left_value != right_value:
                issues.append(
                    f'{element} 的 DFT+U 有效参数不一致：'
                    f'{left_label}={_fmt_u(left_value)}，'
                    f'{right_label}={_fmt_u(right_value)}')
            else:
                checked += 1

    return {
        'issues': _deduplicate(issues),
        'warnings': _deduplicate(warnings),
        'notes': _deduplicate(notes),
        'checked_fields': checked,
    }


__all__ = ['compare_plans', 'effective_u_by_element']
