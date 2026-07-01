from vcstudio.gui.runner import submit, poll


def test_submit_ok_puts_result():
    q = submit(lambda a, b: a + b, 2, 3)
    kind, val = q.get(timeout=3)
    assert kind == 'ok' and val == 5


def test_submit_error_puts_exception():
    def boom():
        raise ValueError('nope')
    q = submit(boom)
    kind, err = q.get(timeout=3)
    assert kind == 'error' and isinstance(err, ValueError)


def test_poll_empty_returns_none():
    import queue
    assert poll(queue.Queue()) is None
