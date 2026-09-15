"""LLM 分析层:结构化数据 → 优化提示词 → (可选)DeepSeek 兼容 API → 双语分析。

修掉原版 ai_service.py 的 9 条硬伤(提取报告批评,spec 2026-07-06):
① 绝不硬编码方法学参数——真实 INCAR/KPOINTS 注入(原版把假 ENCUT 写进论文=学术红线)
② 显式符号约定(E_ads 越负吸附越强;E 是电子能非自由能,无 ZPE/熵)
③ 禁止编造文献数值/引用 ④ 数据不足要明说 ⑤ 结构化双语输出(中文解读+英文段落+caveats)
⑥ 单一 client,低温 0.2 ⑦ 输出持久化(报告章节),不再关窗即丢
⑧ key 只进 Windows keyring(service='vcstudio-llm'),config 只存 base_url/model
⑨ 数值以数字传入(不搞 '-2.31eV' 字符串)

纯函数(build_payload/build_prompt/render_export)离线测;HTTP 经 transport= 注入。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import json
import time
import urllib.request

KEYRING_SERVICE = 'vcstudio-llm'
DEFAULT_BASE_URL = 'https://api.deepseek.com/v1/chat/completions'
DEFAULT_MODEL = 'deepseek-chat'

SYSTEM_PROMPT = (
    'You are a computational catalysis expert analyzing VASP DFT adsorption-energy results. '
    'Ground EVERY claim in the provided numbers. Sign convention: E_ads more negative = stronger binding. '
    'The energies are DFT electronic energies (no ZPE/entropy corrections) — do not call them free energies '
    'unless the data explicitly provides ΔG. NEVER invent literature values, citations, or computational '
    'parameters not present in the data. If the data is insufficient for a conclusion, say so explicitly. '
    'Respond in JSON with keys: "analysis_zh" (中文解读,面向课题组讨论,200字内), '
    '"paragraph_en" (one polished manuscript-ready paragraph in English, 120-180 words), '
    '"caveats" (array of strings, honest limitations), "confidence" (high/medium/low).')

# 发刊级默认"分析要求"段(可在设置页覆盖;设置页保存的预设经 config llm.prompt_preset
# 生效)。只承载"分析要求",不含 payload 注入与 JSON 输出契约(那两段在 build_prompt 固定)。
# 刻意不写死 d 带/轨道机理诱导语——是否放行由 build_prompt 按 payload 是否含电子结构数据门控。
DEFAULT_PROMPT_PRESET = (
    'Analysis requirements — organize the reasoning into FOUR explicit parts:\n'
    '(1) Data-reliability check: before interpreting, flag any positive or anomalous E_ads, '
    'incomplete/missing configurations, and inconsistencies in the reported computational '
    'parameters; state whether the dataset is sufficient for the conclusions drawn.\n'
    '(2) Key findings (anchor to numbers): describe the adsorption-strength trend across '
    'configurations while CITING the specific ΔE values; name the strongest- and weakest-binding '
    'sites, and — if a discharge_path is present — the potential-determining step and limiting '
    'potential U_L.\n'
    '(3) Mechanism discussion: explain the observed trend using ONLY the provided data; '
    'do NOT fabricate literature values, citations, or parameters absent from the payload.\n'
    '(4) Limitations & next steps: state honestly what the data cannot support (these are DFT '
    'electronic energies without ZPE/entropy unless ΔG is given) and propose concrete, '
    'prioritized follow-up calculations.\n'
    'Deliver a concise bilingual summary: 中文 in "analysis_zh", polished English in "paragraph_en".')


# 催化剂类型机理语境(移植原版 _CATALYST_CONTEXT 的类型化框架;只给机理概念,
# 不带原版那些无来源的文献数值——守禁编造原则)。按 slab 元素组成启发式判型。
_TM = {'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn', 'Y', 'Zr', 'Nb',
       'Mo', 'Ru', 'Rh', 'Pd', 'Ag', 'W', 'Re', 'Os', 'Ir', 'Pt', 'Au', 'Ta', 'Hf'}


def catalyst_context(elements: list) -> str:
    """slab 元素组成 → 机理语境提示(判不出 → 空串,提示词不加戏)。"""
    els = set(elements or [])
    tms = els & _TM
    if tms and {'N', 'C'} <= els and len(tms) <= 2:
        return ('Catalyst class hint: single/dual-atom catalyst (TM-N-C coordination). '
                'Consider metal-center d-orbital splitting under the N-coordination field, '
                'metal–adsorbate orbital hybridization, and charge transfer at the M site.')
    if tms and 'S' in els and not ({'N', 'C'} & els) and len(tms) == 1:
        return ('Catalyst class hint: transition-metal dichalcogenide (TMD) surface. '
                'Consider basal-plane vs edge-site activity, S-vacancy effects, and '
                'd-band interaction with the adsorbate frontier orbitals.')
    if tms and ('O' in els) and len(tms) <= 2:
        return ('Catalyst class hint: oxide (or oxidized) surface. Consider surface '
                'lattice-oxygen participation, cation oxidation state, and possible '
                'polaronic/charge-transfer effects on adsorption.')
    if len(tms) >= 2:
        return ('Catalyst class hint: bimetallic/heterostructure surface. Consider '
                'ligand and strain effects on the d-band center and site-dependent binding.')
    if tms:
        return ('Catalyst class hint: transition-metal surface. Consider d-band-center '
                'arguments and coordination-number effects on binding strength.')
    return ''


def build_payload(*, project_name: str, delta_rows: list, incar_summary: dict,
                  kpoints=None, path_result: dict | None = None,
                  slab_elements: list | None = None,
                  dos: dict | None = None, bader: dict | None = None) -> dict:
    """项目结果 → 喂给 LLM 的结构化数据(数值为数字;真实计算参数;含失败统计)。

    电子结构数据(dos/bader)仅在显式提供时注入;且**只有**含电子结构数据时才注入
    catalyst_context(d 带/轨道机理语境)——否则 build_prompt 会明示"仅基于能量数据,
    不要推断电子结构机理",防止无 DOS/Bader 依据的机理编造。
    """
    ok_rows = [r for r in delta_rows if r.get('delta_e') is not None]
    payload = {
        'project': project_name,
        'sign_convention': 'E_ads negative = favorable adsorption; units eV',
        'adsorption_energies_eV': {r['name']: round(r['delta_e'], 4) for r in ok_rows},
        'n_configs_total': len(delta_rows),
        'n_configs_with_energy': len(ok_rows),
        'incomplete': [r['name'] for r in delta_rows if r.get('delta_e') is None],
        'computational_parameters': dict(incar_summary or {}),   # 真实 INCAR 键,绝不编造
    }
    if kpoints:
        payload['kpoints'] = list(kpoints)
    if dos:
        payload['dos'] = dict(dos)
    if bader:
        payload['bader'] = dict(bader)
    # catalyst_context 门控:仅当 payload 含电子结构数据(dos/bader)才注入 d 带/轨道语境
    ctx = catalyst_context(slab_elements or [])
    if ctx and (dos or bader):
        payload['catalyst_context'] = ctx
    if path_result:
        payload['discharge_path'] = {
            'delta_G_eV': {s['label']: s['G'] for s in path_result['steps']},
            'pds_index': path_result['pds_index'],
            'limiting_potential_V': path_result['u_l'],
            'mu_li_eV': path_result['mu_li'],
            'note': 'electronic energies, no ZPE/entropy',
        }
    return payload


def active_prompt_preset() -> str:
    """生效中的"分析要求"段:config llm.prompt_preset(非空)> 内置发刊级默认。"""
    preset = _config_llm().get('prompt_preset')
    return preset if isinstance(preset, str) and preset.strip() else DEFAULT_PROMPT_PRESET


def build_prompt(payload: dict, preset: str | None = None) -> str:
    """拼装 user 提示词:固定 payload 注入段 + 可替换的"分析要求"段(preset)+ 电子结构
    机理门控 + 固定 JSON 输出契约。

    门控:payload 含电子结构数据(catalyst_context/dos/bader)才放行 d 带/轨道机理讨论;
    否则明示"仅基于能量数据,不要推断电子结构机理"——避免无 DOS/Bader 依据的机理编造。
    """
    preset = preset if (isinstance(preset, str) and preset.strip()) else active_prompt_preset()
    has_electronic = any(k in payload for k in ('catalyst_context', 'dos', 'bader'))
    if has_electronic:
        gate = (' Electronic-structure data (catalyst_context / DOS / Bader) is provided: you MAY '
                'discuss d-band-center, orbital-hybridization and charge-transfer mechanisms, '
                'strictly grounded in that data.')
    else:
        gate = (' The dataset contains ONLY energies (no DOS/Bader electronic-structure data); '
                'base the discussion strictly on energy/geometry trends and do NOT infer '
                'd-band-center, orbital-hybridization or charge-transfer mechanisms.')
    return (
        'Analyze these VASP adsorption results. Data (JSON):\n'
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + '\n\n' + preset + gate
        + ' Remember: no invented references or parameters. '
        # 键名约束放末尾(recency):Claude 系模型会无视只写在 system 里的 schema 自创结构
        'Output MUST be a single JSON object with EXACTLY these four keys and no others: '
        '"analysis_zh" (string, 中文), "paragraph_en" (string), '
        '"caveats" (array of strings), "confidence" ("high"|"medium"|"low").')


def render_export_md(payload: dict) -> str:
    """提示词包导出(无 key 也能用):system+user 提示词 + 数据,粘任意 AI 即可。"""
    return ('# VASP 结果分析提示词包(粘贴到任意 AI 使用)\n\n'
            '## System prompt\n\n```\n' + SYSTEM_PROMPT + '\n```\n\n'
            '## User prompt\n\n```\n' + build_prompt(payload) + '\n```\n')


# ── key 管理(keyring,不落盘) ─────────────────────────────────────────────────
def save_api_key(key: str):
    import keyring
    keyring.set_password(KEYRING_SERVICE, 'api_key', key)


def load_api_key() -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, 'api_key')
    except Exception:
        return None


def _extract_json(content: str) -> str:
    """从模型输出提取 JSON:Claude 系常包 ```json 围栏或带前后语,response_format
    经网关可能被忽略——取首个 '{' 到末个 '}' 的切片(纯 JSON 时原样通过)。"""
    s = content.strip()
    i, j = s.find('{'), s.rfind('}')
    return s[i:j + 1] if 0 <= i < j else s


# ── API 调用(OpenAI 兼容;transport 可注入) ────────────────────────────────────
def _default_transport(url: str, body: bytes, headers: dict, timeout: int):
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # 4xx/5xx 必须以 (status, body) 返回而不是抛:analyze 靠状态码做
        # 网关方言自适应(400 剥参重试)与降级,吞成异常就全瞎了
        return e.code, e.read()


def _config_llm() -> dict:
    """config.yaml 的 llm 小节(读不到 → 空 dict,绝不抛)。"""
    try:
        from vcstudio.shared.config import load_config
        return dict(load_config().get('llm') or {})
    except Exception:                                    # noqa: BLE001 配置层任何问题都降级默认
        return {}


def probe(*, api_key: str | None = None, base_url: str = '', model: str = '',
          transport=None, timeout: int = 20) -> dict:
    """连通性探测:发一个极小请求(max_tokens≤8),不外发项目数据。返回 {'ok','error'}。

    专供设置页"测试连接":只验端点/密钥/模型可达,不做分析,故不受联网门控约束。
    400 且疑似拒 max_tokens → 剥掉重试一次(部分网关不认该参数)。
    """
    base_url = base_url or _config_llm().get('base_url') or DEFAULT_BASE_URL
    model = model or _config_llm().get('model') or DEFAULT_MODEL
    transport = transport or _default_transport
    key = api_key or load_api_key()
    if not key:
        return {'ok': False, 'error': '未设置 API 密钥(请先在设置页保存密钥再测试)'}
    req = {'model': model, 'max_tokens': 8,
           'messages': [{'role': 'user', 'content': 'ping'}]}
    headers = {'Content-Type': 'application/json; charset=utf-8',
               'Authorization': f'Bearer {key}'}
    for attempt in range(2):
        body = json.dumps(req, ensure_ascii=False).encode('utf-8')
        try:
            status, raw = transport(base_url, body, headers, timeout)
        except Exception as e:                            # noqa: BLE001 网络异常统一降级
            return {'ok': False, 'error': f'请求失败:{e}'}
        if status == 200:
            return {'ok': True, 'error': None}
        text = raw[:200].decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
        if status == 400 and 'max_tokens' in req and attempt == 0:
            req.pop('max_tokens')
            continue
        return {'ok': False, 'error': f'HTTP {status}:{text}'}
    return {'ok': False, 'error': '未知错误'}


def analyze(payload: dict, *, api_key: str | None = None,
            base_url: str = '', model: str = '',
            transport=None, timeout: int = 60,
            allow_external: bool | None = None) -> dict:
    """调 LLM 分析。返回 {'ok', 'analysis_zh','paragraph_en','caveats','confidence','error'}。

    联网门控(默认关闭):allow_external=None 时读 config llm.allow_external(缺省 False);
    未开启则不外发项目数据,返回 skipped 结果并给离线指引(报告层据此降级为提示词包路线)。
    端点/模型解析优先级:显式入参 > config.yaml llm 小节 > 内置默认(DeepSeek)。
    失败不抛(报告章节降级为提示词包指引);温度 0.2(事实性文本);一次重试。
    """
    if allow_external is None:
        allow_external = bool(_config_llm().get('allow_external', False))
    if not allow_external:
        return {'ok': False, 'skipped': True,
                'error': ('未开启联网分析:请在设置页开启「允许将项目数据发送到外部 LLM」'
                          ',或在项目页「导出提示词包」离线使用'),
                'reason': 'llm.allow_external 关闭(默认)'}
    cfg = _config_llm() if not (base_url and model) else {}
    base_url = base_url or cfg.get('base_url') or DEFAULT_BASE_URL
    model = model or cfg.get('model') or DEFAULT_MODEL
    transport = transport or _default_transport
    key = api_key or load_api_key()
    if not key:
        return {'ok': False, 'error': '未配置 API key(设置页 LLM 区保存,或用「导出提示词包」离线路线)'}
    req = {
        'model': model,
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                     {'role': 'user', 'content': build_prompt(payload)}],
    }
    headers = {'Content-Type': 'application/json; charset=utf-8',
               'Authorization': f'Bearer {key}'}
    last_err = ''
    for attempt in range(4):                             # 重试预算(含自适应剥参+网关抖动)
        if attempt:
            time.sleep(1.5)                              # 网关通道轮询抖动(实测 401/502 间歇)缓一拍
        body = json.dumps(req, ensure_ascii=False).encode('utf-8')
        try:
            status, raw = transport(base_url, body, headers, timeout)
        except Exception as e:                           # noqa: BLE001 网络异常统一降级
            last_err = f'请求失败:{e}'
            continue
        if status == 400:
            # 网关方言自适应:Claude 系模型拒收 temperature/response_format
            # (实测 opus-4.8 网关 400 "temperature is deprecated")→ 剥掉重试,不浪费预算
            text = raw[:300].decode('utf-8', errors='replace')
            stripped = False
            for k in ('temperature', 'response_format'):
                if k in req and k in text:
                    req.pop(k)
                    stripped = True
            last_err = f'HTTP 400:{text[:200]}'
            if stripped:
                continue
            break                                        # 400 且无可剥参数 → 无望,直接降级
        if status != 200:
            last_err = f'HTTP {status}:{raw[:200].decode("utf-8", errors="replace")}'
            continue
        try:
            content = json.loads(raw)['choices'][0]['message']['content']
            data = json.loads(_extract_json(content))
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            last_err = f'响应解析失败:{e}'
            continue
        return {'ok': True, 'error': '',
                'analysis_zh': str(data.get('analysis_zh', '')),
                'paragraph_en': str(data.get('paragraph_en', '')),
                'caveats': [str(c) for c in (data.get('caveats') or [])],
                'confidence': str(data.get('confidence', 'medium'))}
    return {'ok': False, 'error': last_err}
