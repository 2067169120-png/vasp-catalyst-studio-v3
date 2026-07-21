"""多目录专用计算器的最小可追溯 HTML 报告。"""
from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
import time
from pathlib import Path

_FILES = ('job.yaml', 'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR', 'CONTCAR',
          'OSZICAR', 'OUTCAR', 'CHGCAR', 'CHGDIFF.vasp')
_MAX_HASH = 256 * 1024 * 1024


def _evidence(job_dirs) -> list[dict]:
    rows = []
    for raw in job_dirs or ():
        root = Path(str(raw)).resolve()
        files = []
        for name in _FILES:
            path = root / name
            if not path.is_file():
                continue
            size = path.stat().st_size
            digest = None
            note = None
            if size <= _MAX_HASH:
                h = hashlib.sha256()
                with path.open('rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        h.update(block)
                digest = h.hexdigest()
            else:
                note = f'文件超过 {_MAX_HASH} bytes，未在交互报告中哈希'
            files.append({'name': name, 'bytes': size, 'sha256': digest, 'note': note})
        rows.append({'directory': str(root), 'files': files})
    return rows


def write(title: str, result: dict, job_dirs, out_path: str) -> str:
    """把计算器结构化返回值与关键文件指纹原子写入 HTML。"""
    target = Path(out_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'title': str(title),
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'result': result,
        'evidence': _evidence(job_dirs),
    }
    escaped = html.escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(str(title))}</title><style>body{{font:15px/1.6 sans-serif;max-width:980px;margin:36px auto;padding:0 24px}}pre{{background:#f5f6f8;padding:16px;overflow:auto}}</style></head><body>
<h1>{html.escape(str(title))}</h1><p>本报告保存计算器原始返回值、输入目录和关键文件 SHA256；警告与未验证项不会被隐藏。</p>
<pre>{escaped}</pre></body></html>'''
    fd, tmp = tempfile.mkstemp(prefix='.vcstudio-report-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(document)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(target)
