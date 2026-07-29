#!/usr/bin/env python3
"""
terminator-session-restore — Auto-restore Terminator terminal layout
and Claude/Codex sessions after reboot.

Single-file, no external dependencies (Python 3.10+ only).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# Configuration — edit these to match your setup
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "data" / "session_state.json"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Layout is handed to Terminator as a partial config overlay via
# `terminator --config-json`. Terminator merges it into its in-memory config
# only, under the reserved name __internal_json_layout__, which Config.save()
# explicitly skips. ~/.config/terminator/config is never read or written.
LAYOUT_JSON = SCRIPT_DIR / "data" / "terminator_layout.json"

CLAUDE_BIN = "claude"
EXTRA_ARGS = []
TERMINATOR_BIN = "terminator"

VERBOSE = False
LOG_FILE = Path("/tmp/session_restore_debug.log")


def _log(msg: str) -> None:
    if not VERBOSE:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ============================================================
# Data model
# ============================================================

@dataclass
class TerminalPane:
    order: int
    pts: str
    cwd: str
    session_id: str | None = None
    source: str | None = None


@dataclass
class SessionSnapshot:
    timestamp: str
    window_geometry: str
    panes: list[TerminalPane]


# ============================================================
# Snapshot capture
# ============================================================

def snapshot_state() -> SessionSnapshot | None:
    terminator_pid = _get_terminator_pid()
    if not terminator_pid:
        return None

    sessions_by_pid = {s["pid"]: s for s in _get_active_claude_sessions()}
    codex_by_pid = _map_codex_sessions()
    raw_panes = _get_panes(terminator_pid)

    if not raw_panes:
        return None

    panes: list[TerminalPane] = []
    for i, (pts, cwd, subtree) in enumerate(raw_panes):
        sid = source = None
        for pid in subtree:
            if pid in sessions_by_pid:
                session = sessions_by_pid[pid]
                sid, source = session["sessionId"], "claude"
                # The session file records the cwd Claude itself is using,
                # which beats anything inferred from the process tree.
                cwd = session.get("cwd") or cwd
                break
            if pid in codex_by_pid:
                sid, source = codex_by_pid[pid], "codex"
                break
        panes.append(TerminalPane(
            order=i,
            pts=pts,
            cwd=cwd,
            session_id=sid,
            source=source,
        ))

    geometry = _get_window_geometry(terminator_pid)

    return SessionSnapshot(
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(),
        window_geometry=geometry,
        panes=panes,
    )


def save_snapshot(snapshot: SessionSnapshot, path: Path | None = None) -> None:
    path = path or STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    # Keep previous snapshot as .prev.json so restore has a fallback
    if path.exists() and path.stat().st_size > 2:
        prev = path.with_suffix(".prev.json")
        try:
            import shutil
            shutil.copy2(path, prev)
        except OSError:
            pass

    data = {
        "timestamp": snapshot.timestamp,
        "window_geometry": snapshot.window_geometry,
        "panes": [asdict(p) for p in snapshot.panes],
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_snapshot(path: Path) -> SessionSnapshot | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        panes = [TerminalPane(**p) for p in data["panes"]]
        return SessionSnapshot(
            timestamp=data["timestamp"],
            window_geometry=data["window_geometry"],
            panes=panes,
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ============================================================
# Session restore
# ============================================================

def _count_sessions(panes: list[TerminalPane]) -> int:
    return sum(1 for p in panes if p.session_id)


def _best_snapshot(state_file: Path) -> SessionSnapshot | None:
    """Pick the snapshot with the most sessions from available files."""
    candidates: list[tuple[str, SessionSnapshot]] = []
    for path in [state_file, state_file.with_suffix(".prev.json"), state_file.with_suffix(".restored")]:
        snap = load_snapshot(path)
        if snap and snap.panes:
            _log(f"  candidate {path.name}: {len(snap.panes)} panes, {_count_sessions(snap.panes)} sessions")
            candidates.append((path.name, snap))

    if not candidates:
        return None
    best_name, best = max(candidates, key=lambda c: (_count_sessions(c[1].panes), c[1].timestamp))
    _log(f"  selected: {best_name}")
    return best


def restore_sessions(state_file: Path | None = None) -> bool:
    state_file = state_file or STATE_FILE
    _log(f"restore_sessions: state_file={state_file}")
    snapshot = _best_snapshot(state_file)

    if not snapshot or not snapshot.panes:
        _log("No snapshot data, aborting")
        return False

    _log(f"Restoring {len(snapshot.panes)} tabs, {_count_sessions(snapshot.panes)} sessions")
    _write_layout_json(_build_tab_layout(snapshot))

    geo = snapshot.window_geometry

    try:
        # No --layout: Terminator only applies the injected JSON layout when
        # the layout option is unset or "default" (see /usr/bin/terminator and
        # terminatorlib/ipc.py). The JSON layout has no window size/position,
        # so geometry is restored via the CLI flag instead — when we have it.
        cmd = [TERMINATOR_BIN, "--config-json", str(LAYOUT_JSON)]
        if geo:
            cmd.append(f"--geometry={geo}")
        _log(f"Launching: {cmd}")
        subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError:
        _log("terminator not found")
        return False

    # Clean up all snapshot files after restore
    for suffix in (".restored", ".prev.json"):
        old = state_file.with_suffix(suffix)
        try:
            if old.exists():
                old.unlink()
        except OSError:
            pass
    try:
        state_file.rename(state_file.with_suffix(".restored"))
    except OSError:
        pass

    return True


# ============================================================
# Layout generation — one tab per captured pane
# ============================================================

def _build_tab_layout(snapshot: SessionSnapshot) -> dict[str, list[dict[str, str]]]:
    """Build the partial-config JSON layout: one tab per captured pane.

    Terminator takes each tab's label from the dict key and preserves
    insertion order, so keys must be unique — a duplicate would silently
    collapse two panes into one tab.
    """
    tabs: dict[str, list[dict[str, str]]] = {}
    for pane in sorted(snapshot.panes, key=lambda p: p.order):
        tabs[_unique_label(pane, tabs)] = [{"command": _pane_command(pane)}]
    return tabs


def _unique_label(pane: TerminalPane, taken: dict[str, Any]) -> str:
    base = os.path.basename(pane.cwd.rstrip("/")) or "root"
    label, n = base, 2
    while label in taken:
        label = f"{base}~{n}"
        n += 1
    return label


def _write_layout_json(tabs: dict[str, list[dict[str, str]]]) -> None:
    LAYOUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = LAYOUT_JSON.with_name(LAYOUT_JSON.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"layout": tabs}, f, ensure_ascii=False, indent=2)
    tmp.replace(LAYOUT_JSON)


def _pane_command(pane: TerminalPane) -> str:
    """Build the shell command that restores a single tab.

    The JSON layout format carries no `directory` key (configjson.py only
    emits `command`), so the working directory is restored by cd-ing inside
    the command — which the removable-media wait below needed anyway.
    """
    # ~/.bashrc typically has an interactive-shell guard (case $- in *i*)
    # that prevents nvm from loading in non-interactive `bash -c`.
    # Source nvm directly to ensure node-based CLIs are found.
    nvm_init = 'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"'
    qdir = shlex.quote(pane.cwd)
    # Removable media may not be mounted yet at boot — wait up to 60s
    cd_wait = (
        f'for i in $(seq 1 30); do [ -d {qdir} ] && break; sleep 2; done; '
        f'cd {qdir} 2>/dev/null || echo warning: {qdir} unavailable'
    )

    if pane.session_id and pane.source == "claude":
        resume = " ".join([shlex.quote(CLAUDE_BIN), "--resume",
                           shlex.quote(pane.session_id),
                           *(shlex.quote(a) for a in EXTRA_ARGS)])
    elif pane.session_id and pane.source == "codex":
        resume = f'codex resume {shlex.quote(pane.session_id)}'
    else:
        resume = ""

    # Hand the tab back to the user's own login shell, not hardcoded bash.
    tail = 'exec "${SHELL:-/bin/sh}"'
    if resume:
        cmd = f'{nvm_init}; {cd_wait}; {resume}; {tail}'
    else:
        cmd = f'{cd_wait}; {tail}'
    return f"bash -c {shlex.quote(cmd)}"


_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def _map_codex_sessions() -> dict[int, str]:
    """Map codex pid -> session UUID by scanning codex processes.

    The session UUID is extracted from the open rollout file
    ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
    """
    pid_map: dict[int, str] = {}
    try:
        result = subprocess.run(
            ["pgrep", "-x", "codex"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return pid_map
        pids = [p for p in result.stdout.strip().split("\n") if p]
    except subprocess.TimeoutExpired:
        return pid_map

    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                m = _ROLLOUT_RE.search(target)
                if m and "/.codex/sessions/" in target:
                    pid_map[int(pid)] = m.group(1)
                    break
        except OSError:
            continue
    return pid_map


# ============================================================
# System info helpers (Linux /proc based)
# ============================================================

def _get_terminator_pid() -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "terminator"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        pids = result.stdout.strip().split("\n")
        return int(pids[0]) if pids and pids[0] else None
    except (subprocess.TimeoutExpired, ValueError):
        return None


def _get_active_claude_sessions() -> list[dict[str, Any]]:
    sessions = []
    if not CLAUDE_SESSIONS_DIR.exists():
        return sessions
    for f in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("pid") and data.get("sessionId"):
                sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return sessions


# Any login shell, not just bash — matching on "bash" alone silently finds
# nothing on a zsh/fish system.
SHELL_NAMES = frozenset({"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh"})
# Agents spawn their own throwaway shells for tool calls; those sit deeper in
# the tree with unrelated cwds, so pane discovery must not descend into them.
AGENT_NAMES = frozenset({"claude", "codex"})


def _proc_table() -> dict[int, tuple[int, str]]:
    """Map pid -> (ppid, comm) by reading /proc, replacing the pstree dependency."""
    table: dict[int, tuple[int, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        # Field 2 is the comm, wrapped in parentheses; it may itself contain
        # spaces or ')', so anchor on the *last* ')' rather than splitting.
        try:
            close = stat.rindex(")")
            comm = stat[stat.index("(") + 1:close]
            ppid = int(stat[close + 2:].split()[1])
        except (ValueError, IndexError):
            continue
        table[int(entry)] = (ppid, comm)
    return table


def _children_map(table: dict[int, tuple[int, str]]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    return kids


def _subtree(root: int, kids: dict[int, list[int]]) -> list[int]:
    out: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(kids.get(pid, []))
    return out


def _readlink(pid: int, name: str) -> str:
    try:
        return os.readlink(f"/proc/{pid}/{name}")
    except OSError:
        return ""


def _deepest_shell_cwd(root: int, kids: dict[int, list[int]],
                       table: dict[int, tuple[int, str]]) -> str:
    """cwd of the deepest shell under root, skipping agent-spawned subshells.

    Wrappers such as zsh-smart-suggestions run the real interactive shell one
    level below Terminator's direct child, leaving the outer shell parked in
    $HOME — so the deepest shell holds the cwd the user actually sees.
    """
    best_depth, best_cwd = -1, ""
    stack = [(root, 0)]
    while stack:
        pid, depth = stack.pop()
        comm = table.get(pid, (0, ""))[1]
        if comm in AGENT_NAMES and pid != root:
            continue  # don't descend into claude/codex tool shells
        if comm in SHELL_NAMES and depth > best_depth:
            cwd = _readlink(pid, "cwd")
            if cwd:
                best_depth, best_cwd = depth, cwd
        for kid in kids.get(pid, []):
            stack.append((kid, depth + 1))
    return best_cwd


def _get_panes(terminator_pid: int) -> list[tuple[str, str, list[int]]]:
    """One pane per direct child of Terminator, which spawns one shell each.

    Returns (pts, cwd, subtree_pids). Counting direct children avoids the
    phantom panes that pts-matching produces when a wrapper allocates its own
    pty in addition to Terminator's.
    """
    table = _proc_table()
    kids = _children_map(table)

    panes: list[tuple[str, str, list[int]]] = []
    for child in sorted(kids.get(terminator_pid, [])):
        cwd = _deepest_shell_cwd(child, kids, table) or _readlink(child, "cwd")
        if not cwd:
            continue
        panes.append((_readlink(child, "fd/0"), cwd, _subtree(child, kids)))
    return panes


def _get_window_geometry(terminator_pid: int) -> str:
    """Best-effort window geometry, or "" when it can't be determined.

    Returns "" rather than a hardcoded guess: under Wayland, Terminator is a
    native Wayland client with no X11 window, so xdotool/xwininfo cannot see
    it at all. Restoring at an invented size is worse than letting Terminator
    use its own default.
    """
    try:
        result = subprocess.run(
            ["xdotool", "search", "--pid", str(terminator_pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            wid = result.stdout.strip().split("\n")[0]
            return _geometry_from_xwininfo(wid)
        _log("  geometry unavailable (no X11 window; Wayland session?)")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        _log("  geometry unavailable (xdotool missing or timed out)")
    return ""


def _geometry_from_xwininfo(wid: str) -> str:
    try:
        result = subprocess.run(
            ["xwininfo", "-id", wid],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return ""

        width = height = x = y = 0
        for line in result.stdout.splitlines():
            if "Absolute upper-left X:" in line:
                x = int(line.split(":")[-1].strip())
            elif "Absolute upper-left Y:" in line:
                y = int(line.split(":")[-1].strip())
            elif "Width:" in line:
                width = int(line.split(":")[-1].strip())
            elif "Height:" in line:
                height = int(line.split(":")[-1].strip())

        if width and height:
            return f"{width}x{height}+{x}+{y}"
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return ""


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Terminator + Claude/Codex session restore tool"
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging to stderr and /tmp/session_restore_debug.log")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("snapshot", help="Save current terminal state snapshot")
    sub.add_parser("restore", help="Restore terminal sessions from snapshot")
    sub.add_parser("status", help="Show current snapshot status")
    p_install = sub.add_parser("install", help="Install autostart on boot")
    p_install.add_argument("--uninstall", action="store_true", help="Remove autostart")

    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    if args.cmd == "snapshot":
        state = snapshot_state()
        if state and state.panes:
            save_snapshot(state)
            print(f"Saved snapshot: {len(state.panes)} panes")
            for p in state.panes:
                tag = f" [{p.source}:{p.session_id[:8]}]" if p.session_id else ""
                print(f"  [{p.order}] {p.cwd}{tag}")
        else:
            print("No active terminator terminal detected")

    elif args.cmd == "restore":
        ok = restore_sessions()
        if ok:
            print("Restore initiated")
        else:
            print("Restore failed (no snapshot data)")

    elif args.cmd == "status":
        for name, path in [("Current", STATE_FILE), ("Backup", STATE_FILE.with_suffix(".restored"))]:
            snap = load_snapshot(path)
            if snap:
                sessions = _count_sessions(snap.panes)
                print(f"{name} ({path.name}): {len(snap.panes)} panes, {sessions} sessions, {snap.timestamp}")
                for p in snap.panes:
                    tag = f" [{p.source}:{p.session_id[:8]}]" if p.session_id else " [empty]"
                    print(f"  [{p.order}] {p.cwd}{tag}")
            else:
                print(f"{name}: none")
            print()

    elif args.cmd == "install":
        _install_autostart(uninstall=args.uninstall)

    else:
        parser.print_help()


def _install_autostart(uninstall: bool = False):
    autostart_dir = Path.home() / ".config" / "autostart"
    desktop_file = autostart_dir / "terminator-session-restore.desktop"

    if uninstall:
        if desktop_file.exists():
            desktop_file.unlink()
            print(f"Removed autostart: {desktop_file}")
        else:
            print("Autostart not installed")
        return

    autostart_dir.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    content = f"""[Desktop Entry]
Type=Application
Name=TerminatorSessionRestore
Comment=Restore Terminator terminal layout and Claude/Codex sessions on boot
Exec=/usr/bin/python3 {script_path} restore
Terminal=false
Categories=Development;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
"""
    desktop_file.write_text(content)
    print(f"Installed autostart: {desktop_file}")
    print(f"  Script: {script_path}")


if __name__ == "__main__":
    main()
