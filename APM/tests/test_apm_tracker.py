import csv
import io
import tempfile
import unittest
from pathlib import Path

from apm_tracker import (
    ActionCounter,
    AppFocusRecorder,
    AppFocusTracker,
    CsvRecorder,
    MACOS_ACTION_EVENT_TYPES,
    MinuteTracker,
    consume_actions,
    format_timestamp,
    minute_start,
    selected_source,
)


class TestActionCounter(unittest.TestCase):
    def test_minute_start_aligns_to_epoch_minute(self):
        self.assertEqual(minute_start(125), 120)
        self.assertEqual(minute_start(179.9), 120)

    def test_counts_are_separated_by_minute(self):
        counter = ActionCounter(clock=lambda: 61)
        counter.record()
        counter.record(119.9)
        counter.record(120)

        self.assertEqual(counter.take(60), 2)
        self.assertEqual(counter.take(120), 1)
        self.assertEqual(counter.take(120), 0)


class TestCsvRecorder(unittest.TestCase):
    def test_timestamp_is_iso8601_with_utc_offset(self):
        timestamp = format_timestamp(120)

        self.assertRegex(
            timestamp,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
        )

    def test_writes_header_and_rows(self):
        destination = io.StringIO()
        with CsvRecorder(Path("unused.csv"), destination) as recorder:
            recorder.record(7, 120)

        destination.seek(0)
        self.assertEqual(
            list(csv.reader(destination)),
            [["actions", "timestamp"], ["7", format_timestamp(120)]],
        )

    def test_writes_to_nested_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "apm.csv"
            with CsvRecorder(path) as recorder:
                recorder.record(1, 60)

            self.assertTrue(path.exists())
            self.assertIn(f"1,{format_timestamp(60)}", path.read_text(encoding="utf-8"))

    def test_does_not_duplicate_header_in_existing_file(self):
        destination = io.StringIO("actions,timestamp\n")
        with CsvRecorder(Path("unused.csv"), destination) as recorder:
            recorder.record(2, 180)

        destination.seek(0)
        self.assertEqual(
            list(csv.reader(destination)),
            [["actions", "timestamp"], ["2", format_timestamp(180)]],
        )


class TestAppFocus(unittest.TestCase):
    def test_writes_focus_sessions_with_iso_timestamps(self):
        output = io.StringIO()
        with AppFocusRecorder(Path("unused.csv"), output) as recorder:
            recorder.record("Editor", 0, 2.5)

        output.seek(0)
        self.assertEqual(
            list(csv.reader(output)),
            [
                [
                    "app_name",
                    "started_at",
                    "stopped_at",
                    "duration_seconds",
                ],
                [
                    "Editor",
                    format_timestamp(0),
                    format_timestamp(2.5),
                    "2.500",
                ],
            ],
        )

    def test_does_not_write_loginwindow_sessions(self):
        output = io.StringIO()
        with AppFocusRecorder(Path("unused.csv"), output) as recorder:
            recorder.record("loginwindow", 0, 10)

        output.seek(0)
        self.assertEqual(
            list(csv.reader(output)),
            [["app_name", "started_at", "stopped_at", "duration_seconds"]],
        )

    def test_loginwindow_ends_sessions_but_is_not_started_or_resumed(self):
        output = io.StringIO()
        with AppFocusRecorder(Path("unused.csv"), output) as recorder:
            frontmost_app = ["loginwindow"]
            current_time = [0]
            tracker = AppFocusTracker(
                recorder,
                provider=lambda: frontmost_app[0],
                clock=lambda: current_time[0],
                poll_interval=60,
            )
            tracker.start()
            tracker.observe("Terminal", 10)
            tracker.observe("loginwindow", 20)
            current_time[0] = 100
            tracker.resume(100)
            tracker.observe("Calculator", 105)
            current_time[0] = 110
            tracker.stop()

        output.seek(0)
        self.assertEqual(
            list(csv.reader(output)),
            [
                ["app_name", "started_at", "stopped_at", "duration_seconds"],
                ["Terminal", format_timestamp(10), format_timestamp(20), "10.000"],
                ["Calculator", format_timestamp(105), format_timestamp(110), "5.000"],
            ],
        )

    def test_closes_previous_and_active_focus_sessions(self):
        output = io.StringIO()
        with AppFocusRecorder(Path("unused.csv"), output) as recorder:
            current_time = [0]
            tracker = AppFocusTracker(
                recorder,
                provider=lambda: "Terminal",
                clock=lambda: current_time[0],
                poll_interval=60,
            )
            tracker.start()
            tracker.observe("Editor", 5)
            current_time[0] = 10
            tracker.stop()

        output.seek(0)
        rows = list(csv.reader(output))
        self.assertEqual(
            rows[1],
            ["Terminal", format_timestamp(0), format_timestamp(5), "5.000"],
        )
        self.assertEqual(
            rows[2],
            ["Editor", format_timestamp(5), format_timestamp(10), "5.000"],
        )

    def test_sleep_time_is_excluded_from_focus_duration(self):
        output = io.StringIO()
        with AppFocusRecorder(Path("unused.csv"), output) as recorder:
            current_time = [0]
            tracker = AppFocusTracker(
                recorder,
                provider=lambda: "Terminal",
                clock=lambda: current_time[0],
                poll_interval=60,
            )
            tracker.start()
            tracker.pause(5)
            current_time[0] = 105
            tracker.resume(105)
            current_time[0] = 110
            tracker.stop()

        output.seek(0)
        rows = list(csv.reader(output))
        self.assertEqual(
            rows[1],
            ["Terminal", format_timestamp(0), format_timestamp(5), "5.000"],
        )
        self.assertEqual(
            rows[2],
            ["Terminal", format_timestamp(105), format_timestamp(110), "5.000"],
        )


class TestMinuteTracker(unittest.TestCase):
    def test_boundary_records_previous_minute(self):
        counter = ActionCounter(clock=lambda: 61)
        counter.record()
        output = io.StringIO()
        with CsvRecorder(Path("unused.csv"), output) as recorder:
            tracker = MinuteTracker(recorder, counter=counter, clock=lambda: 120)
            tracker.record_boundary(120)

        output.seek(0)
        self.assertEqual(
            list(csv.reader(output))[-1], ["1", format_timestamp(120)]
        )

    def test_sleep_records_transition_minute_and_skips_slept_minutes(self):
        counter = ActionCounter(clock=lambda: 61)
        counter.record()
        counter.record()
        output = io.StringIO()
        with CsvRecorder(Path("unused.csv"), output) as recorder:
            tracker = MinuteTracker(recorder, counter=counter, clock=lambda: 61)
            next_boundary = tracker.record_due_boundaries(120, 600, system_slept=True)

        output.seek(0)
        self.assertEqual(list(csv.reader(output)), [
            ["actions", "timestamp"],
            ["2", format_timestamp(120)],
        ])
        self.assertEqual(next_boundary, 660)

    def test_sleep_uses_explicit_transition_boundary(self):
        counter = ActionCounter(clock=lambda: 121)
        counter.record()
        output = io.StringIO()
        with CsvRecorder(Path("unused.csv"), output) as recorder:
            tracker = MinuteTracker(recorder, counter=counter, clock=lambda: 121)
            next_boundary = tracker.record_due_boundaries(
                180,
                600,
                system_slept=True,
                sleep_transition_boundary=180,
            )

        output.seek(0)
        self.assertEqual(list(csv.reader(output)), [
            ["actions", "timestamp"],
            ["1", format_timestamp(180)],
        ])
        self.assertEqual(next_boundary, 660)

    def test_sleep_before_boundary_keeps_that_boundary_pending(self):
        counter = ActionCounter(clock=lambda: 121)
        counter.record()
        output = io.StringIO()
        with CsvRecorder(Path("unused.csv"), output) as recorder:
            tracker = MinuteTracker(recorder, counter=counter, clock=lambda: 121)
            next_boundary = tracker.record_due_boundaries(
                180,
                150,
                system_slept=True,
            )

        output.seek(0)
        self.assertEqual(list(csv.reader(output)), [["actions", "timestamp"]])
        self.assertEqual(next_boundary, 180)

    def test_run_detects_sleep_and_records_transition_bucket(self):
        class OneWakeEvent:
            def __init__(self):
                self.waits = 0

            def is_set(self):
                return self.waits > 1

            def wait(self, _timeout):
                self.waits += 1
                return self.waits > 1

        wall_times = iter((61, 61, 600, 600))
        uptime_times = iter((0, 1))
        counter = ActionCounter(clock=lambda: 61)
        counter.record()
        output = io.StringIO()
        with CsvRecorder(Path("unused.csv"), output) as recorder:
            tracker = MinuteTracker(
                recorder,
                counter=counter,
                clock=lambda: next(wall_times),
                monotonic_clock=lambda: next(uptime_times),
            )
            tracker.run(OneWakeEvent())

        output.seek(0)
        self.assertEqual(list(csv.reader(output)), [
            ["actions", "timestamp"],
            ["1", format_timestamp(120)],
        ])


class TestInput(unittest.TestCase):
    def test_counts_non_empty_lines_only(self):
        counter = ActionCounter(clock=lambda: 61)
        consume_actions(io.StringIO("click\n\n  \nkeypress\n"), counter)

        self.assertEqual(counter.take(60), 2)

    def test_macos_event_mask_includes_each_action_type(self):
        from apm_tracker import MacOSEventSource

        mask = MacOSEventSource.event_mask()
        for event_type in MACOS_ACTION_EVENT_TYPES:
            self.assertTrue(mask & (1 << event_type))

    def test_auto_source_uses_stdin_for_non_interactive_input(self):
        class NonInteractiveInput(io.StringIO):
            def isatty(self):
                return False

        self.assertEqual(selected_source("auto", NonInteractiveInput()), "stdin")


if __name__ == "__main__":
    unittest.main()
