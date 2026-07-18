"""molbuild.ocsr 测试:DECIMER 探测/识别(假模块注入)+ RDKit SVG 降级(假模块注入)。"""
import importlib.util
import types

import pytest

from vcstudio.molbuild import ocsr

_HAS_RDKIT = importlib.util.find_spec('rdkit') is not None


def _fake_decimer(predict):
    """构造假 DECIMER 模块(predict 为 predict_SMILES 的行为函数)。"""
    return types.SimpleNamespace(predict_SMILES=predict)


def _fake_rdkit(*, mol_for=lambda smi: object(), svg='<svg>ok</svg>'):
    """构造假 (Chem, rdMolDraw2D):MolFromSmiles + MolDraw2DSVG 绘图链。"""
    class _Drawer:
        def __init__(self, w, h):
            self.w, self.h = w, h
            self._text = svg

        def DrawMolecule(self, mol):
            self._text = f'<svg width="{self.w}" height="{self.h}">{svg}</svg>'

        def FinishDrawing(self):
            pass

        def GetDrawingText(self):
            return self._text

    chem = types.SimpleNamespace(MolFromSmiles=mol_for)
    rd_draw = types.SimpleNamespace(MolDraw2DSVG=lambda w, h: _Drawer(w, h))
    return chem, rd_draw


# ── probe ────────────────────────────────────────────────────────────────────

def test_probe_missing_decimer(monkeypatch):
    monkeypatch.setattr(ocsr, '_lazy_decimer', lambda: None)
    r = ocsr.probe()
    assert r['available'] is False
    assert 'pip install decimer' in r['detail']


def test_probe_available(monkeypatch):
    monkeypatch.setattr(ocsr, '_lazy_decimer', lambda: _fake_decimer(lambda p: 'CCO'))
    r = ocsr.probe()
    assert r['available'] is True and 'DECIMER' in r['detail']


# ── image_to_smiles ────────────────────────────────────────────────────────────

def test_image_to_smiles_missing_decimer(monkeypatch, tmp_path):
    monkeypatch.setattr(ocsr, '_lazy_decimer', lambda: None)
    img = tmp_path / 'm.png'
    img.write_bytes(b'\x89PNG')
    r = ocsr.image_to_smiles(str(img))
    assert not r['ok'] and 'decimer' in r['error'] and r['smiles'] == ''


def test_image_to_smiles_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ocsr, '_lazy_decimer',
                        lambda: _fake_decimer(lambda p: '  CCO  '))
    img = tmp_path / 'm.png'
    img.write_bytes(b'\x89PNG')
    r = ocsr.image_to_smiles(str(img))
    assert r['ok'] and r['smiles'] == 'CCO'          # 前后空白被裁
    assert isinstance(r['elapsed_ms'], float) and r['elapsed_ms'] >= 0.0


def test_image_to_smiles_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ocsr, '_lazy_decimer', lambda: _fake_decimer(lambda p: 'CCO'))
    r = ocsr.image_to_smiles(str(tmp_path / 'nope.png'))
    assert not r['ok'] and '图片文件不存在' in r['error']


def test_image_to_smiles_predict_raises(monkeypatch, tmp_path):
    def _boom(p):
        raise RuntimeError('TF backend error')

    monkeypatch.setattr(ocsr, '_lazy_decimer', lambda: _fake_decimer(_boom))
    img = tmp_path / 'm.png'
    img.write_bytes(b'x')
    r = ocsr.image_to_smiles(str(img))
    assert not r['ok'] and 'DECIMER 识别失败' in r['error']


def test_image_to_smiles_empty_result(monkeypatch, tmp_path):
    monkeypatch.setattr(ocsr, '_lazy_decimer', lambda: _fake_decimer(lambda p: '   '))
    img = tmp_path / 'm.png'
    img.write_bytes(b'x')
    r = ocsr.image_to_smiles(str(img))
    assert not r['ok'] and '空 SMILES' in r['error']


# ── smiles_svg ─────────────────────────────────────────────────────────────────

def test_smiles_svg_missing_rdkit(monkeypatch):
    monkeypatch.setattr(ocsr, '_lazy_rdkit', lambda: None)
    r = ocsr.smiles_svg('CCO')
    assert not r['ok'] and 'rdkit' in r['error'] and r['svg'] == ''


def test_smiles_svg_success(monkeypatch):
    monkeypatch.setattr(ocsr, '_lazy_rdkit', lambda: _fake_rdkit())
    r = ocsr.smiles_svg('CCO')
    assert r['ok'] and '<svg' in r['svg']
    assert 'width="400"' in r['svg'] and 'height="300"' in r['svg']    # 默认 400×300


def test_smiles_svg_bad_smiles(monkeypatch):
    monkeypatch.setattr(ocsr, '_lazy_rdkit',
                        lambda: _fake_rdkit(mol_for=lambda smi: None))
    r = ocsr.smiles_svg('not-a-smiles')
    assert not r['ok'] and 'SMILES 解析失败' in r['error']


# ── 真实冒烟(仅本机装了 rdkit 时)────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_RDKIT, reason='未安装 rdkit,跳过真实 SVG 冒烟')
def test_real_smiles_svg_ethanol():
    r = ocsr.smiles_svg('CCO')
    assert r['ok'] and '<svg' in r['svg'].lower()
