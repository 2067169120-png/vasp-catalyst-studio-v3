from __future__ import annotations

import threading
import time
from datetime import datetime
from types import SimpleNamespace

from vcstudio.gui_web.api import Api
from vcstudio.gui_web.pipeline_supervisor import PipelineSupervisor


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError('condition was not reached before timeout')


def test_supervisor_honours_startup_delay_and_publishes_ui_state():
    ticks = []
    published = []
    started_at = time.monotonic()
    supervisor = PipelineSupervisor(
        lambda: {'enabled': True, 'interval_seconds': 0.2},
        lambda: ticks.append(time.monotonic()) or {'ok': True, 'round': len(ticks)},
        published.append,
        startup_delay=0.04,
        config_refresh_interval=0.005,
    )

    assert supervisor.start() is True
    assert supervisor.start() is False
    assert supervisor._thread is not None and supervisor._thread.daemon is True
    time.sleep(0.015)
    assert ticks == []
    _wait_until(lambda: len(ticks) == 1)

    state = supervisor.snapshot()
    assert state['running'] is True
    assert state['paused'] is False
    assert state['tick_running'] is False
    assert state['last_outcome'] == {'ok': True, 'round': 1}
    assert state['last_error'] is None
    assert state['last_started'] and state['last_finished'] and state['next_check']
    assert datetime.fromisoformat(state['last_started'])
    assert datetime.fromisoformat(state['last_finished'])
    assert datetime.fromisoformat(state['next_check'])
    assert ticks[0] - started_at >= 0.03
    assert any(item['tick_running'] for item in published)
    assert supervisor.stop()
    assert supervisor.snapshot()['running'] is False


def test_disabled_supervisor_pauses_and_reconfigure_applies_shorter_interval():
    config = {'enabled': False, 'interval_seconds': 0.3}
    ticks = []
    supervisor = PipelineSupervisor(
        lambda: dict(config),
        lambda: ticks.append(time.monotonic()) or len(ticks),
        lambda _state: None,
        startup_delay=0.3,
        config_refresh_interval=0.005,
    )
    supervisor.start()
    _wait_until(lambda: supervisor.snapshot()['paused'])
    assert supervisor.snapshot()['next_check'] is None
    time.sleep(0.025)
    assert ticks == []

    config['enabled'] = True
    assert supervisor.reconfigure()
    _wait_until(lambda: len(ticks) == 1)
    assert supervisor.snapshot()['paused'] is False

    config['interval_seconds'] = 0.035
    changed_at = time.monotonic()
    assert supervisor.reconfigure()
    _wait_until(lambda: len(ticks) == 2)
    assert ticks[1] - changed_at < 0.12
    assert supervisor.snapshot()['interval_seconds'] == 0.035
    assert supervisor.stop()


def test_wake_never_overlaps_ticks_and_coalesces_repeated_requests():
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    calls = 0

    def tick():
        nonlocal active, maximum_active, calls
        with lock:
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            this_call = calls
        if this_call == 1:
            entered.set()
            assert release.wait(0.8)
        with lock:
            active -= 1
        return this_call

    supervisor = PipelineSupervisor(
        lambda: {'enabled': True, 'interval_seconds': 10},
        tick,
        lambda _state: None,
        startup_delay=0,
        config_refresh_interval=0.01,
    )
    supervisor.start()
    assert entered.wait(0.5)
    assert supervisor.snapshot()['tick_running'] is True
    for _ in range(5):
        assert supervisor.wake()
    time.sleep(0.02)
    with lock:
        assert calls == 1
        assert maximum_active == 1

    release.set()
    _wait_until(lambda: calls == 2)
    with lock:
        assert maximum_active == 1
    assert supervisor.snapshot()['outcome_seq'] == 2
    assert supervisor.stop()


def test_tick_error_is_exposed_and_next_success_clears_it():
    calls = 0

    def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('cluster unavailable')
        return {'ok': True}

    supervisor = PipelineSupervisor(
        lambda: {'enabled': True, 'interval_seconds': 10},
        tick,
        lambda _state: None,
        startup_delay=0,
        config_refresh_interval=0.005,
    )
    supervisor.start()
    _wait_until(lambda: supervisor.snapshot()['last_finished'] is not None)
    failed = supervisor.snapshot()
    assert failed['last_outcome'] is None
    assert failed['last_error'] == 'RuntimeError: cluster unavailable'

    supervisor.wake()
    _wait_until(lambda: supervisor.snapshot()['last_outcome'] == {'ok': True})
    recovered = supervisor.snapshot()
    assert recovered['last_error'] is None
    assert recovered['last_started'] and recovered['last_finished']
    assert supervisor.stop()


def test_config_failure_is_fail_closed_and_recovers_after_wake():
    broken = True
    ticks = []

    def get_config():
        if broken:
            raise OSError('config locked')
        return {'enabled': True, 'interval_seconds': 10}

    supervisor = PipelineSupervisor(
        get_config,
        lambda: ticks.append(1),
        lambda _state: None,
        startup_delay=0,
        config_refresh_interval=0.01,
    )
    supervisor.start()
    _wait_until(lambda: supervisor.snapshot()['last_error'] == 'OSError: config locked')
    assert supervisor.snapshot()['paused'] is True
    assert ticks == []

    broken = False
    supervisor.reconfigure()
    _wait_until(lambda: len(ticks) == 1)
    assert supervisor.stop()


def test_stop_interrupts_long_wait_promptly():
    supervisor = PipelineSupervisor(
        lambda: {'enabled': True, 'interval_seconds': 60},
        lambda: None,
        lambda _state: None,
        startup_delay=60,
        config_refresh_interval=60,
    )
    supervisor.start()
    _wait_until(lambda: supervisor.snapshot()['next_check'] is not None)
    before = time.monotonic()
    assert supervisor.stop(timeout=0.5)
    assert time.monotonic() - before < 0.2
    assert supervisor.is_alive is False


def test_outcome_sequence_distinguishes_ticks_with_the_same_wall_timestamp():
    fixed_wall_time = datetime(2026, 7, 23, 12, 0, 0, 123000)
    calls = []
    supervisor = PipelineSupervisor(
        lambda: {'enabled': True, 'interval_seconds': 10},
        lambda: calls.append(len(calls) + 1) or {'round': len(calls)},
        lambda _state: None,
        startup_delay=0,
        config_refresh_interval=0.005,
        wall_time=lambda: fixed_wall_time,
    )

    supervisor.start()
    _wait_until(lambda: supervisor.snapshot()['outcome_seq'] == 1)
    first = supervisor.snapshot()
    supervisor.wake()
    _wait_until(lambda: supervisor.snapshot()['outcome_seq'] == 2)
    second = supervisor.snapshot()

    assert first['last_finished'] == second['last_finished']
    assert first['last_outcome'] == {'round': 1}
    assert second['last_outcome'] == {'round': 2}
    assert [item['seq'] for item in second['outcome_history']] == [1, 2]
    assert [item['outcome'] for item in second['outcome_history']] == [
        {'round': 1}, {'round': 2},
    ]
    assert all(item['finished'] == first['last_finished']
               for item in second['outcome_history'])
    assert supervisor.stop()


def test_api_owns_one_supervisor_and_reconfigures_it_after_settings_change():
    instances = []

    class FakeSupervisor:
        def __init__(self, get_config, tick, publish_state):
            self.get_config = get_config
            self.tick = tick
            self.publish_state = publish_state
            self.started = False
            self.wakes = 0
            self.reconfigures = 0
            self.stops = 0
            instances.append(self)

        def start(self):
            was_started = self.started
            self.started = True
            return not was_started

        def wake(self):
            self.wakes += 1
            return True

        def reconfigure(self):
            self.reconfigures += 1
            return True

        def stop(self, timeout=1.0):
            self.stops += 1
            return True

        def snapshot(self):
            return {
                'running': self.started,
                'paused': False,
                'tick_running': False,
                'enabled': True,
                'interval_seconds': 300,
                'last_started': None,
                'last_finished': None,
                'next_check': 'later',
                'last_outcome': None,
                'last_error': None,
            }

    ui = {
        'autopilot': True,
        'poll_interval': 5,
        'autopilot_continue': True,
        'autopilot_fetch': True,
        'autopilot_report': True,
        'autopilot_campaigns': False,
    }
    config = SimpleNamespace(
        get_ui_state=lambda: dict(ui),
        set_ui_state=lambda **values: ui.update(values),
    )
    api = Api(config_mod=config, pipeline_supervisor_cls=FakeSupervisor)

    first = api.start_background_services()
    second = api.start_background_services()
    assert first['started'] is True
    assert second['started'] is False
    assert len(instances) == 1
    assert api.pipeline_runtime_status()['state']['running'] is True

    assert api.pipeline_wake()['queued'] is True
    saved = api.autopilot_save(autopilot=True, poll_interval=10)
    assert saved['ok'] is True
    assert instances[0].wakes == 2
    assert instances[0].reconfigures == 1
    assert instances[0].get_config()['interval'] == 10

    assert api.stop_background_services()['stopped'] is True
    assert instances[0].stops == 1
