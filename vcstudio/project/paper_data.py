"""论文数据表抽取 → 参考数据集 → 与计算值确定性对照(复现校验)。

本模块是「设计宪法第一条(科学正确)」在**数据复现**环节的可执行化。三段式,LLM 只碰第一段
的文本抽取,数值对照与统计全在确定性层:

1. **LLM 只抄论文里出现的数值**:`extract_data_tables` 的抽取纪律钉死——只誊抄表格里印着的
   数字,每行带页码线索,不确定留 null;绝不计算/推断/编造。抽取后必经**确定性校验层**:
   值必须能解析成数(否则弃行)、体系名非空、值域合理性(|ΔE|<20 eV)、同表重复行去重。
   抽出的是**文献参考值**,只用于和计算值对照,**绝不回流进 INCAR/POSCAR/能量计算链路**。
2. **归一**:`build_reference_dataset` 把体系名规范成 M@N4 风格、物种名对齐 molecules 库命名,
   量纲(吸附能/自由能/极限电位/能垒)由表 kind 派生成 quantity。
3. **纯确定性对照**:`compare_with_computed` 只做匹配 + 减法 + MAE/RMSE(单位假定 eV),缺对齐
   项列 unmatched;`mae_report_md` 出 validation.md 口径的对照表。**无一处 LLM,无一处编造**。

复用 ai_paper 的 `_call_llm`(transport 注入)/ `_parse_float` / `_TEMPLATE_ALIASES` / `_ELEMENTS`。
纯逻辑离线可测,HTTP 经 transport= 注入。中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import math
import re

from vcstudio.project import ai_paper

# ── 常量 ─────────────────────────────────────────────────────────────────────
#: 抽取值域合理性上限(eV):|value| 超过即判为畸值弃行(吸附能/自由能/能垒/极限电位通用松界)。
MAX_ABS_EV = 20.0

#: 数据表 kind 白名单 → quantity 记号(供对照对齐)。
_KIND_QUANTITY = {
    'adsorption_energy': 'E_ads',
    'free_energy': 'dG',
    'u_l': 'U_L',
    'barrier': 'Ea',
    'other': 'value',
}
_ALLOWED_KINDS = frozenset(_KIND_QUANTITY)

EXTRACT_TABLES_SYSTEM_PROMPT = (
    'You are a scientific-literature EXTRACTION assistant for computational catalysis. Your ONLY '
    'job is to COPY numeric values that are LITERALLY PRINTED in the paper\'s data tables/text into '
    'a structured JSON. You are STRICTLY FORBIDDEN from computing, averaging, converting units, '
    'deriving, or inventing any value. NEVER produce a number that is not written verbatim in the '
    'source. If a cell is not stated, set its value to null. Every row MUST carry a "page_hint" '
    'locating it (page number or table label); if you cannot locate it, set page_hint to null — do '
    'NOT guess a page. Do NOT translate or normalize system names (keep "Fe-N4", "FeN4" as printed) '
    '— a downstream deterministic validator normalizes them. Respond with a single JSON object only.')


def build_extract_tables_prompt(paper_text, *, max_chars=24000):
    """拼装数据表抽取提示词:论文正文(截断) + 抽取纪律 + JSON 契约(形状放末尾,recency)。"""
    body = str(paper_text or '')
    if len(body) > max_chars:
        body = body[:max_chars] + '\n…[truncated]'
    shape = {
        'tables': [{
            'label': '<table label exactly as printed, e.g. Table 3 or 表3-2>',
            'kind': 'adsorption_energy|free_energy|u_l|barrier|other',
            'columns': ['<column headers copied verbatim>'],
            'rows': [{
                'system': '<catalyst/system name copied verbatim, non-empty>',
                'species': '<adsorbate/intermediate copied verbatim, or null>',
                'value_ev': '<number copied verbatim from the cell, or null>',
                'page_hint': '<page number or table locator, or null if unknown>',
            }],
            'origin_snippet': '<verbatim excerpt (<=200 chars) around the table>',
        }],
    }
    return (
        'Extract the numeric DATA TABLES from this paper text. COPY ONLY values printed in the '
        'source; leave any unknown cell null; NEVER compute, average, convert, or invent a number.\n\n'
        'PAPER TEXT:\n' + body + '\n\n'
        'Output MUST be a single JSON object with EXACTLY this shape (every value copied verbatim, '
        'unknowns null):\n' + json.dumps(shape, ensure_ascii=False, indent=1))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. LLM 抽取 + 确定性校验层
# ═══════════════════════════════════════════════════════════════════════════════
def _num(v):
    """把任意写法(含 Unicode 负号 −/–/—、'−1.23 eV')解析成 float;抽不到 → None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).replace('−', '-').replace('–', '-').replace('—', '-')
    return ai_paper._parse_float(s)


def _norm_kind(v):
    """kind 归一到白名单;不认得 → 'other'(绝不臆造新类)。"""
    k = re.sub(r'[\s\-]', '_', str(v or '').strip().lower())
    return k if k in _ALLOWED_KINDS else 'other'


def _clean_rows(raw_rows):
    """确定性校验一张表的行:值必须成数、体系名非空、值域合理、同表去重(保序)。

    返回 (rows, dropped):dropped 记被弃行的原因(便于上层呈报,不静默吞)。
    """
    rows, dropped, seen = [], [], set()
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            dropped.append({'reason': '行不是对象', 'row': r})
            continue
        system = str(r.get('system') or '').strip()
        if not system:                                       # 体系名非空(契约)
            dropped.append({'reason': '体系名为空', 'row': r})
            continue
        val = _num(r.get('value_ev'))
        if val is None:                                      # 值必须能解析成数(契约)
            dropped.append({'reason': '值非数值', 'row': r})
            continue
        if abs(val) >= MAX_ABS_EV:                           # 值域合理性(|ΔE|<20 eV)
            dropped.append({'reason': f'值 {val:g} 超出合理值域(|v|≥{MAX_ABS_EV:g})', 'row': r})
            continue
        species = r.get('species')
        species = str(species).strip() if species not in (None, '') else None
        page = r.get('page_hint')
        page = str(page).strip() if page not in (None, '') else None   # 缺页码 → null,不臆造
        key = (system, species, round(val, 6))
        if key in seen:                                      # 同表重复行去重
            dropped.append({'reason': '重复行', 'row': r})
            continue
        seen.add(key)
        rows.append({'system': system, 'species': species,
                     'value_ev': val, 'page_hint': page})
    return rows, dropped


def _validate_tables(data):
    """LLM 原始输出 → 校验后的 tables 列表(确定性层)。非 dict/缺 tables → []。"""
    tables = []
    for t in ((data or {}).get('tables') or []):
        if not isinstance(t, dict):
            continue
        rows, dropped = _clean_rows(t.get('rows'))
        cols = [str(c) for c in (t.get('columns') or []) if c not in (None, '')]
        tables.append({
            'label': str(t.get('label') or '').strip() or '(未标注表号)',
            'kind': _norm_kind(t.get('kind')),
            'columns': cols,
            'rows': rows,
            'origin_snippet': str(t.get('origin_snippet') or '').strip(),
            'dropped': dropped,
        })
    return tables


def extract_data_tables(paper_text, *, transport=None, base_url=None, model=None, api_key=None):
    """论文正文 → 结构化数据表 {'ok','tables':[...],'error'}。

    tables 每项:{'label','kind','columns','rows':[{'system','species','value_ev','page_hint'}],
    'origin_snippet','dropped'}。LLM **只誊抄**论文里印着的数值(提示词纪律 + 单一 JSON 契约),
    返回后必经确定性校验层(值成数 / 体系非空 / |v|<20 eV / 同表去重)。

    本函数是「数据复现」链的**取数**环节;抽出的是文献参考值,仅供对照,**绝不回流计算链路**。
    与 ai_paper._call_llm 同样走 transport 注入 / 密钥 / _extract_json / 重试;无 key 即降级引导。
    联网门控由上层(ai_paper)负责,与 _call_llm 一致(本函数是被调层)。
    """
    cfg = {}
    if base_url:
        cfg['base_url'] = base_url
    if model:
        cfg['model'] = model
    if api_key:
        cfg['api_key'] = api_key
    llm = ai_paper._call_llm(EXTRACT_TABLES_SYSTEM_PROMPT,
                             build_extract_tables_prompt(paper_text),
                             transport=transport, config=cfg)
    if not llm['ok']:
        return {'ok': False, 'tables': [], 'error': llm['error']}
    return {'ok': True, 'tables': _validate_tables(llm['data']), 'error': None}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 参考数据集归一(体系名 M@N4 风格 / 物种对齐 molecules 库 / 量纲派生)
# ═══════════════════════════════════════════════════════════════════════════════
def _canon_system(s):
    """体系名规范化 → 'Fe@N4' 风格。接受 'Fe-N4'/'FeN4'/'Fe@N4'/'Fe/N4'/'Fe@MN4'/'Fe N4'。

    解析不出金属符号 → 原样返回(不硬塞)。配位环境经 _TEMPLATE_ALIASES 归一(MN4→N4)。
    """
    raw = str(s or '').strip()
    if not raw:
        return ''
    compact = re.sub(r'[\s\-@/_]', '', raw)
    metal, rest = None, ''
    for length in (2, 1):                                    # 优先双字符元素(Fe/Co/Ni…)
        cand = compact[:length]
        if cand in ai_paper._ELEMENTS:
            metal, rest = cand, compact[length:]
            break
    if metal is None:
        return raw
    if not rest:
        return metal
    env_key = rest.upper()
    tmpl = ai_paper._TEMPLATE_ALIASES.get(env_key)
    env = (tmpl[1:] if tmpl and tmpl.startswith('M') else (tmpl or rest.upper()))
    return f'{metal}@{env}'


def _canon_species(s):
    """物种名对齐 molecules 库命名(大小写不敏感,剥 '*');库里没有 → 原样(去 '*')。"""
    if s in (None, ''):
        return None
    raw = str(s).strip()
    name = raw.strip('*').strip()
    if not name:
        return raw
    from vcstudio.generate import molecules
    lut = {n.lower(): n for n in molecules.list_molecules()}
    return lut.get(name.lower(), name)


def _norm_quantity(kind_or_quantity):
    """kind 或已给的 quantity → 规范 quantity 记号(E_ads/dG/U_L/Ea/value)。"""
    v = str(kind_or_quantity or '').strip()
    if v in _KIND_QUANTITY:
        return _KIND_QUANTITY[v]
    if v in _KIND_QUANTITY.values():
        return v
    return 'value'


def build_reference_dataset(tables):
    """数据表 → 归一参考集 {'entries':[{'system','species','quantity','ref_value','label'}]}。

    体系名规范成 M@N4、物种对齐 molecules 库、量纲由表 kind 派生。**纯确定性归一,不产新数值**。
    """
    entries = []
    for t in (tables or []):
        if not isinstance(t, dict):
            continue
        quantity = _norm_quantity(t.get('kind'))
        label = t.get('label') or ''
        for r in (t.get('rows') or []):
            val = _num(r.get('value_ev'))
            if val is None:
                continue
            entries.append({
                'system': _canon_system(r.get('system')),
                'species': _canon_species(r.get('species')),
                'quantity': quantity,
                'ref_value': val,
                'label': label,
            })
    return {'entries': entries}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 与计算值确定性对照(纯确定性:匹配 + 减法 + MAE/RMSE;单位假定 eV)
# ═══════════════════════════════════════════════════════════════════════════════
def _match_key(system, species, quantity):
    return (_canon_system(system), _canon_species(species), _norm_quantity(quantity))


def compare_with_computed(reference, computed):
    """文献参考集 × 计算值 → 对照 {'pairs','mae','rmse','n','worst','unmatched','summary_zh'}。

    **纯确定性**:按(体系, 物种, 量)对齐,单位假定 eV;delta = 计算 − 文献。worst 取 |delta|
    前三;缺对齐项(任一侧无对应)列入 unmatched。绝不 LLM、绝不编造。
    """
    entries = (reference or {}).get('entries') or []
    comp_index = {}
    for c in (computed or []):
        if not isinstance(c, dict):
            continue
        v = _num(c.get('value'))
        if v is None:
            continue
        comp_index.setdefault(
            _match_key(c.get('system'), c.get('species'), c.get('quantity')),
            {'value': v, 'system': c.get('system'), 'species': c.get('species'),
             'quantity': c.get('quantity')})

    pairs, unmatched, used = [], [], set()
    for e in entries:
        ref = _num(e.get('ref_value'))
        if ref is None:
            continue
        key = _match_key(e.get('system'), e.get('species'), e.get('quantity'))
        hit = comp_index.get(key)
        if hit is None:
            unmatched.append({'system': e.get('system'), 'species': e.get('species'),
                              'quantity': e.get('quantity'), 'ref': ref,
                              'reason': '计算侧无对齐项'})
            continue
        ours = hit['value']
        delta = ours - ref
        pairs.append({'system': e.get('system'), 'species': e.get('species'),
                      'quantity': _norm_quantity(e.get('quantity')),
                      'ref': ref, 'ours': ours,
                      'delta': delta, 'abs_delta': abs(delta)})
        used.add(key)
    for key, hit in comp_index.items():
        if key not in used:
            unmatched.append({'system': hit['system'], 'species': hit['species'],
                              'quantity': hit['quantity'], 'ours': hit['value'],
                              'reason': '文献侧无对齐项'})

    n = len(pairs)
    mae = (sum(p['abs_delta'] for p in pairs) / n) if n else None
    rmse = (math.sqrt(sum(p['delta'] ** 2 for p in pairs) / n)) if n else None
    worst = sorted(pairs, key=lambda p: p['abs_delta'], reverse=True)[:3]

    if n:
        w = worst[0]
        summary_zh = (
            f'共比对 {n} 项:MAE = {mae:.3f} eV,RMSE = {rmse:.3f} eV;'
            f'偏差最大为 {w["system"]}/{w["species"]}(计算 {w["ours"]:+.3f} 对文献 '
            f'{w["ref"]:+.3f},Δ = {w["delta"]:+.3f} eV)。')
        if unmatched:
            summary_zh += f'另有 {len(unmatched)} 项因无对齐(体系/物种/量不匹配)未纳入对照。'
    else:
        summary_zh = ('无可比对项:文献参考集与计算值在(体系, 物种, 量)上无任何对齐,'
                      '请核对体系/物种命名或补齐计算。')

    return {'pairs': pairs, 'mae': mae, 'rmse': rmse, 'n': n,
            'worst': worst, 'unmatched': unmatched, 'summary_zh': summary_zh}


def _fmt(v, nd=3):
    return '—' if v is None else f'{float(v):.{nd}f}'


def mae_report_md(comparison):
    """对照结果 → Markdown 对照表(validation.md 口径:体系 × 物种 × 文献值 × 计算值 × 差)。"""
    comparison = comparison or {}
    n = comparison.get('n') or 0
    mae, rmse = comparison.get('mae'), comparison.get('rmse')
    lines = ['## 文献对照(复现校验)', '']
    if n:
        lines.append(f'共 {n} 项对照,MAE = {_fmt(mae)} eV,RMSE = {_fmt(rmse)} eV。')
    else:
        lines.append('无可比对项(文献参考集与计算值无对齐的体系/物种/量)。')
    lines += ['', '| 体系 | 物种 | 文献值/eV | 计算值/eV | 差/eV |',
              '| --- | --- | --- | --- | --- |']
    for p in comparison.get('pairs') or []:
        lines.append(f'| {p.get("system") or ""} | {p.get("species") or ""} | '
                     f'{_fmt(p.get("ref"))} | {_fmt(p.get("ours"))} | {_fmt(p.get("delta"))} |')
    unmatched = comparison.get('unmatched') or []
    if unmatched:
        lines += ['', f'> 未对齐项({len(unmatched)}):']
        for u in unmatched:
            lines.append(f'> - {u.get("system") or ""}/{u.get("species") or ""} '
                         f'（{u.get("quantity") or ""}）：{u.get("reason") or ""}')
    return '\n'.join(lines) + '\n'
