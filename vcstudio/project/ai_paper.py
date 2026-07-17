"""AI 智能体入口:论文 → 规格表 → (确认或全自动) → 计算活动(蓝图 E9/E10/E11/E13/E17)。

本模块是「设计宪法第一条(科学正确)」在 AI 入口处的可执行化,把 LLM 钉死成写进确定性
文件控制面之下、可否决的**薄提案层**。硬护栏(全自动模式同样生效,写进代码不是提示词):

1. **LLM 只做文本抽取,绝不产数值结论**:抽取提示词禁止计算/推断/编造;每格叶子值配
   `origin`(页码 + ≤120 字原文引句)+ confidence,不确定留 null+low。
2. **每格带出处**:SPEC_SCHEMA 每个叶子 = {'value','origin':{'page','quote'},'confidence'}。
3. **spec 必须过确定性校验**:`validate_spec` 做泛函归一 / ENCUT 区间 / k 网格 / 金属符号 /
   模板映射 / 吸附质映射 / 反应预设匹配,每个修正记 `normalized` 并保留原值。
4. **实例化必经机时预算闸 + 单点先行闸**:`instantiate` 门禁序列 ①budget ②pilot ③写 campaign
   ④账本,全自动与交互共用同一套闸。
5. **所有决策写账本**(每步 origin='ai_paper');**allow_external 门**沿用 ai_analysis 口径。
6. **contradicts-halt(科学停)**:全自动编排 `autopilot_step` 若计算值与文献锚点偏差超阈值,
   立刻 halt + 中文平铺,绝不自动判「复现成功」或掩盖;`accepted` 只能由确定性 validated +
   accept_gate 产生,**绝不由本模块直写**。

复用而不改动:ai_analysis 的 transport 注入 / 密钥 / _extract_json / 重试;campaign 的
schema/states/gates/derive/ledger/budget/fingerprint;generate 的 molecules / reactions 库。
纯逻辑离线可测,HTTP 经 transport= 注入。中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import math
import re
import time

from vcstudio.campaign import budget, derive, fingerprint, gates, ledger, schema, states
from vcstudio.project import ai_analysis

# ── 常量 ─────────────────────────────────────────────────────────────────────
#: 方法学段落关键词(双语),只把命中窗口喂 LLM(省 token + 防迷航)。
METHOD_KEYWORDS = (
    'VASP', 'ENCUT', 'cutoff', 'plane-wave', 'plane wave', 'k-point', 'k point',
    'kpoint', 'Monkhorst', 'Gamma-centered', 'Brillouin', 'PBE', 'RPBE', 'GGA',
    'PAW', 'pseudopotential', 'functional', 'exchange-correlation', 'dispersion',
    'DFT-D3', 'DFT-D2', 'vdW', 'EDIFF', 'EDIFFG', 'convergence', 'converged',
    'spin-polar', 'ISPIN', 'Hubbard', 'DFT+U', 'implicit solvation', 'VASPsol',
    # 中文
    '泛函', '赝势', '截断能', '收敛', '色散校正', '布里渊', '平面波', '自旋极化',
    '单原子催化', '吸附能',
)

#: methods 小节的字段清单(SPEC_SCHEMA 与 validate 共用)。
METHOD_FIELDS = ('functional', 'dispersion', 'encut', 'kpoints_relax', 'kpoints_static',
                 'ediff', 'ediffg', 'spin', 'u_values', 'solvation')

#: 单点先行闸的显式跳过令牌(用户自负其责;写进账本)。
SKIP_PILOT_TOKEN = 'skip-pilot-I-know'

#: contradicts-halt 默认容差(eV;文献锚点可逐项覆盖 tol)。
CONTRADICT_TOL = 0.3

#: LLM 重试退避(秒);测试可置 0 以免真 sleep。
RETRY_BACKOFF_SEC = 1.5

#: 机时估算的量级默认(仅供预算闸比对,非真实基准)。
_DEFAULT_CORES = 64
_SLAB_NATOMS = 32
_ADS_EXTRA = 8

#: 每个实例化作业默认声明的确定性检查(真值由执行器/checker 回填)。
DEFAULT_CHECKS = ('scf_converged', 'force_below_ediffg')

#: 规格表文档化 schema:每个叶子值形如 {'value','origin':{'page','quote'},'confidence'}。
_LEAF = {'value': None, 'origin': {'page': None, 'quote': ''}, 'confidence': 'low'}
SPEC_SCHEMA = {
    'systems': [{'substrate': _LEAF, 'sites': [_LEAF], 'metals': [_LEAF]}],
    'adsorbates': [_LEAF],
    'methods': {f: _LEAF for f in METHOD_FIELDS},
    'reactions': [_LEAF],   # 预设 key 候选(ORR/OER/HER/CO2RR/Li-S …)
    'outputs': [_LEAF],     # 图表类型(火山图 / CHE 台阶图 / PDOS …)
    'mode': _LEAF,          # reproduce | rebuild | design
}

#: SAC 六类模板别名 → sac_builder 规范模板名。
_TEMPLATE_ALIASES = {
    'N4': 'MN4', 'MN4': 'MN4', 'MEN4': 'MN4',
    'N3': 'MN3', 'MN3': 'MN3',
    'P1N3': 'MP1N3', 'MP1N3': 'MP1N3', 'PN3': 'MP1N3',
    'S1N3': 'MS1N3', 'MS1N3': 'MS1N3', 'SN3': 'MS1N3',
    'B1N3': 'MB1N3', 'MB1N3': 'MB1N3', 'BN3': 'MB1N3',
    'N4+B': 'MN4+B', 'MN4+B': 'MN4+B', 'N4B': 'MN4+B', 'MN4B': 'MN4+B',
}

#: 泛函别名表(清洗:去空白/连字符/下划线后大写)。
_FUNCTIONAL_ALIASES = {
    'PBE': 'PBE', 'GGAPBE': 'PBE', 'PBEGGA': 'PBE', 'PERDEWBURKEERNZERHOF': 'PBE',
    'RPBE': 'RPBE', 'REVPBE': 'revPBE', 'PBESOL': 'PBEsol', 'PW91': 'PW91',
    'BEEFVDW': 'BEEF-vdW', 'BEEF': 'BEEF-vdW', 'SCAN': 'SCAN', 'R2SCAN': 'r2SCAN',
    'HSE06': 'HSE06', 'HSE': 'HSE06', 'PBE0': 'PBE0',
    'OPTB88VDW': 'optB88-vdW', 'OPTB86BVDW': 'optB86b-vdW', 'OPTPBEVDW': 'optPBE-vdW',
}

#: 色散别名表。
_DISPERSION_ALIASES = {
    'DFT-D3': 'DFT-D3', 'D3': 'DFT-D3', 'D3ZERO': 'DFT-D3', 'IVDW=11': 'DFT-D3',
    'D3(BJ)': 'DFT-D3(BJ)', 'DFT-D3(BJ)': 'DFT-D3(BJ)', 'D3BJ': 'DFT-D3(BJ)', 'IVDW=12': 'DFT-D3(BJ)',
    'D2': 'DFT-D2', 'DFT-D2': 'DFT-D2', 'IVDW=1': 'DFT-D2',
    'TS': 'TS', 'MBD': 'MBD@rsSCS', 'RVV10': 'rVV10',
}

#: 反应预设别名(清洗后大写)。
_REACTION_ALIASES = {
    'ORR': 'ORR_4E', 'OER': 'OER_4E', 'HER': 'HER',
    'CO2RR': 'CO2RR_TO_CO', 'CO2REDUCTION': 'CO2RR_TO_CO', 'CO2TOCO': 'CO2RR_TO_CO',
    'LIS': 'LIS_16E', 'LI-S': 'LIS_16E', 'LI2S': 'LIS_16E',
    'LITHIUMSULFUR': 'LIS_16E', 'LISULFUR': 'LIS_16E',
}

#: 周期表元素符号(用于金属符号校验)。
_ELEMENTS = frozenset((
    'H', 'He', 'Li', 'Be', 'B', 'C', 'N', 'O', 'F', 'Ne', 'Na', 'Mg', 'Al', 'Si',
    'P', 'S', 'Cl', 'Ar', 'K', 'Ca', 'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni',
    'Cu', 'Zn', 'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr', 'Rb', 'Sr', 'Y', 'Zr', 'Nb',
    'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd', 'In', 'Sn', 'Sb', 'Te', 'I', 'Xe',
    'Cs', 'Ba', 'La', 'Ce', 'Pr', 'Nd', 'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy', 'Ho',
    'Er', 'Tm', 'Yb', 'Lu', 'Hf', 'Ta', 'W', 'Re', 'Os', 'Ir', 'Pt', 'Au', 'Hg',
    'Tl', 'Pb', 'Bi', 'Po', 'At', 'Rn', 'Fr', 'Ra', 'Ac', 'Th', 'Pa', 'U', 'Np',
    'Pu', 'Am', 'Cm', 'Bk', 'Cf', 'Es', 'Fm', 'Md', 'No', 'Lr',
))

__all__ = [
    'SPEC_SCHEMA', 'METHOD_FIELDS', 'METHOD_KEYWORDS', 'SKIP_PILOT_TOKEN',
    'CONTRADICT_TOL', 'extract_text', 'ingest_source', 'locate_method_sections',
    'extract_spec', 'validate_spec', 'plan_campaign', 'instantiate', 'autopilot_step',
    'build_extract_prompt', 'EXTRACT_SYSTEM_PROMPT',
]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. PDF/文本摄取 + 方法学段落定位
# ═══════════════════════════════════════════════════════════════════════════════
def _load_pypdf():
    """延迟 import pypdf(缺失即抛 ImportError);单独成函数便于测试打桩。"""
    import pypdf
    return pypdf


def extract_text(pdf_path, *, max_pages=40):
    """PDF → {'pages':[{'n','text'}],'ok','error'}。

    pypdf 为**软依赖**(pyproject 不加硬依赖):缺失时不抛,返回中文引导「未安装 pypdf,
    可粘贴文本代替」。逐页抽取失败的页记空文本不拖垮整篇。
    """
    try:
        pypdf = _load_pypdf()
    except ImportError:
        return {'pages': [], 'ok': False, 'error': '未安装 pypdf,可粘贴文本代替'}
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception as e:                                    # noqa: BLE001 读盘/解析异常统一降级
        return {'pages': [], 'ok': False, 'error': f'PDF 读取失败:{e}'}
    pages = []
    for i, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            txt = page.extract_text() or ''
        except Exception:                                     # noqa: BLE001 单页抽取失败记空
            txt = ''
        pages.append({'n': i, 'text': txt})
    return {'pages': pages, 'ok': True, 'error': None}


def _looks_like_pdf_path(s):
    """短单行且以 .pdf 结尾 → 判为路径(论文全文不会以 .pdf 结尾且必含换行)。"""
    return len(s) < 400 and '\n' not in s and s.lower().rstrip().endswith('.pdf')


def _paginate_text(text):
    """纯文本分页:优先按换页符 \\f 切,否则整篇算一页。伪装页码从 1 起。"""
    chunks = text.split('\f') if '\f' in text else [text]
    return [{'n': i, 'text': c} for i, c in enumerate(chunks, start=1)]


def ingest_source(text_or_path, *, max_pages=40):
    """统一摄取入口:PDF 路径 → extract_text;纯文本 → 按换页/整篇分页伪装页码。

    返回 {'pages':[{'n','text'}],'ok','error'}(与 extract_text 同形状)。
    """
    s = str(text_or_path or '')
    if _looks_like_pdf_path(s):
        return extract_text(s, max_pages=max_pages)
    return {'pages': _paginate_text(s), 'ok': True, 'error': None}


def locate_method_sections(pages, *, window=160):
    """方法学段落定位 → [{'page','snippet'}]。

    在每页里扫 METHOD_KEYWORDS(大小写不敏感),取命中处 ±window 字窗口,页内合并重叠窗口,
    只把这些窗口喂 LLM(省 token + 防迷航)。全页无命中则不产出该页。
    """
    out = []
    for pg in (pages or []):
        n = pg.get('n')
        text = pg.get('text') or ''
        low = text.lower()
        spans = []
        for kw in METHOD_KEYWORDS:
            k = kw.lower()
            start = 0
            while True:
                i = low.find(k, start)
                if i < 0:
                    break
                spans.append((max(0, i - window), min(len(text), i + len(kw) + window)))
                start = i + len(kw)
        if not spans:
            continue
        spans.sort()
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            snippet = text[a:b].strip()
            if snippet:
                out.append({'page': n, 'snippet': snippet})
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 规格表抽取(LLM 只抽文本 + 每格带出处)+ 确定性校验
# ═══════════════════════════════════════════════════════════════════════════════
EXTRACT_SYSTEM_PROMPT = (
    'You are a scientific-literature EXTRACTION assistant for computational catalysis (VASP DFT). '
    'Your ONLY job is to COPY facts literally present in the provided method-section snippets into '
    'a structured spec table. You are STRICTLY FORBIDDEN from computing, deriving, or inventing any '
    'value: NEVER produce a numeric conclusion, adsorption energy, limiting potential, or any '
    'derived quantity. If a field is NOT stated in the snippets, set its "value" to null and '
    '"confidence" to "low". Every extracted leaf MUST carry an "origin" with the source page number '
    'and a verbatim "quote" (<=120 characters, copied exactly from a snippet) justifying the value. '
    'Do NOT translate or normalize values (keep "PBE", "500 eV", "3x3x1" as written) — a downstream '
    'deterministic validator normalizes and checks them. Respond with a single JSON object only.')


def build_extract_prompt(windows):
    """拼装抽取提示词:方法窗口(JSON) + 抽取纪律 + 键名约束(末尾 recency)+ schema 形状。"""
    return (
        'Extract a computational-methods spec table STRICTLY from these method-section snippets '
        '(JSON list of {page, snippet}):\n'
        + json.dumps(windows, ensure_ascii=False, indent=1)
        + '\n\nFill ONLY fields supported by the snippets; leave everything else null with '
          'confidence "low". Copy a verbatim quote (<=120 chars) + page into every leaf\'s '
          '"origin". Do NOT compute, infer, or invent any number.\n'
        # 键名/结构约束放末尾(recency):Claude 系模型会无视只写在 system 里的 schema 自创结构
        + 'Output MUST be a single JSON object with EXACTLY this shape, where every leaf is '
          '{"value": <copied value or null>, "origin": {"page": <int>, "quote": <verbatim str>}, '
          '"confidence": "high"|"low"}:\n'
        + json.dumps(SPEC_SCHEMA, ensure_ascii=False, indent=1))


def _allow_external(config=None):
    """联网门控:显式 config.allow_external 优先,否则复用 ai_analysis 的 config 口径(默认关)。"""
    if isinstance(config, dict) and 'allow_external' in config:
        return bool(config['allow_external'])
    try:
        return bool(ai_analysis._config_llm().get('allow_external', False))
    except Exception:                                         # noqa: BLE001 配置层任何问题都降级默认关
        return False


def _call_llm(system_prompt, user_prompt, *, transport=None, config=None, timeout=60):
    """通用 LLM JSON 调用:复用 ai_analysis 的 transport/密钥/_extract_json/重试机制(注入式)。

    返回 {'ok','data'(dict|None),'error'}。不做联网门控(调用方先门控)。方言自适应:400 拒
    temperature/response_format → 剥参重试;Claude 围栏 JSON 经 _extract_json 提取。
    """
    cfg = dict(config or {})
    llm_cfg = ai_analysis._config_llm() if not (cfg.get('base_url') and cfg.get('model')) else {}
    base_url = cfg.get('base_url') or llm_cfg.get('base_url') or ai_analysis.DEFAULT_BASE_URL
    model = cfg.get('model') or llm_cfg.get('model') or ai_analysis.DEFAULT_MODEL
    transport = transport or ai_analysis._default_transport
    key = cfg.get('api_key') or ai_analysis.load_api_key()
    if not key:
        return {'ok': False, 'data': None,
                'error': '未配置 API key(设置页 LLM 区保存,或粘贴文本手填规格表离线)'}
    req = {
        'model': model,
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': system_prompt},
                     {'role': 'user', 'content': user_prompt}],
    }
    headers = {'Content-Type': 'application/json; charset=utf-8',
               'Authorization': f'Bearer {key}'}
    last_err = ''
    for attempt in range(4):
        if attempt and RETRY_BACKOFF_SEC:
            time.sleep(RETRY_BACKOFF_SEC)                     # 网关抖动缓一拍
        body = json.dumps(req, ensure_ascii=False).encode('utf-8')
        try:
            status, raw = transport(base_url, body, headers, timeout)
        except Exception as e:                                # noqa: BLE001 网络异常统一降级
            last_err = f'请求失败:{e}'
            continue
        text = raw[:300].decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
        if status == 400:
            stripped = False
            for k in ('temperature', 'response_format'):
                if k in req and k in text:
                    req.pop(k)
                    stripped = True
            last_err = f'HTTP 400:{text[:200]}'
            if stripped:
                continue
            break                                             # 400 且无可剥参数 → 无望
        if status != 200:
            last_err = f'HTTP {status}:{text[:200]}'
            continue
        try:
            content = json.loads(raw)['choices'][0]['message']['content']
            data = json.loads(ai_analysis._extract_json(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            last_err = f'响应解析失败:{e}'
            continue
        return {'ok': True, 'data': data, 'error': ''}
    return {'ok': False, 'data': None, 'error': last_err}


def extract_spec(source, *, transport=None, config=None):
    """论文 → 规格表 {'ok','spec','issues','error'}。

    流程:①allow_external 门控(关→引导设置页或手填)②摄取 + 方法学定位(只喂命中窗口)
    ③走 ai_analysis 的 transport/_extract_json/重试(注入式)④LLM 返回后过 validate_spec
    做确定性校验并归一。spec 为归一后的规格表,issues 为确定性校验发现(可非空但不阻断抽取)。
    """
    if not _allow_external(config):
        return {'ok': False, 'spec': None, 'issues': [],
                'error': ('未开启联网抽取:请在设置页开启「允许将文本发送到外部 LLM」,'
                          '或直接粘贴论文方法学段落手填规格表(离线)')}
    ing = ingest_source(source)
    if not ing['ok']:
        return {'ok': False, 'spec': None, 'issues': [], 'error': ing['error']}
    windows = locate_method_sections(ing['pages'])
    if not windows:
        return {'ok': False, 'spec': None, 'issues': [],
                'error': ('未定位到方法学段落(未命中 VASP/ENCUT/泛函/k 点 等关键词),'
                          '请粘贴论文的计算方法学章节')}
    llm = _call_llm(EXTRACT_SYSTEM_PROMPT, build_extract_prompt(windows),
                    transport=transport, config=config)
    if not llm['ok']:
        return {'ok': False, 'spec': None, 'issues': [], 'error': llm['error']}
    val = validate_spec(llm['data'])
    return {'ok': True, 'spec': val['normalized'], 'issues': val['issues'], 'error': None}


# ── 确定性校验 / 归一 ─────────────────────────────────────────────────────────
def _as_cell(x):
    """把叶子统一成 cell:已是 {'value',...} 则补默认字段,否则包成 cell(confidence=low)。"""
    if isinstance(x, dict) and ('value' in x or 'origin' in x or 'confidence' in x):
        c = dict(x)
        c.setdefault('value', x.get('value'))
        c.setdefault('origin', x.get('origin') or {})
        c.setdefault('confidence', x.get('confidence') or 'low')
        return c
    return {'value': x, 'origin': {}, 'confidence': 'low'}


def _cell_norm(cell):
    """取一个 cell 的归一值(缺归一则回落原值);None-safe。"""
    if not isinstance(cell, dict):
        return None
    return cell.get('normalized', cell.get('value'))


def _parse_float(v):
    """从任意写法里抽第一个数(含科学计数与负号);抽不到 → None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r'-?\d+\.?\d*(?:[eE][+-]?\d+)?', str(v))
    return float(m.group(0)) if m else None


def _parse_kgrid(v):
    """k 网格 → [a,b,c]:接受 [3,3,1] / '3x3x1' / '3×3×1' / '3 3 1' / 'Gamma 3 3 1'。"""
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return [int(v[0]), int(v[1]), int(v[2])]
        except (ValueError, TypeError):
            return None
    nums = re.findall(r'\d+', str(v if v is not None else ''))
    if len(nums) >= 3:
        return [int(nums[0]), int(nums[1]), int(nums[2])]
    return None


def _grid_product(g):
    """k 网格三元组的点数积;非法 → None。"""
    if isinstance(g, (list, tuple)) and len(g) >= 3:
        try:
            return int(g[0]) * int(g[1]) * int(g[2])
        except (ValueError, TypeError):
            return None
    return None


def _blank(v):
    return v is None or (isinstance(v, str) and v.strip() == '')


def _norm_functional(v):
    if _blank(v):
        return None, None
    key = re.sub(r'[\s\-_]', '', str(v)).upper()
    if key in _FUNCTIONAL_ALIASES:
        return _FUNCTIONAL_ALIASES[key], None
    return None, f'未知泛函「{v}」(不在别名表中,请人工核对或补充别名)'


def _norm_dispersion(v):
    if _blank(v):
        return None, None
    key = re.sub(r'[\s_]', '', str(v)).upper()
    if key in ('NONE', 'NO', 'OFF', '0'):
        return None, None
    # 未知色散写法原样保留(命名多样,不误报),已知的归一到规范名
    return _DISPERSION_ALIASES.get(key, str(v).strip()), None


def _norm_encut(v):
    if _blank(v):
        return None, None
    f = _parse_float(v)
    if f is None:
        return None, f'无法解析截断能 ENCUT「{v}」'
    if not (200.0 <= f <= 1000.0):
        return f, f'截断能 ENCUT {f:g} eV 超出合理区间 200–1000,请人工核对'
    return f, None


def _norm_kgrid(v):
    if _blank(v):
        return None, None
    g = _parse_kgrid(v)
    if g is None:
        return None, f'无法解析 k 点网格「{v}」(期望形如 3×3×1)'
    return g, None


def _norm_ediff(field, v):
    if _blank(v):
        return None, None
    f = _parse_float(v)
    if f is None:
        return None, f'无法解析 {field.upper()}「{v}」'
    return f, None


def _norm_spin(v):
    if _blank(v):
        return None, None
    s = str(v).lower()
    m = re.search(r'ispin\s*=?\s*([12])', s)
    if m:
        return int(m.group(1)), None
    if any(t in s for t in ('non-magnetic', 'nonmagnetic', 'non-spin', 'non spin',
                            'spin-unpolar', 'spin unpolar', 'unpolarized')):
        return 1, None
    if any(t in s for t in ('spin', 'magnetic', 'polariz', 'polaris')):
        return 2, None
    if s.strip() in ('1', '2'):
        return int(s.strip()), None
    return None, f'无法解析自旋设置「{v}」'


def _norm_method_field(field, v):
    """methods 各字段的确定性归一 → (normalized, issue|None)。"""
    if field == 'functional':
        return _norm_functional(v)
    if field == 'dispersion':
        return _norm_dispersion(v)
    if field == 'encut':
        return _norm_encut(v)
    if field in ('kpoints_relax', 'kpoints_static'):
        return _norm_kgrid(v)
    if field in ('ediff', 'ediffg'):
        return _norm_ediff(field, v)
    if field == 'spin':
        return _norm_spin(v)
    # u_values / solvation:原样透传(不强校验)
    if _blank(v):
        return None, None
    return v, None


def _norm_metal(v):
    if _blank(v):
        return None, None
    sym = str(v).strip()
    if sym in _ELEMENTS:
        return sym, None
    return None, f'未知金属符号「{v}」(不在周期表中,请人工核对)'


def _norm_template(v):
    if _blank(v):
        return None, None
    key = re.sub(r'[\s\-_]', '', str(v)).upper()
    if key in _TEMPLATE_ALIASES:
        return _TEMPLATE_ALIASES[key], None
    return None, (f'未知配位模板「{v}」(无法映射到 SAC 六类 '
                  'MN4/MN3/MP1N3/MS1N3/MB1N3/MN4+B),请人工指定')


def _norm_adsorbate(v):
    if _blank(v):
        return None, None
    from vcstudio.generate import molecules
    lut = {n.lower(): n for n in molecules.list_molecules()}
    raw = str(v).strip()
    name = raw.strip('*').strip()
    if name.lower() in lut:
        return lut[name.lower()], None
    return None, f'未知吸附质「{raw}」,需人工提供结构'


def _norm_reaction(v):
    if _blank(v):
        return None, None
    from vcstudio.project import reactions
    presets = reactions.list_presets()
    s = str(v).strip()
    if s in presets:
        return s, None
    key = re.sub(r'[\s_\-]', '', s).upper()
    if key in _REACTION_ALIASES:
        return _REACTION_ALIASES[key], None
    for p in presets:
        if p.upper() == key:
            return p, None
    return None, f'未知反应预设「{v}」(无法匹配内置预设),可人工指定或按吸附能路线跳过'


def _norm_mode(v):
    if _blank(v):
        return 'reproduce', None          # 缺省保守取复现
    s = str(v).strip().lower()
    if s in ('reproduce', 'reproduction', 'repro', '复现'):
        return 'reproduce', None
    if s in ('rebuild', 'reconstruct', 'reconstruction', '重建'):
        return 'rebuild', None
    if s in ('design', 'designed', 'de novo', '设计'):
        return 'design', None
    return 'reproduce', f'未知模式「{v}」,已回退为 reproduce(复现),请人工确认'


def _norm_expected(expected, issues):
    """可选文献数值锚点 → {metric:{'value','tol','origin'}}(供 contradicts-halt)。"""
    out = {}
    if isinstance(expected, dict):
        items = list(expected.items())
    elif isinstance(expected, list):
        items = [(e.get('metric'), e) for e in expected if isinstance(e, dict)]
    else:
        return out
    for metric, cell in items:
        if not metric:
            continue
        c = _as_cell(cell)
        val = _parse_float(c.get('value'))
        if val is None:
            issues.append(f'文献锚点「{metric}」期望值无法解析为数值,已忽略')
            continue
        out[str(metric)] = {'value': val, 'tol': float(c.get('tol', CONTRADICT_TOL) or CONTRADICT_TOL),
                            'origin': c.get('origin') or {}}
    return out


def _norm_leaf_list(raw_list, normalizer, issues):
    """归一一列叶子:每格补 normalized(命中)并保留原 value;issue 收集进 issues。"""
    out = []
    for item in (raw_list or []):
        cell = _as_cell(item)
        norm, iss = normalizer(cell.get('value'))
        if iss:
            issues.append(iss)
        if norm is not None:
            cell['normalized'] = norm
        out.append(cell)
    return out


def validate_spec(spec):
    """确定性校验 → {'ok','issues','normalized'}。

    对泛函/ENCUT/k 网格/金属符号/模板/吸附质/反应预设做归一与区间/成员校验;**每个修正记
    `normalized` 并保留原 `value`**;发现问题追加中文 issue。ok = 无 issue。此校验是「spec 必须
    过确定性校验」护栏的执行点,plan/instantiate 只消费已归一的字段。
    """
    spec = spec or {}
    issues = []
    out = {}

    systems_out = []
    for sysentry in (spec.get('systems') or []):
        sysentry = sysentry or {}
        systems_out.append({
            'substrate': _as_cell(sysentry.get('substrate')),
            'metals': _norm_leaf_list(sysentry.get('metals'), _norm_metal, issues),
            'sites': _norm_leaf_list(sysentry.get('sites'), _norm_template, issues),
        })
    out['systems'] = systems_out

    out['adsorbates'] = _norm_leaf_list(spec.get('adsorbates'), _norm_adsorbate, issues)

    methods_in = spec.get('methods') or {}
    methods_out = {}
    for field in METHOD_FIELDS:
        cell = _as_cell(methods_in.get(field))
        norm, iss = _norm_method_field(field, cell.get('value'))
        if iss:
            issues.append(iss)
        if norm is not None:
            cell['normalized'] = norm
        methods_out[field] = cell
    out['methods'] = methods_out

    out['reactions'] = _norm_leaf_list(spec.get('reactions'), _norm_reaction, issues)
    out['outputs'] = [_as_cell(o) for o in (spec.get('outputs') or [])]   # 图表类型不强校验

    mode_cell = _as_cell(spec.get('mode'))
    mnorm, miss = _norm_mode(mode_cell.get('value'))
    if miss:
        issues.append(miss)
    mode_cell['normalized'] = mnorm
    out['mode'] = mode_cell

    if spec.get('expected'):
        out['expected'] = _norm_expected(spec['expected'], issues)

    return {'ok': not issues, 'issues': issues, 'normalized': out}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 实例化计划(纯计划对象,不落盘不提交)
# ═══════════════════════════════════════════════════════════════════════════════
def _all_metal_cells(norm):
    cells = []
    for sysentry in (norm.get('systems') or []):
        cells.extend(sysentry.get('metals') or [])
    return cells


def _all_template_cells(norm):
    cells = []
    for sysentry in (norm.get('systems') or []):
        cells.extend(sysentry.get('sites') or [])
    return cells


def _collect(cells):
    """收集一列 cell 的 normalized 值(去重保序,跳过未归一)。"""
    out = []
    for c in (cells or []):
        val = (c or {}).get('normalized') if isinstance(c, dict) else None
        if val is None:
            continue
        if val not in out:
            out.append(val)
    return out


def _build_fingerprint(methods_norm):
    """归一 methods → 方法指纹对象(E4;供 accept_gate 一致性核对与可复现包)。"""
    kr = methods_norm.get('kpoints_relax')
    scheme = (f'Gamma {kr[0]}x{kr[1]}x{kr[2]}'
              if isinstance(kr, (list, tuple)) and len(kr) >= 3 else None)
    return fingerprint.new_fingerprint(
        functional=methods_norm.get('functional'), dispersion=methods_norm.get('dispersion'),
        encut=methods_norm.get('encut'), kpoints_scheme=scheme, spin=methods_norm.get('spin'),
        u_values=methods_norm.get('u_values'), ediff=methods_norm.get('ediff'),
        ediffg=methods_norm.get('ediffg'), reference_convention=None)


def plan_campaign(spec, *, incar_defaults=None):
    """规格表 → 实例化计划 {'plan':{...},'ok'}。**纯计划对象,不落盘不提交**。

    把 spec 翻译成 sac_matrix 参数(金属×模板)+ 吸附批参数(映射后的分子)+ 反应预设选择;
    产出任务蓝图 tasks(金属×模板 的 slab 弛豫 + slab×吸附质 的吸附弛豫,第一个 slab 为 pilot)。
    缺 INCAR → warning「需用户提供 INCAR 或用精度档向导」。ok = 有可实例化作业。
    """
    val = validate_spec(spec)
    norm = val['normalized']
    warnings = list(val['issues'])

    metals = _collect(_all_metal_cells(norm))
    templates = _collect(_all_template_cells(norm))
    ads_names = _collect(norm.get('adsorbates'))
    reactions_norm = _collect(norm.get('reactions'))
    reaction_preset = reactions_norm[0] if reactions_norm else None
    mode = _cell_norm(norm.get('mode')) or 'reproduce'

    slabs = [f'{m}@{t}' for m in metals for t in templates]

    tasks = []
    slab_ids = []
    for slab in slabs:
        sid = f'relax__{slab}'
        tasks.append({'id': sid, 'kind': 'relax', 'depends_on': [], 'slab': slab})
        slab_ids.append(sid)
    for slab, sid in zip(slabs, slab_ids):
        for ads in ads_names:
            tasks.append({'id': f'ads__{slab}__{ads}', 'kind': 'relax',
                          'depends_on': [sid], 'slab': slab, 'adsorbate': ads})
    pilot = slab_ids[0] if slab_ids else None

    methods_norm = {f: _cell_norm((norm.get('methods') or {}).get(f)) for f in METHOD_FIELDS}
    fp = _build_fingerprint(methods_norm)
    fp_hash = fingerprint.fingerprint_hash(fp)
    nk = _grid_product(methods_norm.get('kpoints_relax')) or 9

    if incar_defaults is None:
        warnings.append('需用户提供 INCAR 或用精度档向导(未提供 INCAR,暂无法生成输入四件套)')
    if slabs and not ads_names:
        warnings.append('未识别可映射的吸附质:将只做基底弛豫(如需吸附能请人工补充吸附质结构)')
    if not slabs:
        warnings.append('规格表未产出可建的 slab(缺可识别的金属或配位模板)')

    plan = {
        'slabs': slabs,
        'adsorbates': ads_names,
        'matrix': {'metals': metals, 'templates': templates, 'nx': 4, 'ny': 4, 'vacuum': 20.0},
        'reaction_preset': reaction_preset,
        'jobs_estimate': len(tasks),
        'tasks': tasks,
        'pilot': pilot,
        'nk': nk,
        'warnings': warnings,
        'incar_provided': incar_defaults is not None,
        'mode': mode,
        'fingerprint': fp,
        'fingerprint_hash': fp_hash,
        'expected': norm.get('expected') or {},
        'methods': methods_norm,
    }
    return {'plan': plan, 'ok': bool(tasks)}


def _estimate_plan_hours(plan):
    """粗估整份计划核时(数量级;仅供机时预算闸比对)。"""
    nk = int(plan.get('nk') or 9)
    total = 0.0
    for tb in (plan.get('tasks') or []):
        natoms = _SLAB_NATOMS + (_ADS_EXTRA if tb.get('adsorbate') else 0)
        total += budget.estimate_job(natoms, nk, tb.get('kind', 'relax'), _DEFAULT_CORES)
    return round(total, 3)


def instantiate(plan, out_root, *, confirm_token=None, budget_cap_hours=None,
                campaign_mods=None, sac_mods=None, dry_run=False):
    """计划 → campaign(门禁序列,全自动与交互共用)。返回 {'ok','created','campaign_dir','gates','error'}。

    门禁序列:
      ① **机时预算硬闸**:估算核时 > budget_cap_hours → 拒绝并给拆分建议(不落盘)。
      ② **单点先行闸**:除非 jobs_estimate<=1 或 confirm_token==SKIP_PILOT_TOKEN,置 require_pilot,
         代表作业(pilot)之外的节点 blocked-on-pilot,并返回 {'pilot','awaiting':'pilot_validation'}。
      ③ **写 campaign**:schema 注册**全部**任务节点(pilot 之外挂 pilot 依赖阻塞)+ fingerprint。
      ④ **决策账本**:每步 origin='ai_paper'。
    dry_run:全走门禁逻辑但不写盘(campaign_dir 为将建路径,created 为将建节点)。
    """
    plan = plan or {}
    tasks_bp = list(plan.get('tasks') or [])
    if not tasks_bp:
        return {'ok': False, 'created': [], 'campaign_dir': None, 'gates': {},
                'error': '规格表未产出可实例化的作业(缺金属或配位模板),请补全规格表后重试'}
    campaign_mods = dict(campaign_mods or {})
    sac_mods = dict(sac_mods or {})
    camp_id = campaign_mods.get('id', 'ai-paper')
    jobs_estimate = int(plan.get('jobs_estimate') or len(tasks_bp))
    gates_out = {}

    # ── ① 机时预算硬闸 ─────────────────────────────────────────────
    est_total = _estimate_plan_hours(plan)
    if budget_cap_hours is not None and est_total > float(budget_cap_hours):
        n_batches = max(2, math.ceil(est_total / float(budget_cap_hours)))
        per_batch = max(1, math.ceil(jobs_estimate / n_batches))
        suggestion = (f'预估 {est_total:g} 核时 超过上限 {float(budget_cap_hours):g} 核时;'
                      f'建议按金属/模板拆成约 {n_batches} 批(每批约 {per_batch} 个作业)分批实例化,'
                      f'或调高机时预算。')
        gates_out['budget'] = {'status': 'blocked', 'estimated_core_hours': est_total,
                               'cap': float(budget_cap_hours), 'suggestion': suggestion}
        return {'ok': False, 'created': [], 'campaign_dir': None,
                'gates': gates_out, 'error': suggestion}
    gates_out['budget'] = {'status': 'pass', 'estimated_core_hours': est_total,
                           'cap': (float(budget_cap_hours) if budget_cap_hours is not None else None)}

    # ── ② 单点先行闸 ──────────────────────────────────────────────
    skip_pilot = (confirm_token == SKIP_PILOT_TOKEN)
    single = jobs_estimate <= 1
    pilot_required = not (single or skip_pilot)
    pilot_id = plan.get('pilot') or (tasks_bp[0]['id'] if tasks_bp else None)

    fp = plan.get('fingerprint')
    fp_hash = plan.get('fingerprint_hash')
    task_objs = []
    for tb in tasks_bp:
        deps = list(tb.get('depends_on') or [])
        # pilot 之外的「源头」节点(无既有依赖者)直接挂 pilot 依赖 → blocked-on-pilot;
        # 其余(如吸附节点)靠对 slab 弛豫的既有依赖间接阻塞在 pilot 之后。
        if pilot_required and pilot_id and tb['id'] != pilot_id and not deps:
            deps = [pilot_id]
        t = schema.new_task(tb['id'], tb['kind'], depends_on=deps,
                            required_checks=list(DEFAULT_CHECKS),
                            is_pilot=(tb['id'] == pilot_id and pilot_required),
                            fingerprint_hash=fp_hash)
        t['origin'] = 'ai_paper'
        ref = {k: tb[k] for k in ('slab', 'adsorbate') if k in tb}
        if ref:
            t['spec_ref'] = ref
        task_objs.append(t)
    created = [t['id'] for t in task_objs]

    if pilot_required:
        gates_out['pilot'] = {'status': 'awaiting', 'pilot': pilot_id,
                              'detail': ('多作业矩阵:先释放代表作业(单点先行),'
                                         '待其 validated/accepted 后再放行 fan-out')}
    elif skip_pilot:
        gates_out['pilot'] = {'status': 'skipped',
                              'detail': f'confirm_token={SKIP_PILOT_TOKEN} 显式跳过单点先行(用户自负其责)'}
    else:
        gates_out['pilot'] = {'status': 'not_required', 'detail': '单作业,无需单点先行'}

    result = {'ok': True, 'created': created, 'gates': gates_out, 'error': None,
              'jobs_estimate': jobs_estimate,
              'campaign_dir': str(schema.campaign_dir(out_root, camp_id))}
    if pilot_required:
        result['pilot'] = {'id': pilot_id}
        result['awaiting'] = 'pilot_validation'
    if dry_run:
        result['dry_run'] = True
        return result

    # ── ③ 写 campaign(全部任务节点 + fingerprint) ─────────────────
    meta_kw = {
        'title': campaign_mods.get('title', 'AI 论文实例化 campaign'),
        'hypothesis': campaign_mods.get('hypothesis', ''),
        'budget_core_hours': campaign_mods.get('budget_core_hours', budget_cap_hours),
        'require_pilot': pilot_required,
        'fingerprint_hash': fp_hash,
        'extra': {'mode': plan.get('mode'), 'origin': 'ai_paper',
                  'expected': plan.get('expected') or {},
                  'reaction_preset': plan.get('reaction_preset'),
                  'sac_params': {**{'nx': 4, 'ny': 4, 'vacuum': 20.0},
                                 **(plan.get('matrix') or {}), **sac_mods}},
    }
    camp = schema.init_campaign(out_root, camp_id, tasks=task_objs, fingerprint=fp, **meta_kw)
    cdir = camp['dir']
    result['campaign_dir'] = cdir

    # ── ④ 决策账本(每步 origin='ai_paper') ───────────────────────
    ledger.record_decision(
        cdir, 'campaign-instantiated',
        f'从 AI 抽取规格表实例化 {len(created)} 个作业节点(mode={plan.get("mode")})',
        'auto', context={'origin': 'ai_paper', 'jobs': len(created)})
    if pilot_required:
        ledger.record_decision(
            cdir, 'pilot-first',
            f'单点先行:先释放代表作业 {pilot_id},其余节点 blocked-on-pilot',
            'auto', context={'origin': 'ai_paper', 'pilot': pilot_id})
    elif skip_pilot:
        ledger.record_decision(
            cdir, 'skip-pilot',
            f'用户以 confirm_token 跳过单点先行,直接 fan-out {len(created)} 个作业',
            'user', context={'origin': 'ai_paper'})
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 全自动编排钩子(GUI 全自动开关调用的引擎步,单步幂等)
# ═══════════════════════════════════════════════════════════════════════════════
def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _contradiction(task, measured, expected, tol):
    """测量值 vs 文献锚点:偏差超容差 → 返回中文平铺 halt 记录,否则 None(科学停 E17)。"""
    default_tol = CONTRADICT_TOL if tol is None else tol
    for metric, mval in (measured or {}).items():
        exp = expected.get(metric)
        if not isinstance(exp, dict):
            continue
        ev = exp.get('value')
        if not (_is_number(mval) and _is_number(ev)):
            continue
        etol = float(exp.get('tol', default_tol) or default_tol)
        dev = abs(float(mval) - float(ev))
        if dev > etol:
            origin = exp.get('origin') or {}
            detail = (f'科学停(contradicts-halt):任务「{task.get("id")}」的 {metric} 计算值 '
                      f'{float(mval):g} 与文献值 {float(ev):g}(出处:第 {origin.get("page", "?")} 页'
                      f'「{origin.get("quote", "")}」)偏差 {dev:g} 超过容差 {etol:g};'
                      f'已平铺呈报,绝不自动判为复现成功或掩盖,请人工裁决。')
            return {'metric': metric, 'measured': float(mval), 'expected': float(ev),
                    'tol': etol, 'deviation': dev, 'detail': detail}
    return None


def autopilot_step(campaign_dir, *, runners, contradict_tol=None):
    """全自动编排单步(单步幂等):读派生就绪任务 → 注入执行器推进 → 三态 + 三门。

    返回 {'actions':[...],'halted','reason'}。红线:`accepted` 只能由确定性 promote_validated +
    accept_gate 通过后 promote_accepted 产生,**本模块绝不直写 accepted**;若执行器回报的测量值
    与 campaign 记录的文献锚点(meta.expected)偏差超阈值 → **contradicts-halt**(halted=True + 中文
    平铺),当步立即停,不进 accept。runners['execute'](task) → {'completed','checks','energy_eV',
    'measured'{metric:val},'error'}(测试用假件)。
    """
    camp = schema.load_campaign(campaign_dir)
    if not camp:
        return {'actions': [], 'halted': True,
                'reason': '未找到 campaign(campaign.yaml 缺失或损坏),无法推进'}
    execute = (runners or {}).get('execute')
    ready = derive.derive_ready(camp)['ready']
    if ready and not callable(execute):
        return {'actions': [], 'halted': True,
                'reason': "runners 缺少可调用的 execute 执行器,无法推进(请注入 runners['execute'])"}
    by_id = schema.tasks_by_id(camp)
    meta = camp.get('meta') or {}
    camp_fp = meta.get('fingerprint_hash')
    expected = meta.get('expected') or {}
    actions = []

    for tid in ready:
        task = by_id.get(tid)
        if task is None:
            continue
        states.mark_running(task, by='autopilot', campaign_dir=campaign_dir)
        schema.save_task(campaign_dir, task)
        res = execute(task) or {}

        if not res.get('completed'):
            reason = str(res.get('error') or 'execute 未报告完成')
            states.mark_failed(task, reason=reason, by='autopilot', campaign_dir=campaign_dir)
            schema.save_task(campaign_dir, task)
            actions.append({'task': tid, 'action': 'failed', 'detail': reason})
            continue

        states.mark_completed(task, by='autopilot', campaign_dir=campaign_dir)
        schema.save_task(campaign_dir, task)

        checks = res.get('checks') or []
        try:
            states.promote_validated(task, checks, by='autopilot', campaign_dir=campaign_dir)
        except ValueError as e:
            schema.save_task(campaign_dir, task)
            actions.append({'task': tid, 'action': 'validate-blocked', 'detail': str(e)})
            continue
        schema.save_task(campaign_dir, task)

        # contradicts-halt(科学停):测量值 vs 文献锚点,超差立即停,绝不 spin
        halt = _contradiction(task, res.get('measured') or {}, expected, contradict_tol)
        if halt:
            ledger.record_event(campaign_dir, 'contradicts_halt', tid, halt)
            ledger.record_decision(campaign_dir, 'contradicts-halt', halt['detail'], 'auto',
                                   context={'origin': 'ai_paper', 'task': tid})
            actions.append({'task': tid, 'action': 'halt', 'detail': halt['detail']})
            return {'actions': actions, 'halted': True, 'reason': halt['detail']}

        # accept_gate(确定性)→ promote_accepted(须出示门裁决,绝不直写 accepted)
        verdict = gates.accept_gate({
            'task_fingerprint_hash': task.get('fingerprint_hash'),
            'campaign_fingerprint_hash': camp_fp,
            'required_checks': checks,
            'energy_eV': res.get('energy_eV'),
        })
        if verdict['status'] in ('pass', 'waived'):
            states.promote_accepted(task, verdict, signed_by=None, campaign_dir=campaign_dir)
            schema.save_task(campaign_dir, task)
            actions.append({'task': tid, 'action': 'accepted'})
        else:
            actions.append({'task': tid, 'action': 'accept-blocked',
                            'detail': verdict['blocking_issues']})

    return {'actions': actions, 'halted': False, 'reason': None}
