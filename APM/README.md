# Actions per minute tracker

This project records actions in a two-column CSV file, one row per completed
minute. macOS runs also track frontmost-application sessions in
`app_focus.csv` and system Now Playing sessions in `now_playing.csv`. On macOS,
an interactive run captures global keyboard and mouse actions directly; it no
longer depends on the terminal producing one input line per action.

## Input contract

The default `--source auto` mode uses the macOS event tap when the program is
started from an interactive terminal. It counts keyboard key-downs, mouse
button presses, and scroll-wheel events. Mouse movement is not counted because
it is a continuous stream rather than a discrete action.

For scripts and other event producers, use `--source stdin`; each non-empty
input line represents one action and blank lines are ignored:

```sh
printf 'click\nkeypress\nscroll\n' | python3 apm_tracker.py --source stdin
```

The macOS event tap requires Accessibility/Input Monitoring permission for the
terminal or Python process running the tracker. If permission is missing, the
program exits with an explanatory error instead of silently recording zeros.

## Run

```sh
python3 apm_tracker.py --output apm.csv
```

Use `--source macos` to require the macOS event tap, or `--source stdin` to
require line-oriented input regardless of whether stdin is interactive. Use
`--app-output PATH` and `--media-output PATH` to change the focus and media CSV
destinations.

The CSV contains a header followed by rows in this form:

```csv
actions,timestamp
3,2026-08-07T12:00:00-07:00
```

The timestamp is ISO-8601 with the machine's local UTC offset. The row contains
the number of actions received during the preceding 60-second minute. Rows are
flushed as soon as they are written, and missed minute boundaries are
backfilled with zero-action rows if the process is temporarily delayed.

If the Mac goes to sleep, the tracker detects the sleep gap using the wall
clock and monotonic uptime clock. It writes the minute containing the sleep
transition, skips minutes spent entirely asleep instead of writing misleading
zero rows, and resumes normal minute recording after wake. The transition
minute's row uses the timestamp of the following minute boundary because that
is when the completed row is recorded. Normal scheduling still waits in
one-minute intervals; a macOS power notification interrupts that wait after
wake so the transition row is written promptly.

## Application focus

On macOS, the tracker starts a session for the current frontmost application,
closes it when another application becomes frontmost, and closes the active
session when the tracker stops. The focus CSV has this form:

```csv
app_name,started_at,stopped_at,duration_seconds
Terminal,2026-08-07T12:00:00-07:00,2026-08-07T12:03:12-07:00,192.000
```

Focus timestamps use the same local ISO-8601 format and UTC offset as the
actions CSV. The frontmost process is sampled every 0.5 seconds using macOS
Process Manager APIs; focus tracking is unavailable on non-macOS systems, but
the separate file is still initialized there for a stable output contract. A
focus session is closed before system sleep and a new session starts after
wake, so sleeping time is excluded from `duration_seconds`. The macOS
`loginwindow` process, which can appear frontmost while the laptop is asleep,
is never written as an application session.

## System media

On macOS, the tracker polls the system Now Playing controller once per second
and writes a row when playback starts, pauses, changes items or sources, or the
tracker stops. The CSV includes the source reported by the system, title,
artist, album, local ISO-8601 start and stop times, and session duration:

```csv
source,title,artist,album,started_at,stopped_at,duration_seconds
Spotify,Example song,Example artist,Example album,2026-08-07T12:00:00-07:00,2026-08-07T12:03:12-07:00,192.000
```

This uses the `media-control` command to read the system media controller. Its
MediaRemote adapter supports current macOS releases where direct access to the
private framework is restricted. Install it with Homebrew before starting the
tracker:

```sh
brew tap ungive/media-control
brew install media-control
```

If the command is installed outside `PATH`, pass its location with
`--media-command PATH`. The source value uses a service name when macOS provides
one, otherwise it uses the reporting app's name or bundle identifier. Browser
playback may therefore be labeled with the browser rather than the website.
On non-macOS systems, the media CSV is initialized with its header but no
sessions are collected.

## Development

The tracker code uses only the Python standard library. macOS Now Playing
collection additionally requires the external `media-control` command. Run the
test suite with:

```sh
python3 -m unittest discover -s tests -v
```
