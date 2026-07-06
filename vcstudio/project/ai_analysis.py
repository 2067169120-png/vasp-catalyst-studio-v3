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


def build_payload(*, project_name: str, delta_rows: list, incar_summary: dict,
                  kpoints=None, path_result: dict | None = None) -> dict:
    """项目结果 → 喂给 LLM 的结构化数据(数值为数字;真实计算参数;含失败统计)。"""
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
    if path_result:
        payload['discharge_path'] = {
            'delta_G_eV': {s['label']: s['G'] for s in path_result['steps']},
            'pds_index': path_result['pds_index'],
            'limiting_potential_V': path_result['u_l'],
            'mu_li_eV': path_result['mu_li'],
            'note': 'electronic energies, no ZPE/entropy',
        }
    return payload


def build_prompt(payload: dict) -> str:
    return (
        'Analyze these VASP adsorption results. Data (JSON):\n'
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + '\n\nTasks: 1) interpret the adsorption-strength trend across configurations; '
          '2) if discharge_path present, interpret the potential-determining step; '
          '3) note anything anomalous (positive E_ads, missing configs). '
          'Remember: JSON output only, no invented references or parameters.')


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


# ── API 调用(OpenAI 兼容;transport 可注入) ────────────────────────────────────
def _default_transport(url: str, body: bytes, headers: dict, timeout: int):
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def analyze(payload: dict, *, api_key: str | None = None,
            base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
            transport=None, timeout: int = 60) -> dict:
    """调 LLM 分析。返回 {'ok', 'analysis_zh','paragraph_en','caveats','confidence','error'}。

    失败不抛(报告章节降级为提示词包指引);温度 0.2(事实性文本);一次重试。
    """
    transport = transport or _default_transport
    key = api_key or load_api_key()
    if not key:
        return {'ok': False, 'error': '未配置 API key(集群页 LLM 区保存,或用"导出提示词包"离线路线)'}
    body = json.dumps({
        'model': model,
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                     {'role': 'user', 'content': build_prompt(payload)}],
    }, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}
    last_err = ''
    for _ in range(2):                                   # 一次重试
        try:
            status, raw = transport(base_url, body, headers, timeout)
        except Exception as e:                           # noqa: BLE001 网络异常统一降级
            last_err = f'请求失败:{e}'
            continue
        if status != 200:
            last_err = f'HTTP {status}:{raw[:200].decode("utf-8", errors="replace")}'
            continue
        try:
            content = json.loads(raw)['choices'][0]['message']['content']
            data = json.loads(content)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            last_err = f'响应解析失败:{e}'
            continue
        return {'ok': True, 'error': '',
                'analysis_zh': str(data.get('analysis_zh', '')),
                'paragraph_en': str(data.get('paragraph_en', '')),
                'caveats': [str(c) for c in (data.get('caveats') or [])],
                'confidence': str(data.get('confidence', 'medium'))}
    return {'ok': False, 'error': last_err}
