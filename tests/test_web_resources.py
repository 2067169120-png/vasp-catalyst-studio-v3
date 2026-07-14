import sys
from pathlib import Path

from vcstudio.gui_web import resources


def test_asset_dir_dev_mode():
    d = resources.asset_dir()
    assert d.name == 'assets' and d.is_dir()


def test_asset_dir_frozen(monkeypatch, tmp_path):
    (tmp_path / 'vcstudio_assets').mkdir()
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert resources.asset_dir() == tmp_path / 'vcstudio_assets'


def test_index_html_points_into_asset_dir():
    p = Path(resources.index_html())
    assert p.name == 'index.html' and p.parent == resources.asset_dir()
