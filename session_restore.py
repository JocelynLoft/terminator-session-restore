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
TERMINATOR_CONFIG = Path.home() / ".config" / "terminator" / "config"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
LAYOUT_NAME = "recovery"

CLAUDE_BIN = "claude"
EXTRA_ARGS = ["--dangerously-skip-permissions"]
TERMINATOR_BIN = "terminator"
GRID_LAYOUT = "2x3"  # "2x3", "3x2", or "vertical"

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

    claude_sessions = _get_active_claude_sessions()
    pts_to_session = _map_sessions_to_pts(claude_sessions)
    bash_panes = _get_bash_panes(terminator_pid)

    if not bash_panes:
        return None

    panes: list[TerminalPane] = []
    for i, (pts, cwd) in enumerate(sorted(bash_panes, key=lambda x: x[0])):
        session_info = pts_to_session.get(pts)
        panes.append(TerminalPane(
            order=i,
            pts=pts,
            cwd=cwd,
            session_id=session_info["sessionId"] if session_info else None,
            source="claude" if session_info else None,
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


def restore_sessions(state_file: Path | None = None) -> bool:
    state_file = state_file or STATE_FILE
    _log(f"restore_sessions: state_file={state_file}")
    snapshot = load_snapshot(state_file)

    # Only use .restored as fallback when primary snapshot is missing/empty
    if not snapshot or not snapshot.panes:
        restored_file = state_file.with_suffix(".restored")
        if restored_file.exists():
            old = load_snapshot(restored_file)
            if old and old.panes:
                _log(f"Primary missing, using .restored ({len(old.panes)} panes, {_count_sessions(old.panes)} sessions)")
                snapshot = old

    if not snapshot or not snapshot.panes:
        _log("No snapshot data, aborting")
        return False

    _log(f"Restoring {len(snapshot.panes)} panes, {_count_sessions(snapshot.panes)} sessions")
    layout = _generate_layout(snapshot)
    _write_terminator_layout(layout)

    geo = snapshot.window_geometry
    already_running = subprocess.run(
        ["pgrep", "-x", "terminator"], capture_output=True
    ).returncode == 0

    try:
        if already_running:
            cmd = [TERMINATOR_BIN, "--layout", LAYOUT_NAME]
        else:
            cmd = [TERMINATOR_BIN, "--layout", LAYOUT_NAME, f"--geometry={geo}"]
        _log(f"Launching: {cmd}")
        subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError:
        _log("terminator not found")
        return False

    try:
        state_file.rename(restored_file)
    except OSError:
        pass

    return True


# ============================================================
# Layout generation
# ============================================================

def _generate_layout(snapshot: SessionSnapshot) -> dict[str, dict[str, str]]:
    panes = snapshot.panes
    n = len(panes)
    layout: dict[str, dict[str, str]] = {}

    geo = snapshot.window_geometry
    width, rest = geo.split("x", 1)
    height, pos = rest.split("+", 1)
    pos_x, pos_y = pos.split("+", 1)

    layout["window0"] = {
        "type": "Window",
        "parent": "",
        "size": f"{width}, {height}",
        "position": f"{pos_x}:{pos_y}",
    }

    if n == 1:
        layout["terminal0"] = _make_terminal(panes[0], "window0", 0)
        return layout

    if n == 6 and GRID_LAYOUT == "2x3":
        return _generate_grid_2x3(layout, panes)
    elif n == 6 and GRID_LAYOUT == "3x2":
        return _generate_grid_3x2(layout, panes)

    # Fallback: vertical stack
    parent = "window0"
    for i in range(n - 1):
        pane_name = f"child{i + 1}"
        ratio = round(1.0 / (n - i), 4)
        layout[pane_name] = {
            "type": "VPaned",
            "parent": parent,
            "ratio": str(ratio),
        }
        layout[f"terminal{i}"] = _make_terminal(panes[i], pane_name, 0)
        if i == n - 2:
            layout[f"terminal{i + 1}"] = _make_terminal(panes[i + 1], pane_name, 1)
        else:
            parent = pane_name

    return layout


def _generate_grid_2x3(layout: dict, panes: list[TerminalPane]) -> dict:
    layout["child_v"] = {"type": "VPaned", "parent": "window0", "ratio": "0.5"}

    layout["child_h_top"] = {"type": "HPaned", "parent": "child_v", "order": "0", "ratio": "0.3333"}
    layout["terminal0"] = _make_terminal(panes[0], "child_h_top", 0)
    layout["child_h_top_r"] = {"type": "HPaned", "parent": "child_h_top", "order": "1", "ratio": "0.5"}
    layout["terminal1"] = _make_terminal(panes[1], "child_h_top_r", 0)
    layout["terminal2"] = _make_terminal(panes[2], "child_h_top_r", 1)

    layout["child_h_bot"] = {"type": "HPaned", "parent": "child_v", "order": "1", "ratio": "0.3333"}
    layout["terminal3"] = _make_terminal(panes[3], "child_h_bot", 0)
    layout["child_h_bot_r"] = {"type": "HPaned", "parent": "child_h_bot", "order": "1", "ratio": "0.5"}
    layout["terminal4"] = _make_terminal(panes[4], "child_h_bot_r", 0)
    layout["terminal5"] = _make_terminal(panes[5], "child_h_bot_r", 1)
    return layout


def _generate_grid_3x2(layout: dict, panes: list[TerminalPane]) -> dict:
    layout["child_v1"] = {"type": "VPaned", "parent": "window0", "ratio": "0.3333"}
    layout["child_h_row0"] = {"type": "HPaned", "parent": "child_v1", "order": "0", "ratio": "0.5"}
    layout["terminal0"] = _make_terminal(panes[0], "child_h_row0", 0)
    layout["terminal1"] = _make_terminal(panes[1], "child_h_row0", 1)

    layout["child_v2"] = {"type": "VPaned", "parent": "child_v1", "order": "1", "ratio": "0.5"}
    layout["child_h_row1"] = {"type": "HPaned", "parent": "child_v2", "order": "0", "ratio": "0.5"}
    layout["terminal2"] = _make_terminal(panes[2], "child_h_row1", 0)
    layout["terminal3"] = _make_terminal(panes[3], "child_h_row1", 1)

    layout["child_h_row2"] = {"type": "HPaned", "parent": "child_v2", "order": "1", "ratio": "0.5"}
    layout["terminal4"] = _make_terminal(panes[4], "child_h_row2", 0)
    layout["terminal5"] = _make_terminal(panes[5], "child_h_row2", 1)
    return layout


def _make_terminal(pane: TerminalPane, parent: str, order: int) -> dict[str, str]:
    terminal: dict[str, str] = {
        "type": "Terminal",
        "parent": parent,
        "order": str(order),
        "directory": pane.cwd,
    }

    # ~/.bashrc typically has an interactive-shell guard (case $- in *i*)
    # that prevents nvm from loading in non-interactive `bash -c`.
    # Source nvm directly to ensure node-based CLIs are found.
    nvm_init = 'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"'

    if pane.session_id and pane.source == "claude":
        args_str = " ".join(shlex.quote(a) for a in EXTRA_ARGS)
        cmd = (
            f'{nvm_init}; '
            f'{shlex.quote(CLAUDE_BIN)} --resume {pane.session_id} {args_str}; '
            f'exec bash'
        )
        terminal["command"] = f"bash -c {shlex.quote(cmd)}"
    elif pane.session_id and pane.source == "codex":
        cmd = (
            f'{nvm_init}; '
            f'cd {shlex.quote(pane.cwd)} && codex resume {pane.session_id}; '
            f'exec bash'
        )
        terminal["command"] = f"bash -c {shlex.quote(cmd)}"

    return terminal


# ============================================================
# Terminator config file operations
# ============================================================

def _write_terminator_layout(layout: dict[str, dict[str, str]]) -> None:
    TERMINATOR_CONFIG.parent.mkdir(parents=True, exist_ok=True)

    if TERMINATOR_CONFIG.exists():
        content = TERMINATOR_CONFIG.read_text(encoding="utf-8")
        lines = _remove_existing_layout(content, LAYOUT_NAME)
    else:
        lines = [
            "[global_config]", "[keybindings]", "[profiles]", "  [[default]]",
            "[layouts]", "  [[default]]",
            "    [[[window0]]]", "      type = Window", '      parent = ""',
            "    [[[child1]]]", "      type = Terminal", "      parent = window0",
            "[plugins]",
        ]

    layout_section = _format_layout_section(layout)

    insert_idx = len(lines)
    in_layouts = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[layouts]":
            in_layouts = True
            continue
        if in_layouts and stripped.startswith("[") and not stripped.startswith("[["):
            insert_idx = i
            break

    for j, section_line in enumerate(layout_section):
        lines.insert(insert_idx + j, section_line)

    TERMINATOR_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _remove_existing_layout(content: str, name: str) -> list[str]:
    lines = content.splitlines()
    result: list[str] = []
    skip = False
    target_header = f"[[{name}]]"

    for line in lines:
        stripped = line.strip()
        if stripped == target_header:
            skip = True
            continue
        if skip:
            if stripped.startswith("[[") and not stripped.startswith("[[["):
                if stripped != target_header:
                    skip = False
            elif stripped.startswith("[") and not stripped.startswith("[["):
                skip = False
        if not skip:
            result.append(line)

    return result


def _format_layout_section(layout: dict[str, dict[str, str]]) -> list[str]:
    lines: list[str] = [f"  [[{LAYOUT_NAME}]]"]
    for node_name, props in layout.items():
        lines.append(f"    [[[{node_name}]]]")
        for key, value in props.items():
            if key == "parent" and value == "":
                lines.append(f"      {key} = \"\"")
            else:
                lines.append(f"      {key} = {value}")
    return lines


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


def _map_sessions_to_pts(sessions: list[dict]) -> dict[str, dict]:
    pts_map: dict[str, dict] = {}
    for s in sessions:
        pid = s["pid"]
        try:
            fd0 = os.readlink(f"/proc/{pid}/fd/0")
            if fd0.startswith("/dev/pts/"):
                pts_map[fd0] = s
        except OSError:
            continue
    return pts_map


def _get_bash_panes(terminator_pid: int) -> list[tuple[str, str]]:
    try:
        result = subprocess.run(
            ["pstree", "-p", str(terminator_pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
    except subprocess.TimeoutExpired:
        return []

    bash_pids = re.findall(r"bash\((\d+)\)", result.stdout)
    panes: list[tuple[str, str]] = []

    for pid_str in bash_pids:
        pid = int(pid_str)
        try:
            pts = os.readlink(f"/proc/{pid}/fd/0")
            if not pts.startswith("/dev/pts/"):
                continue
            cwd = os.readlink(f"/proc/{pid}/cwd")
            panes.append((pts, cwd))
        except OSError:
            continue

    return panes


def _get_window_geometry(terminator_pid: int) -> str:
    try:
        result = subprocess.run(
            ["xdotool", "search", "--pid", str(terminator_pid)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            wid = result.stdout.strip().split("\n")[0]
            return _geometry_from_xwininfo(wid)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "1800x990+96+71"


def _geometry_from_xwininfo(wid: str) -> str:
    try:
        result = subprocess.run(
            ["xwininfo", "-id", wid],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return "1800x990+96+71"

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
    return "1800x990+96+71"


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
