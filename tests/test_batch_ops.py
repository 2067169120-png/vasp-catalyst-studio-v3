"""batch_ops 是 UI 无关线程体:两个 GUI 共用,绝不 import tkinter。"""
import sys

from vcstudio.cluster import batch_ops


def test_no_tkinter_dependency():
    assert 'tkinter' not in sys.modules or True  # 见下:检查模块源
    import inspect
    src = inspect.getsource(batch_ops)
    assert 'tkinter' not in src


def test_filter_continuable_empty():
    eligible, skipped = batch_ops.filter_continuable([])
    assert eligible == [] and skipped == 0


def test_public_surface():
    for name in ('submit_batch', 'fetch_batch', 'continue_batch',
                 'refresh_batch', 'queue_detail', 'tune_batch'):
        assert callable(getattr(batch_ops, name))
