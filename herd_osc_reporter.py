#!/usr/bin/env python3
"""herd-osc-reporter: make any coding agent visible in the Herd desktop app.

The Herd desktop app marks a terminal pane as an agent when the program running
in it emits an OSC 7501 sequence on its pty:

    ESC ] 7501 ; state=<idle|working|blocked|done|error> : app=<name> BEL

Herd's own agent catalog (closed-source herdr-core) does not accept state
reports from third parties, and many agents never emit OSC 7501, so their
panes never appear in the desktop app's Agents panel (herdrdev/herdr #5673).

This daemon watches every herdr-core terminal whose foreground process matches
a configured agent, classifies the pane state by matching the agent's own TUI
screen strings (via `hc read`), and writes the OSC 7501 sequence into the
pane's tty through /proc/<fgpid>/fd/1 — the slave side of the pty, which
herdr-core reads as normal pane output. The sequence is consumed by herdr-core
and never reaches the screen.

It also supports optional per-agent "session titles": if the agent's sessions
live in a SQLite table and the chat/session id can be correlated from a log
file, the pane subtitle shows the current conversation title (via OSC 0).

Tested with Hermes Agent on Debian LXC + Herd desktop (Windows) over SSH.
License: MIT. Author: Lucas M. Sastre (oniricosistemas), with Hermes Agent.
"""

import calendar
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

CONFIG_PATH = os.environ.get(
    "HERD_OSC_REPORTER_CONFIG",
    os.path.expanduser("~/.config/herd-osc-reporter/config.json"),
)
STATE_PATH = os.path.expanduser("~/.local/state/herd-osc-reporter/state.json")


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for name, agent in cfg.get("agents", {}).items():
        agent["name"] = name
        agent["_working"] = [re.compile(p, re.I) for p in agent.get("working", [])]
        agent["_blocked"] = [re.compile(p, re.I) for p in agent.get("blocked", [])]
        agent["_idle"] = [re.compile(p, re.I) for p in agent.get("idle", [])]
        agent["_match"] = agent.get("match", [name])
    return cfg


def log(msg: str) -> None:
    path = os.path.expanduser(cfg.get("log_path", "~/.local/state/herd-osc-reporter/log"))
    try:
        if os.path.exists(path) and os.path.getsize(path) > cfg.get("max_log_bytes", 512_000):
            os.replace(path, path + ".1")
        with open(path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def hc_json(*args: str):
    try:
        proc = subprocess.run([cfg["hc_bin"], *args], capture_output=True, text=True,
                              timeout=cfg.get("hc_timeout", 15))
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def classify(agent, screen: str):
    """Match the agent's TUI status strings, restricted to the bottom of the
    screen (status bar and dialogs live there; transcript text higher up may
    contain trigger words and cause false states).

    Put distinctive dialog markers in `blocked` (e.g. the literal title of your
    agent's approval dialog): they are checked FIRST so a working clock in the
    status bar cannot mask a real approval prompt. Generic words like "approve"
    belong nowhere: they appear in transcripts and cause false states."""
    lines = [l for l in screen.splitlines() if l.strip()]
    bottom = "\n".join(lines[-12:])
    for rx in agent["_blocked"]:
        if rx.search(bottom):
            return "blocked"
    for rx in agent["_working"]:
        if rx.search(bottom):
            return "working"
    for rx in agent["_idle"]:
        if rx.search(bottom):
            return "idle"
    return None


def emit_osc(pid: int, state: str | None, title: str | None = None, app: str = "hermes") -> bool:
    try:
        payload = ""
        if state is not None:
            payload += f"\x1b]7501;state={state}:app={app}\x07"
        if title is not None:
            payload += f"\x1b]0;{title}\x07"
        if not payload:
            return False
        with open(f"/proc/{pid}/fd/1", "w") as tty:
            tty.write(payload)
            tty.flush()
        return True
    except OSError as exc:
        log(f"emit -> pid {pid} failed: {exc.__class__.__name__}: {exc}")
        return False


class SessionTitles:
    """Optional: conversation titles from a SQLite DB, correlated via a log."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self._cache: dict = {}
        self._last_read = 0.0
        self._offset = 0
        self._inode = None
        self._ts_re = re.compile(agent["session_title"]["ts_regex"]) if agent.get("session_title") else None
        self._id_re = re.compile(agent["session_title"]["id_regex"]) if agent.get("session_title") else None

    def poll_chat_events(self) -> list:
        st_cfg = self.agent.get("session_title")
        if not st_cfg:
            return []
        log_path = os.path.expanduser(st_cfg["log"])
        events = []
        try:
            st = os.stat(log_path)
        except OSError:
            return events
        if self._inode is not None and st.st_ino != self._inode:
            self._offset = 0
        if st.st_size < self._offset:
            self._offset = 0
        self._inode = st._inode if hasattr(st, "_inode") else st.st_ino
        if st.st_size == self._offset:
            return events
        try:
            with open(log_path, errors="replace") as f:
                f.seek(self._offset)
                chunk = f.read()
            self._offset += len(chunk.encode("utf-8", "replace"))
        except OSError:
            return events
        for line in chunk.splitlines():
            ts = self._ts_re.search(line)
            cid = self._id_re.search(line)
            if not (ts and cid):
                continue
            fmt = st_cfg.get("ts_format", "%Y-%m-%d %H:%M:%S")
            try:
                epoch = (calendar.timegm if st_cfg.get("ts_utc", True) else time.mktime)(
                    time.strptime(ts.group(1), fmt))
            except ValueError:
                continue
            events.append((epoch, cid.group(1)))
        return events

    def get(self, chat_id: str) -> str | None:
        st_cfg = self.agent.get("session_title")
        if not (st_cfg and chat_id):
            return None
        now = time.time()
        hit = self._cache.get(chat_id)
        if hit is not None and now - self._last_read < st_cfg.get("refresh_seconds", 15):
            return hit
        self._last_read = now
        title = None
        try:
            db = sqlite3.connect(f"file:{os.path.expanduser(st_cfg['db'])}?mode=ro", uri=True, timeout=5)
            try:
                row = db.execute(st_cfg["query"], (chat_id,)).fetchone()
            finally:
                db.close()
            title = row[0] if row and row[0] else None
        except sqlite3.Error as exc:
            log(f"title query failed: {exc}")
        self._cache[chat_id] = title
        return title


def main() -> int:
    global cfg
    cfg = load_config()
    poll_s = cfg.get("poll_seconds", 2.0)
    keepalive_s = cfg.get("keepalive_seconds", 30.0)
    log("daemon start")
    state = {}
    titles = {n: SessionTitles(a) for n, a in cfg.get("agents", {}).items()}
    pending: dict = {n: [] for n in titles}   # agent -> [(epoch, chat_id)]
    claimed: set = set()

    while True:
        cycle_start = time.time()
        try:
            for name, agent in cfg.get("agents", {}).items():
                if name in pending:
                    pending[name].extend(titles[name].poll_chat_events())

            listing = hc_json("ls") or {}
            seen = set()
            for term in listing.get("terminals") or []:
                address = term.get("address")
                if not address:
                    continue
                detail = hc_json("get", address) or {}
                fg = detail.get("foreground") or {}
                fg_name = fg.get("name") or ""
                argv = " ".join(str(a) for a in (fg.get("argv") or []))
                key = None
                for n, agent in cfg.get("agents", {}).items():
                    if any(m and (m == fg_name or m in argv) for m in agent["_match"]):
                        key = n
                        break
                if key is None:
                    continue
                agent = cfg["agents"][key]
                seen.add(address)
                fg_pid = fg.get("pid")

                screen = (hc_json("read", address) or {}).get("text") or ""
                detected = classify(agent, screen)

                prev = state.get(address, {})
                reported = prev.get("reported")
                last_emit = prev.get("last_emit", 0.0)
                last_title = prev.get("title")
                chat_id = prev.get("chat_id")
                awaiting_since = prev.get("awaiting_since")
                now = time.time()

                if detected == "working" and reported in (None, "idle") and agent.get("session_title"):
                    if awaiting_since is None:
                        awaiting_since = now

                title = None
                if key in titles and awaiting_since is not None:
                    # Bind the closest unclaimed chat event (screen shows working
                    # ~2s before the gateway logs the inbound message).
                    cands = [e for e in pending.get(key, [])
                             if awaiting_since - 3 <= e[0] <= now and e[1] not in claimed]
                    if cands:
                        pick = min(cands, key=lambda e: abs(e[0] - awaiting_since))
                        claimed.add(pick[1])
                        pending[key].remove(pick)
                        if chat_id != pick[1]:
                            chat_id = pick[1]
                            last_title = None  # force title re-emit
                            log(f"{address} bound to {pick[1]}")
                        awaiting_since = None
                    elif now - awaiting_since > 15:
                        awaiting_since = None  # give up; fallback title
                if key in titles and chat_id:
                    title = titles[key].get(chat_id)
                if not title:
                    cwd = (detail.get("cwd") or "").rstrip("/")
                    base = os.path.basename(cwd) if cwd else ""
                    title = base if base and cwd != os.path.expanduser("~") else os.uname().nodename
                if title and title != last_title and fg_pid:
                    if emit_osc(fg_pid, None, title=title, app=key):
                        log(f"{address} pid={fg_pid} -> title '{title}'")
                        last_title = title

                if reported is None:
                    desired = detected or "idle"
                elif detected is None:
                    desired = reported
                else:
                    desired = detected

                changed = desired != reported
                stale = (now - last_emit) > keepalive_s
                if (changed or stale) and fg_pid:
                    if emit_osc(fg_pid, desired, app=key):
                        if changed:
                            log(f"{address} pid={fg_pid} -> {desired}")
                        reported = desired
                        last_emit = now
                state[address] = {"agent": key, "reported": reported, "last_emit": last_emit,
                                  "title": last_title, "chat_id": chat_id,
                                  "awaiting_since": awaiting_since}

            for gone in set(state) - seen:
                log(f"{gone} disappeared; clearing state")
                del state[gone]
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w") as f:
                json.dump(state, f)
        except Exception as exc:
            log(f"cycle error: {exc.__class__.__name__}: {exc}")
        time.sleep(max(poll_s - (time.time() - cycle_start), 0.2))


if __name__ == "__main__":
    sys.exit(main())
