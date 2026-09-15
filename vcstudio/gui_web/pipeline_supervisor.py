"""Process-local scheduler for the automatic pipeline.

The web UI used to own the automation timer.  That makes automation disappear
when the page is reloaded and makes it possible for several pages to schedule
the same work.  :class:`PipelineSupervisor` deliberately owns exactly one
daemon thread and calls the injected ``tick`` callback serially.

``get_config`` may return either the backend's existing mapping
``{"enabled": bool, "interval": minutes}`` or a mapping with
``interval_seconds`` (useful for tests and non-UI callers).  ``poll_interval``
is also accepted as a minute value.
"""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any


State = dict[str, Any]


class PipelineSupervisor:
    """Run one non-overlapping automation tick on a configurable schedule.

    The callbacks are intentionally injected so this module has no dependency
    on the API object, configuration storage, pywebview, or network code.

    ``wake()`` requests one prompt tick if automation is enabled.
    ``reconfigure()`` only interrupts the wait so changed settings are applied.
    Configuration is also sampled periodically, so an external config edit is
    eventually noticed even if the caller forgets to call ``reconfigure``.
    """

    def __init__(
        self,
        get_config: Callable[[], Any],
        tick: Callable[[], Any],
        publish_state: Callable[[State], None],
        *,
        startup_delay: float = 3.5,
        default_interval: float = 600.0,
        min_interval: float = 0.01,
        config_refresh_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], datetime] | None = None,
        thread_name: str = 'vcs-pipeline-supervisor',
    ) -> None:
        if not callable(get_config):
            raise TypeError('get_config must be callable')
        if not callable(tick):
            raise TypeError('tick must be callable')
        if not callable(publish_state):
            raise TypeError('publish_state must be callable')
        if startup_delay < 0:
            raise ValueError('startup_delay must be >= 0')
        if default_interval <= 0:
            raise ValueError('default_interval must be > 0')
        if min_interval <= 0:
            raise ValueError('min_interval must be > 0')
        if config_refresh_interval <= 0:
            raise ValueError('config_refresh_interval must be > 0')

        self._get_config = get_config
        self._tick = tick
        self._publish_state = publish_state
        self._startup_delay = float(startup_delay)
        self._default_interval = max(float(default_interval), float(min_interval))
        self._min_interval = float(min_interval)
        self._config_refresh_interval = float(config_refresh_interval)
        self._monotonic = monotonic
        self._wall_time = wall_time or (lambda: datetime.now().astimezone())
        self._thread_name = str(thread_name)

        self._lock = threading.RLock()
        self._publish_lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._force_tick = False
        self._last_finished_monotonic: float | None = None
        self._state: State = {
            'running': False,
            'paused': False,
            'tick_running': False,
            'enabled': None,
            'interval_seconds': None,
            'last_started': None,
            'last_finished': None,
            'next_check': None,
            'outcome_seq': 0,
            'outcome_history': [],
            'last_outcome': None,
            'last_error': None,
        }

    def start(self) -> bool:
        """Start the daemon scheduler once.

        Returns ``True`` when a new thread was started and ``False`` when the
        existing scheduler is already alive.
        """
        # Hold the publish lock through thread creation so subscribers always
        # observe the initial running state before any worker transition.
        with self._publish_lock:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return False
                self._stop_event.clear()
                self._wake_event.clear()
                self._force_tick = False
                self._set_state_locked(
                    running=True,
                    paused=False,
                    tick_running=False,
                    enabled=None,
                    next_check=None,
                )
                thread = threading.Thread(
                    target=self._run,
                    name=self._thread_name,
                    daemon=True,
                )
                self._thread = thread
                state = self._snapshot_locked()
                thread.start()
            self._publish(state)
        return True

    def wake(self) -> bool:
        """Request an immediate tick once automation is enabled."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._force_tick = True
            self._wake_event.set()
            return True

    def reconfigure(self) -> bool:
        """Wake the scheduler to re-read config without forcing an early tick."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._wake_event.set()
            return True

    def stop(self, timeout: float | None = 1.0) -> bool:
        """Signal the scheduler and wait briefly for it to exit.

        Waiting for a future deadline is interruptible and stops immediately.
        A callback already executing cannot be killed safely; in that case this
        method returns ``False`` when ``timeout`` expires, and the daemon thread
        exits as soon as the callback returns.
        """
        if timeout is not None and timeout < 0:
            raise ValueError('timeout must be >= 0 or None')
        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            self._stop_event.set()
            self._wake_event.set()
        if thread is not threading.current_thread():
            thread.join(timeout)
        return not thread.is_alive()

    def snapshot(self) -> State:
        """Return an isolated copy of the latest UI-safe scheduler state."""
        with self._lock:
            return self._snapshot_locked()

    @property
    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._thread is not None and self._thread.is_alive())

    def _run(self) -> None:
        deadline: float | None = None
        deadline_iso: str | None = None
        previous_enabled: bool | None = None
        previous_interval: float | None = None
        first_valid_config = True
        try:
            while not self._stop_event.is_set():
                # Consume the wake that brought us to this iteration.  A wake
                # arriving later remains set and interrupts the next wait.
                self._wake_event.clear()
                try:
                    enabled, interval = self._normalise_config(self._get_config())
                except Exception as exc:                 # noqa: BLE001
                    deadline = None
                    deadline_iso = None
                    previous_enabled = None
                    self._update_state(
                        paused=True,
                        enabled=None,
                        tick_running=False,
                        interval_seconds=None,
                        next_check=None,
                        last_error=self._format_error(exc),
                    )
                    self._wait(self._config_refresh_interval)
                    continue

                now = self._monotonic()
                with self._lock:
                    force_tick = self._force_tick
                    self._force_tick = False

                if not enabled:
                    deadline = None
                    deadline_iso = None
                    previous_enabled = False
                    previous_interval = interval
                    first_valid_config = False
                    self._update_state(
                        paused=True,
                        enabled=False,
                        tick_running=False,
                        interval_seconds=interval,
                        next_check=None,
                    )
                    self._wait(self._config_refresh_interval)
                    continue

                if previous_enabled is None:
                    delay = self._startup_delay if first_valid_config else 0.0
                    deadline = now + delay
                    deadline_iso = self._future_iso(delay)
                elif previous_enabled is False:
                    # Enabling an already-running supervisor should be visible
                    # promptly; the startup delay applies only at process start.
                    deadline = now
                    deadline_iso = self._future_iso(0)
                elif previous_interval != interval:
                    if self._last_finished_monotonic is None:
                        # No tick has completed yet, so preserve startup timing.
                        deadline = deadline if deadline is not None else now
                    else:
                        deadline = max(now, self._last_finished_monotonic + interval)
                    deadline_iso = self._future_iso(max(0.0, deadline - now))

                if force_tick:
                    deadline = now
                    deadline_iso = self._future_iso(0)

                previous_enabled = True
                previous_interval = interval
                first_valid_config = False
                if deadline is None:
                    deadline = now
                    deadline_iso = self._future_iso(0)
                self._update_state(
                    paused=False,
                    enabled=True,
                    interval_seconds=interval,
                    next_check=deadline_iso,
                )

                if self._stop_event.is_set():
                    break
                remaining = deadline - self._monotonic()
                if remaining > 0:
                    self._wait(min(remaining, self._config_refresh_interval))
                    continue

                self._run_tick()
                finished = self._monotonic()
                self._last_finished_monotonic = finished
                deadline = finished + interval
                deadline_iso = self._future_iso(interval)
                self._update_state(next_check=deadline_iso)
        finally:
            self._update_state(
                running=False,
                paused=False,
                tick_running=False,
                enabled=None,
                next_check=None,
            )

    def _run_tick(self) -> None:
        self._update_state(
            tick_running=True,
            last_started=self._now_iso(),
            next_check=None,
        )
        try:
            outcome = self._tick()
        except Exception as exc:                          # noqa: BLE001
            finished = self._now_iso()
            error = self._format_error(exc)
            with self._lock:
                outcome_seq = int(self._state.get('outcome_seq') or 0) + 1
                history = list(self._state.get('outcome_history') or [])
                history.append({
                    'seq': outcome_seq, 'finished': finished,
                    'outcome': None, 'error': error,
                })
                history = history[-20:]
            self._update_state(
                tick_running=False,
                last_finished=finished,
                outcome_seq=outcome_seq,
                outcome_history=history,
                last_outcome=None,
                last_error=error,
            )
        else:
            finished = self._now_iso()
            with self._lock:
                outcome_seq = int(self._state.get('outcome_seq') or 0) + 1
                history = list(self._state.get('outcome_history') or [])
                history.append({
                    'seq': outcome_seq, 'finished': finished,
                    'outcome': outcome, 'error': None,
                })
                history = history[-20:]
            self._update_state(
                tick_running=False,
                last_finished=finished,
                outcome_seq=outcome_seq,
                outcome_history=history,
                last_outcome=outcome,
                last_error=None,
            )

    def _normalise_config(self, raw: Any) -> tuple[bool, float]:
        if isinstance(raw, Mapping):
            get = raw.get
        else:
            def get(key, default=None):
                return getattr(raw, key, default)

        enabled_raw = get('enabled', get('autopilot', False))
        enabled = self._as_bool(enabled_raw)
        seconds = get('interval_seconds')
        if seconds is None:
            minutes = get('poll_interval')
            if minutes is None:
                minutes = get('interval')
            seconds = self._default_interval if minutes is None else float(minutes) * 60.0
        interval = float(seconds)
        if interval <= 0:
            raise ValueError('pipeline interval must be > 0')
        return enabled, max(interval, self._min_interval)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            normalised = value.strip().lower()
            if normalised in {'true', '1', 'yes', 'on'}:
                return True
            if normalised in {'false', '0', 'no', 'off', ''}:
                return False
            raise ValueError(f'invalid enabled value: {value!r}')
        return bool(value)

    def _wait(self, timeout: float) -> None:
        if timeout > 0 and not self._stop_event.is_set():
            self._wake_event.wait(timeout)

    def _update_state(self, **changes: Any) -> None:
        with self._lock:
            changed = self._set_state_locked(**changes)
            state = self._snapshot_locked() if changed else None
        if state is not None:
            self._publish(state)

    def _set_state_locked(self, **changes: Any) -> bool:
        changed = False
        for key, value in changes.items():
            if self._state.get(key) != value:
                self._state[key] = value
                changed = True
        return changed

    def _snapshot_locked(self) -> State:
        try:
            return copy.deepcopy(self._state)
        except Exception:                                 # noqa: BLE001
            # A third-party tick may return an object that cannot be deep-copied.
            # Preserve scheduler health even if the outcome is only shallow-safe.
            return dict(self._state)

    def _publish(self, state: State) -> None:
        with self._publish_lock:
            try:
                self._publish_state(state)
            except Exception:                             # noqa: BLE001
                # Publishing is observational and must never stop automation.
                pass

    def _wall_now(self) -> datetime:
        value = self._wall_time()
        if not isinstance(value, datetime):
            raise TypeError('wall_time must return datetime')
        if value.tzinfo is None:
            return value.astimezone()
        return value

    def _now_iso(self) -> str:
        # Milliseconds are needed as the UI's outcome cursor: a manual wake can
        # legitimately complete twice inside one wall-clock second.
        return self._wall_now().isoformat(timespec='milliseconds')

    def _future_iso(self, delay: float) -> str:
        return (self._wall_now() + timedelta(seconds=max(0.0, delay))).isoformat(
            timespec='milliseconds')

    @staticmethod
    def _format_error(exc: BaseException) -> str:
        return f'{type(exc).__name__}: {exc}'


__all__ = ['PipelineSupervisor']
