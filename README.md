# herd-osc-reporter

Make any coding agent visible in the [Herd](https://herdr.dev) desktop app's
**Agents** panel — with live `idle` / `working` / `blocked` state, notifications,
and conversation titles — even when neither the agent nor herdr-core supports it.

## The problem

The Herd desktop app drives a closed-source backend (`herdr-core`) whose API has
no agent-report method, and it only marks a pane as an agent when the program
emits the **OSC 7501** sequence (`ESC ] 7501 ; state=<state> : app=<name> BEL`).
Agents that don't emit it (and agents started in unusual ways, e.g. via a Python
launcher) never show up — see [herdrdev/herdr#5673](https://github.com/herdrdev/herdr/issues/5673).

## How this works

1. **Watch**: poll `herdr-core` (`hc ls` / `hc get`) for panes whose foreground
   process matches a configured agent.
2. **Classify**: read the pane screen (`hc read`) and match the agent's own TUI
   status strings (e.g. for Hermes: `Working…` -> working, `⏲` -> idle).
3. **Report**: write `OSC 7501` into the pane's tty through
   `/proc/<fgpid>/fd/1` — the pty slave side, which herdr-core reads as normal
   pane output. The sequence is consumed by herdr-core and never reaches the screen.
4. **Title (optional)**: correlate panes to conversation sessions via the agent's
   log file (the backend logs inbound messages at the same instant the screen
   switches to working) and pull the auto-generated title from the agent's
   SQLite state DB. Shown in the panel subtitle via OSC 0.

## Requirements

- Linux host reached by the Herd desktop app (LXC/container fine, same user as the panes).
- `herdr-core` CLI (`hc`) on PATH.
- Python 3.10+ stdlib only.

## Install

```bash
mkdir -p ~/.config/herd-osc-reporter
cp config.example.json ~/.config/herd-osc-reporter/config.json
# edit agents/patterns to match your agent's TUI
python3 herd_osc_reporter.py   # foreground test
```

Then install as a service — either cron:

```cron
@reboot python3 /path/to/herd_osc_reporter >> ~/.local/state/herd-osc-reporter/log 2>&1
*/2 * * * * pgrep -f herd_osc_reporter >/dev/null || python3 /path/to/herd_osc_reporter >> ~/.local/state/herd-osc-reporter/log 2>&1
```

or a systemd user unit (`Restart=always`).

## Configuration

See `config.example.json`. Per agent:

- `match`: process names/argv substrings that identify the agent's foreground process.
- `working` / `blocked` / `idle`: regexes matched against the pane's live screen.
- `session_title` (optional): correlate panes to sessions and read titles from
  a SQLite DB (regexes for timestamp and chat id in a log file, plus a query).

## Tested with

- **Hermes Agent** (Nous Research) on Debian 13 LXC, Herd desktop on Windows over SSH.
  The bundled `config.example.json` contains working patterns for it.

## Notes and caveats

- State detection is screen-matching, exactly what Herdr's own TUI manifests do.
  If your agent's UI changes, update the patterns.
- OSC 7501 also accepts `done` and `error` states, and blocked dialogs can carry
  a `kind` (permission/question/auth) — this reporter currently maps to
  working/idle/blocked only.
- The `blocked` patterns are best-effort; check your agent's approval dialog text.
- Does not require patching herdr-core or the desktop app.

## Why not upstream?

The Herd desktop stack (`herdr-core` / Herd.app) is closed source; its public
repo ships the terminal (TUI) application only, whose own detection cannot help
app panes ([#5673](https://github.com/herdrdev/herdr/issues/5673)). This reporter
is a user-space bridge while first-class support lands.

## License

MIT
