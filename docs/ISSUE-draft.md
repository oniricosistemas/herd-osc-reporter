# Desktop app: agents that don't emit OSC 7501 never appear in the Agents panel — user-space workaround

## Summary

As documented in #5673, the Herd desktop app's Agents panel only lists panes whose program reports state via **OSC 7501** (`ESC ] 7501 ; state=<state> : app=<name> BEL`). herdr-core 0.1.x has no agent-report API (its OpenRPC schema exposes only `terminal.*` methods), so agents that don't emit OSC 7501 — or that are started through wrappers such as Python launchers — never appear, even though the terminal app detects them fine.

## What we found (Hermes Agent on Debian LXC + Windows desktop over SSH)

- herdr-core parses OSC 7501 with the **BEL terminator only** (`ESC ] ... ESC \` is ignored).
- Extra keys (`title=`, `label=`, `task=`, `msg=`) in the payload are silently ignored; only `state` and `app` are kept.
- The pane process's environment exposes `HC_TERMINAL_ID` / `HC_TERMINAL_SOCKET`, but there is no API to report state back through it.
- **Any local process can report on behalf of a pane**: writing an OSC 7501 sequence to `/proc/<fgpid>/fd/1` (the pty slave side) reaches herdr-core as pane output, and `hc get` immediately shows `"reported": true, "status": {"app": "...", "state": "..."}`. Reports persist (no TTL observed), and the app shows the built-in icon for known `app=` names.
- The Hermes CLI runs its turns in a shared gateway process, so in-pane plugin hooks never fire — screen classification is the only viable state source.

## Workaround shipped

We wrote a small user-space daemon (`herd-osc-reporter`, MIT) that:

1. polls `hc ls`/`hc get` for panes matching a configured agent,
2. classifies the pane state by screen-matching the agent's own TUI strings (what your TUI manifests do),
3. writes OSC 7501 to the pane tty via `/proc/<fgpid>/fd/1`,
4. optionally pulls the conversation title from the agent's SQLite DB after correlating panes to sessions via the backend log.

It makes Hermes panes appear in the Agents panel with live state and titles, no patching of herdr-core or the desktop app.

## Suggestions

1. Document the OSC 7501 grammar (states, kinds, extra keys, terminator rules) — it is currently only discoverable from binary strings.
2. Consider accepting enriched reports (`title`/`task`) or a `pane.report_agent` equivalent in herdr-core, so integrations like this one can use the socket instead of tty writes.
3. Consider screen-manifest fallback in herdr-core (the terminal app already ships manifests per agent).
