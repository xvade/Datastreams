#!/usr/bin/env python3
"""Record actions per minute from macOS input events or a line stream.

The tracker writes one CSV row at each minute boundary for the minute that
just ended. The timestamp in a row is therefore the epoch timestamp of the
boundary at which that row was recorded.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.util
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO


SECONDS_PER_MINUTE = 60
# Small scheduling delays are normal. A larger wall-time/monotonic-time gap
# indicates that the machine slept while the tracker was waiting.
SLEEP_DETECTION_TOLERANCE_SECONDS = 2
# Normal scheduling remains one wait per minute. A macOS power notification
# wakes the wait immediately when the system finishes waking from sleep.
SCHEDULER_WAIT_INTERVAL_SECONDS = SECONDS_PER_MINUTE
CSV_HEADER = ("actions", "timestamp")
APP_FOCUS_HEADER = (
    "app_name",
    "started_at",
    "stopped_at",
    "duration_seconds",
)
# macOS reports ``loginwindow`` as the frontmost process while the user
# session is asleep. It is a system state, not an application-focus session.
IGNORED_FOCUS_APPS = frozenset({"loginwindow"})

# These are the CoreGraphics event types that represent a discrete user
# action. Mouse movement is deliberately excluded: counting every movement
# event would measure pointer noise rather than actions. The values are part
# of the stable macOS CGEvent API.
MACOS_ACTION_EVENT_TYPES = frozenset({1, 3, 10, 22, 25})
MACOS_EVENT_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
MACOS_EVENT_TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF


def minute_start(epoch_seconds: float) -> int:
    """Return the epoch timestamp at the start of the containing minute."""

    # Floor division is intentional: it also gives the correct result for
    # timestamps before the Unix epoch, unlike truncation toward zero.
    return int(epoch_seconds // SECONDS_PER_MINUTE) * SECONDS_PER_MINUTE


def format_timestamp(epoch_seconds: int | float) -> str:
    """Format epoch seconds as local ISO-8601 time with its UTC offset."""

    # Convert through UTC before applying the local zone so timestamps around
    # daylight-saving transitions receive the correct historical offset.
    return datetime.fromtimestamp(
        epoch_seconds,
        tz=timezone.utc,
    ).astimezone().isoformat(timespec="seconds")


def is_ignored_focus_app(app_name: str) -> bool:
    """Return whether macOS's reported app should be omitted from focus data."""

    return app_name.casefold() in IGNORED_FOCUS_APPS


class ActionCounter:
    """Thread-safe action counts grouped by the minute they occurred in."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._counts: dict[int, int] = defaultdict(int)
        self._lock = threading.Lock()

    def record(self, occurred_at: float | None = None) -> None:
        """Count one action, using the supplied time or the current time."""

        with self._lock:
            # Read the live clock while holding the same lock used by
            # ``take``. This prevents an input thread from reading a timestamp
            # just before a boundary and incrementing that bucket after it was
            # already written.
            timestamp = self._clock() if occurred_at is None else occurred_at
            bucket = minute_start(timestamp)
            self._counts[bucket] += 1

    def take(self, bucket: int) -> int:
        """Remove and return the count for *bucket*, returning zero if empty."""

        with self._lock:
            # Popping makes it impossible for a completed minute to be written
            # twice if the scheduler wakes up late and catches up boundaries.
            return self._counts.pop(bucket, 0)


class AppFocusRecorder:
    """Append application-focus sessions to a separate CSV file."""

    def __init__(self, path: Path, stream: TextIO | None = None) -> None:
        self.path = path
        self._stream = stream
        self._owns_stream = stream is None

        if self._stream is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a", newline="", encoding="utf-8")

        self._writer = csv.writer(self._stream)
        self._write_header_if_needed()

    def _write_header_if_needed(self) -> None:
        """Write the four-column header when the destination is empty."""

        assert self._stream is not None
        if self._stream.tell() == 0:
            self._writer.writerow(APP_FOCUS_HEADER)
            self._stream.flush()

    def record(
        self,
        app_name: str,
        started_at: float,
        stopped_at: float,
    ) -> None:
        """Write one completed focus session and flush it immediately."""

        # Keep this guard at the file-writing boundary as well as in the
        # tracker, protecting the CSV if another caller records a raw
        # frontmost-app observation in the future.
        if is_ignored_focus_app(app_name):
            return

        assert self._stream is not None
        duration = max(0.0, stopped_at - started_at)
        self._writer.writerow(
            (
                app_name,
                format_timestamp(started_at),
                format_timestamp(stopped_at),
                f"{duration:.3f}",
            )
        )
        self._stream.flush()

    def close(self) -> None:
        """Close the destination when this recorder opened it."""

        if self._owns_stream and self._stream is not None:
            self._stream.close()

    def __enter__(self) -> "AppFocusRecorder":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


class ProcessSerialNumber(ctypes.Structure):
    """Carbon identifier used to query the frontmost macOS process."""

    _fields_ = (("high", ctypes.c_uint32), ("low", ctypes.c_uint32))


class MacOSFrontmostAppProvider:
    """Read the frontmost application name through macOS Process Manager."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("application focus tracking is only available on macOS")

        app_services_name = ctypes.util.find_library("ApplicationServices")
        foundation_name = ctypes.util.find_library("CoreFoundation")
        if app_services_name is None or foundation_name is None:
            raise RuntimeError("macOS ApplicationServices libraries could not be loaded")

        self._application_services = ctypes.CDLL(app_services_name)
        self._core_foundation = ctypes.CDLL(foundation_name)
        self._application_services.GetFrontProcess.argtypes = [
            ctypes.POINTER(ProcessSerialNumber)
        ]
        self._application_services.GetFrontProcess.restype = ctypes.c_int16
        self._application_services.CopyProcessName.argtypes = [
            ctypes.POINTER(ProcessSerialNumber),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._application_services.CopyProcessName.restype = ctypes.c_int32
        self._core_foundation.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self._core_foundation.CFStringGetCString.restype = ctypes.c_bool
        self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self._core_foundation.CFRelease.restype = None

    def __call__(self) -> str:
        """Return the name of the current frontmost app."""

        process = ProcessSerialNumber()
        if self._application_services.GetFrontProcess(ctypes.byref(process)) != 0:
            return "Unknown"

        process_name = ctypes.c_void_p()
        if (
            self._application_services.CopyProcessName(
                ctypes.byref(process),
                ctypes.byref(process_name),
            )
            != 0
            or not process_name
        ):
            return "Unknown"

        try:
            buffer = ctypes.create_string_buffer(256)
            if not self._core_foundation.CFStringGetCString(
                process_name,
                buffer,
                len(buffer),
                0x08000100,  # kCFStringEncodingUTF8
            ):
                return "Unknown"
            return buffer.value.decode("utf-8", errors="replace") or "Unknown"
        finally:
            self._core_foundation.CFRelease(process_name)


class AppFocusTracker:
    """Track frontmost-app sessions and write them when focus changes."""

    def __init__(
        self,
        recorder: AppFocusRecorder,
        provider: Callable[[], str] | None = None,
        clock=time.time,
        poll_interval: float = 0.5,
    ) -> None:
        self.recorder = recorder
        self._provider = provider
        self._clock = clock
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current_app: str | None = None
        self._started_at: float | None = None

    def start(self) -> None:
        """Start tracking with the application currently in the foreground."""

        if self._provider is None:
            self._provider = MacOSFrontmostAppProvider()
        initial_app = self._provider()
        # The machine may be starting the tracker while loginwindow is still
        # reported as frontmost. Wait for a real application instead of
        # creating a session that represents sleep or the lock screen.
        self._current_app = None if is_ignored_focus_app(initial_app) else initial_app
        self._started_at = self._clock() if self._current_app is not None else None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="application-focus-input",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop tracking and close the final active focus session."""

        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._finish_current_session(self._clock())

    def pause(self, stopped_at: float | None = None) -> None:
        """Close the active session immediately before system sleep."""

        self._finish_current_session(
            self._clock() if stopped_at is None else stopped_at
        )

    def resume(self, started_at: float | None = None) -> None:
        """Start a new session after system wake."""

        assert self._provider is not None
        app_name = self._provider()
        started_at = self._clock() if started_at is None else started_at
        # Use the same transition path as polling. In particular, do not
        # create a session if wake initially reports loginwindow.
        self.observe(app_name, started_at)

    def _run(self) -> None:
        """Poll only the frontmost app; rows are written on transitions."""

        assert self._provider is not None
        while not self._stop_event.wait(self._poll_interval):
            self.observe(self._provider(), self._clock())

    def observe(self, app_name: str, observed_at: float | None = None) -> None:
        """Apply a frontmost-app observation, closing a changed session."""

        observed_at = self._clock() if observed_at is None else observed_at
        with self._lock:
            if is_ignored_focus_app(app_name):
                # Treat loginwindow as a boundary that ends the prior real-app
                # session, but never as a session to start or retain.
                if self._current_app is not None and self._started_at is not None:
                    self.recorder.record(
                        self._current_app,
                        self._started_at,
                        observed_at,
                    )
                self._current_app = None
                self._started_at = None
                return
            if self._current_app is None:
                self._current_app = app_name
                self._started_at = observed_at
                return
            if self._current_app == app_name:
                return
            if self._current_app is not None and self._started_at is not None:
                self.recorder.record(self._current_app, self._started_at, observed_at)
            self._current_app = app_name
            self._started_at = observed_at

    def _finish_current_session(self, stopped_at: float) -> None:
        with self._lock:
            if self._current_app is not None and self._started_at is not None:
                self.recorder.record(
                    self._current_app,
                    self._started_at,
                    stopped_at,
                )
                self._current_app = None
                self._started_at = None


class MacOSEventSource:
    """Count global keyboard and mouse actions using macOS CoreGraphics.

    This uses ctypes instead of a package such as PyObjC so a fresh Python
    installation can run the tracker without an additional dependency. macOS
    will ask the user to grant this process Accessibility/Input Monitoring
    permission the first time an event tap is used.
    """

    _CALLBACK = ctypes.CFUNCTYPE(
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    _POWER_CALLBACK = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )

    def __init__(
        self,
        counter: ActionCounter,
        wake_event: threading.Event | None = None,
        on_sleep: Callable[[float], None] | None = None,
        on_wake: Callable[[float], None] | None = None,
    ) -> None:
        self.counter = counter
        self._wake_event = wake_event
        self._on_sleep = on_sleep
        self._on_wake = on_wake
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._run_loop: ctypes.c_void_p | None = None
        self._event_tap: ctypes.c_void_p | None = None
        self._callback = self._CALLBACK(self._handle_event)
        self._startup_error: BaseException | None = None
        self._core_graphics: ctypes.CDLL | None = None
        self._core_foundation: ctypes.CDLL | None = None
        self._iokit: ctypes.CDLL | None = None
        self._power_callback = self._POWER_CALLBACK(self._handle_power_message)
        self._power_root_port: int | None = None
        self._power_notify_port: ctypes.c_void_p | None = None
        self._power_notifier = ctypes.c_uint32(0)
        self._power_source: ctypes.c_void_p | None = None
        self._power_common_modes: ctypes.c_void_p | None = None

    @staticmethod
    def event_mask() -> int:
        """Return the CoreGraphics bit mask for the tracked event types."""

        return sum(1 << event_type for event_type in MACOS_ACTION_EVENT_TYPES)

    def start(self) -> None:
        """Start the event tap and wait until it is ready to receive events."""

        if sys.platform != "darwin":
            raise RuntimeError("the macOS event source is only available on macOS")

        self._thread = threading.Thread(
            target=self._run,
            name="macos-action-input",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("timed out while starting the macOS event source")
        if self._startup_error is not None:
            raise RuntimeError(str(self._startup_error)) from self._startup_error

    def stop(self) -> None:
        """Stop the event tap and wait briefly for its thread to exit."""

        if self._run_loop is not None and self._core_foundation is not None:
            self._core_foundation.CFRunLoopStop(self._run_loop)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        """Create the event tap on its run-loop-owning thread."""

        source = None
        try:
            graphics_name = ctypes.util.find_library("ApplicationServices")
            foundation_name = ctypes.util.find_library("CoreFoundation")
            if graphics_name is None or foundation_name is None:
                raise RuntimeError("macOS CoreGraphics libraries could not be loaded")

            graphics = ctypes.CDLL(graphics_name)
            foundation = ctypes.CDLL(foundation_name)
            self._core_graphics = graphics
            self._core_foundation = foundation

            graphics.CGEventTapCreate.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint64,
                self._CALLBACK,
                ctypes.c_void_p,
            ]
            graphics.CGEventTapCreate.restype = ctypes.c_void_p
            graphics.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            graphics.CGEventTapEnable.restype = None

            foundation.CFRunLoopGetCurrent.restype = ctypes.c_void_p
            foundation.CFRunLoopAddSource.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            foundation.CFRunLoopAddSource.restype = None
            foundation.CFRunLoopRemoveSource.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            foundation.CFRunLoopRemoveSource.restype = None
            foundation.CFRunLoopRun.restype = None
            foundation.CFRunLoopStop.argtypes = [ctypes.c_void_p]
            foundation.CFRunLoopStop.restype = None
            foundation.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
            foundation.CFMachPortInvalidate.restype = None
            foundation.CFMachPortCreateRunLoopSource.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_long,
            ]
            foundation.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
            foundation.CFRelease.argtypes = [ctypes.c_void_p]
            foundation.CFRelease.restype = None

            # CGEventTapCreate(location=HID, placement=head, listen-only) is
            # suitable for observing all user sessions without modifying an
            # event before the foreground application receives it.
            self._event_tap = graphics.CGEventTapCreate(
                0,
                0,
                1,
                self.event_mask(),
                self._callback,
                None,
            )
            if not self._event_tap:
                raise RuntimeError(
                    "macOS denied the event tap; grant Accessibility/Input "
                    "Monitoring permission to the terminal or Python process"
                )

            source = foundation.CFMachPortCreateRunLoopSource(
                None,
                self._event_tap,
                0,
            )
            if not source:
                raise RuntimeError("macOS could not create the event tap run-loop source")

            self._run_loop = foundation.CFRunLoopGetCurrent()
            common_modes = ctypes.c_void_p.in_dll(
                foundation, "kCFRunLoopCommonModes"
            )
            foundation.CFRunLoopAddSource(self._run_loop, source, common_modes)
            self._register_power_notifications(foundation, common_modes)
            graphics.CGEventTapEnable(self._event_tap, True)
            self._ready.set()
            foundation.CFRunLoopRun()
        except BaseException as exc:  # Propagate startup failures to start().
            self._startup_error = exc
            self._ready.set()
        finally:
            self._unregister_power_notifications()
            if self._core_foundation is not None and source is not None:
                self._core_foundation.CFRelease(source)
            if self._core_foundation is not None and self._event_tap is not None:
                assert self._core_graphics is not None
                self._core_foundation.CFMachPortInvalidate(self._event_tap)
                self._core_foundation.CFRelease(self._event_tap)
                self._event_tap = None
            self._run_loop = None

    def _register_power_notifications(
        self,
        foundation: ctypes.CDLL,
        common_modes: ctypes.c_void_p,
    ) -> None:
        """Wake the scheduler from its minute wait when macOS wakes."""

        if self._wake_event is None:
            return

        iokit_name = ctypes.util.find_library("IOKit")
        if iokit_name is None:
            raise RuntimeError("macOS IOKit could not be loaded")

        iokit = ctypes.CDLL(iokit_name)
        self._iokit = iokit
        iokit.IORegisterForSystemPower.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            self._POWER_CALLBACK,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
        iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
        iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        iokit.IODeregisterForSystemPower.argtypes = [
            ctypes.POINTER(ctypes.c_uint32)
        ]
        iokit.IODeregisterForSystemPower.restype = ctypes.c_int32
        iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
        iokit.IOServiceClose.restype = ctypes.c_int32
        iokit.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
        iokit.IONotificationPortDestroy.restype = None
        iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_int64]
        iokit.IOAllowPowerChange.restype = ctypes.c_int32

        self._power_notify_port = ctypes.c_void_p()
        self._power_root_port = iokit.IORegisterForSystemPower(
            None,
            ctypes.byref(self._power_notify_port),
            self._power_callback,
            ctypes.byref(self._power_notifier),
        )
        if not self._power_root_port:
            self._power_notify_port = None
            raise RuntimeError("macOS power notifications could not be registered")

        self._power_source = iokit.IONotificationPortGetRunLoopSource(
            self._power_notify_port
        )
        if not self._power_source:
            raise RuntimeError("macOS power notification run-loop source is unavailable")
        self._power_common_modes = common_modes
        foundation.CFRunLoopAddSource(self._run_loop, self._power_source, common_modes)

    def _unregister_power_notifications(self) -> None:
        """Release the IOKit power notification resources during shutdown."""

        if self._iokit is None:
            return
        if (
            self._power_source is not None
            and self._run_loop is not None
            and self._power_common_modes is not None
            and self._core_foundation is not None
        ):
            self._core_foundation.CFRunLoopRemoveSource(
                self._run_loop,
                self._power_source,
                self._power_common_modes,
            )
        if self._power_root_port is not None:
            self._iokit.IODeregisterForSystemPower(
                ctypes.byref(self._power_notifier)
            )
            self._iokit.IOServiceClose(self._power_root_port)
            self._power_root_port = None
        if self._power_notify_port is not None:
            # IONotificationPortDestroy also disposes of the run-loop source.
            self._iokit.IONotificationPortDestroy(self._power_notify_port)
            self._power_notify_port = None
        self._power_source = None
        self._power_common_modes = None

    def _handle_power_message(
        self,
        _refcon: ctypes.c_void_p,
        _service: int,
        message_type: int,
        message_argument: ctypes.c_void_p,
    ) -> None:
        """Acknowledge sleep and wake the minute scheduler after resume."""

        if self._iokit is None or self._power_root_port is None:
            return

        if message_type in (0x00000000, 0x00000001):
            # kIOMessageCanSystemSleep and kIOMessageSystemWillSleep must be
            # acknowledged or macOS can delay sleep for up to 30 seconds.
            if message_type == 0x00000001 and self._on_sleep is not None:
                # Notify only for SystemWillSleep. CanSystemSleep may be
                # followed by a canceled sleep and must not close a session.
                self._on_sleep(time.time())
            notification_id = int(message_argument or 0)
            self._iokit.IOAllowPowerChange(
                self._power_root_port,
                notification_id,
            )
        elif message_type == 0x00000003:
            # kIOMessageSystemHasPoweredOn: the process is running again.
            if self._wake_event is not None:
                self._wake_event.set()
            if self._on_wake is not None:
                self._on_wake(time.time())

    def _handle_event(
        self,
        _proxy: ctypes.c_void_p,
        event_type: int,
        event: ctypes.c_void_p,
        _refcon: ctypes.c_void_p,
    ) -> ctypes.c_void_p:
        """Count an event and return it unchanged to the foreground app."""

        if event_type in (
            MACOS_EVENT_TAP_DISABLED_BY_TIMEOUT,
            MACOS_EVENT_TAP_DISABLED_BY_USER_INPUT,
        ):
            if self._core_graphics is not None and self._event_tap is not None:
                self._core_graphics.CGEventTapEnable(self._event_tap, True)
        elif event_type in MACOS_ACTION_EVENT_TYPES:
            self.counter.record()
        return event


class CsvRecorder:
    """Append minute rows to a CSV file and flush each completed row."""

    def __init__(self, path: Path, stream: TextIO | None = None) -> None:
        self.path = path
        self._stream = stream
        self._owns_stream = stream is None

        if self._stream is None:
            # Creating the parent directory is convenient for a fresh install
            # and avoids making callers prepare an output directory manually.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a", newline="", encoding="utf-8")

        self._writer = csv.writer(self._stream)
        self._write_header_if_needed()

    def _write_header_if_needed(self) -> None:
        """Write the stable two-column header when the destination is empty."""

        assert self._stream is not None
        if self._stream.tell() == 0:
            self._writer.writerow(CSV_HEADER)
            self._stream.flush()

    def record(self, actions: int, recorded_at: int) -> None:
        """Append one row and flush it so each minute is visible promptly."""

        assert self._stream is not None
        self._writer.writerow((actions, format_timestamp(recorded_at)))
        self._stream.flush()

    def close(self) -> None:
        """Close the destination when this recorder opened it."""

        if self._owns_stream and self._stream is not None:
            self._stream.close()

    def __enter__(self) -> "CsvRecorder":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()


class MinuteTracker:
    """Coordinate input counting with on-the-minute CSV recording."""

    def __init__(
        self,
        recorder: CsvRecorder,
        counter: ActionCounter | None = None,
        clock=time.time,
        monotonic_clock=time.monotonic,
        wake_event: threading.Event | None = None,
    ) -> None:
        self.recorder = recorder
        self.counter = counter or ActionCounter(clock=clock)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._wake_event = wake_event

    @staticmethod
    def next_boundary(epoch_seconds: float) -> int:
        """Return the next minute boundary after an epoch timestamp."""

        return minute_start(epoch_seconds) + SECONDS_PER_MINUTE

    def record_boundary(self, boundary: int) -> None:
        """Write the minute immediately preceding *boundary*."""

        actions = self.counter.take(boundary - SECONDS_PER_MINUTE)
        self.recorder.record(actions, boundary)

    def record_due_boundaries(
        self,
        boundary: int,
        now: float,
        system_slept: bool = False,
        sleep_transition_boundary: int | None = None,
    ) -> int:
        """Record due minutes and return the next boundary to wait for.

        When the system slept, the first missed boundary closes the minute in
        which sleep began. Later missed boundaries represent minutes spent
        entirely asleep, so emitting zero rows for them would be misleading.
        Resume from the next boundary after wake instead.
        """

        if system_slept:
            # If wake happened before the next boundary, keep waiting for
            # that boundary; the current minute still needs to be completed.
            if boundary > now:
                return boundary

            # Use the estimated sleep-transition minute when available. This
            # is more precise than assuming the scheduler's first missed
            # boundary always belongs to the minute in which sleep began.
            transition = sleep_transition_boundary or boundary
            if transition <= now:
                self.record_boundary(transition)
            return self.next_boundary(now)

        while boundary <= now:
            self.record_boundary(boundary)
            boundary += SECONDS_PER_MINUTE
        return boundary

    def run(self, stop_event: threading.Event) -> None:
        """Run until stopped, writing one row for every completed minute."""

        previous_wall_time = self._clock()
        previous_monotonic_time = self._monotonic_clock()
        boundary = self.next_boundary(previous_wall_time)
        while not stop_event.is_set():
            # Recalculate the delay after every wake-up so scheduler delays do
            # not accumulate and the row timestamp remains the true boundary.
            delay = max(0.0, boundary - self._clock())
            if self._wake_event is None:
                if stop_event.wait(min(delay, SCHEDULER_WAIT_INTERVAL_SECONDS)):
                    return
            else:
                # Normal scheduling still waits no more than one minute. The
                # power monitor sets this event as soon as macOS wakes, which
                # interrupts the wait without tight polling.
                if self._wake_event.is_set():
                    self._wake_event.clear()
                elif self._wake_event.wait(
                    min(delay, SCHEDULER_WAIT_INTERVAL_SECONDS)
                ):
                    if stop_event.is_set():
                        return
                    self._wake_event.clear()

            # Catch up ordinary scheduling delays, while treating a wall-clock
            # jump without matching uptime as system sleep.
            now = self._clock()
            monotonic_now = self._monotonic_clock()
            wall_elapsed = now - previous_wall_time
            monotonic_elapsed = monotonic_now - previous_monotonic_time
            system_slept = (
                wall_elapsed - monotonic_elapsed
                > SLEEP_DETECTION_TOLERANCE_SECONDS
            )
            sleep_duration = max(0.0, wall_elapsed - monotonic_elapsed)
            sleep_transition_boundary = minute_start(
                now - sleep_duration
            ) + SECONDS_PER_MINUTE
            boundary = self.record_due_boundaries(
                boundary,
                now,
                system_slept,
                sleep_transition_boundary,
            )
            previous_wall_time = now
            previous_monotonic_time = monotonic_now


def consume_actions(input_stream: TextIO, counter: ActionCounter) -> None:
    """Count every non-empty input line until the input stream reaches EOF."""

    for line in input_stream:
        # Blank lines are ignored so scripts can format their event stream
        # without changing the resulting action count.
        if line.strip():
            counter.record()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Count user actions and write minute CSV rows."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("apm.csv"),
        help="CSV destination (default: apm.csv)",
    )
    parser.add_argument(
        "--app-output",
        type=Path,
        default=Path("app_focus.csv"),
        help="application-focus CSV destination (default: app_focus.csv)",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "macos", "stdin"),
        default="auto",
        help=(
            "input source: auto uses the macOS event tap for an interactive "
            "terminal and stdin otherwise (default: auto)"
        ),
    )
    return parser


def selected_source(source: str, input_stream: TextIO = sys.stdin) -> str:
    """Resolve ``auto`` to a concrete input source for the current process."""

    if source != "auto":
        return source
    if sys.platform == "darwin" and input_stream.isatty():
        return "macos"
    return "stdin"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stop_event = threading.Event()
    scheduler_event = threading.Event()

    def request_stop(_signum, _frame) -> None:
        # Signal handlers run on the main thread; Event lets the timer wake
        # immediately while the input reader, if blocked, remains a daemon.
        stop_event.set()
        scheduler_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with CsvRecorder(args.output) as recorder, AppFocusRecorder(args.app_output) as focus_recorder:
        source_name = selected_source(args.source)
        event_source: MacOSEventSource | None = None
        focus_tracker: AppFocusTracker | None = None
        if source_name == "macos":
            counter = ActionCounter()
            tracker = MinuteTracker(
                recorder,
                counter=counter,
                wake_event=scheduler_event,
            )
        else:
            tracker = MinuteTracker(recorder, wake_event=scheduler_event)
            input_thread = threading.Thread(
                target=consume_actions,
                args=(sys.stdin, tracker.counter),
                name="action-input",
                daemon=True,
            )
            input_thread.start()

        if sys.platform == "darwin":
            focus_tracker = AppFocusTracker(focus_recorder)
            focus_tracker.start()

        if source_name == "macos":
            event_source = MacOSEventSource(
                counter,
                scheduler_event,
                on_sleep=focus_tracker.pause if focus_tracker is not None else None,
                on_wake=focus_tracker.resume if focus_tracker is not None else None,
            )
            event_source.start()

        print(
            f"Recording actions from {source_name} to {args.output}; "
            f"app focus to {args.app_output}. Press Ctrl-C to stop.",
            file=sys.stderr,
        )
        try:
            tracker.run(stop_event)
        finally:
            if event_source is not None:
                event_source.stop()
            if focus_tracker is not None:
                focus_tracker.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
