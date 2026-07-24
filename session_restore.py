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
    pts_to_codex = _map_codex_to_pts()
    bash_panes = _get_bash_panes(terminator_pid)

    if not bash_panes:
        return None

    panes: list[TerminalPane] = []
    for i, (pts, cwd) in enumerate(sorted(bash_panes, key=lambda x: x[0])):
        session_info = pts_to_session.get(pts)
        codex_id = pts_to_codex.get(pts)
        if session_info:
            sid, source = session_info["sessionId"], "claude"
        elif codex_id:
            sid, source = codex_id, "codex"
        else:
            sid, source = None, None
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


LAYOUT_CACHE = Path.home() / ".cache" / "terminator_layout_snapshot.json"


def _layout_from_cache(snapshot: SessionSnapshot) -> dict[str, dict[str, str]] | None:
    """Rebuild the exact pane arrangement from the LayoutSnapshot plugin cache.

    Requires the layout_snapshot.py terminator plugin (see README).
    Falls back to None (generic grid) if the cache is missing or stale.
    """
    try:
        data = json.loads(LAYOUT_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    cache_layout = data.get("layout") or {}
    cache_terms = data.get("terminals") or {}
    if not cache_layout:
        return None

    term_nodes = {k: v for k, v in cache_layout.items()
                  if v.get("type") == "Terminal"}
    if len(term_nodes) != len(snapshot.panes):
        return None

    unmatched = list(snapshot.panes)

    def take_pane(cwd: str) -> TerminalPane | None:
        for i, p in enumerate(unmatched):
            if p.cwd == cwd:
                return unmatched.pop(i)
        return None

    result: dict[str, dict[str, str]] = {}
    for name, node in cache_layout.items():
        node_type = node.get("type")
        parent = node.get("parent", "")
        order = str(node.get("order", 0))

        if node_type == "Window":
            geo = snapshot.window_geometry
            width, rest = geo.split("x", 1)
            height, pos = rest.split("+", 1)
            pos_x, pos_y = pos.split("+", 1)
            result[name] = {
                "type": "Window",
                "parent": "",
                "size": f"{width}, {height}",
                "position": f"{pos_x}:{pos_y}",
            }
        elif node_type in ("VPaned", "HPaned"):
            entry = {"type": node_type, "parent": parent, "order": order}
            if node.get("ratio") is not None:
                entry["ratio"] = str(round(float(node["ratio"]), 4))
            result[name] = entry
        elif node_type == "Terminal":
            uuid = str(node.get("uuid", ""))
            cwd = (cache_terms.get(uuid) or {}).get("cwd", "")
            pane = take_pane(cwd)
            if pane is None:
                pane = unmatched.pop(0) if unmatched else None
            if pane is None:
                return None
            term = _make_terminal(pane, parent, int(order))
            term["order"] = order
            result[name] = term
        else:
            return None

    return result


def restore_sessions(state_file: Path | None = None) -> bool:
    state_file = state_file or STATE_FILE
    _log(f"restore_sessions: state_file={state_file}")
    snapshot = _best_snapshot(state_file)

    if not snapshot or not snapshot.panes:
        _log("No snapshot data, aborting")
        return False

    _log(f"Restoring {len(snapshot.panes)} panes, {_count_sessions(snapshot.panes)} sessions")
    layout = _layout_from_cache(snapshot)
    if layout:
        _log("Using exact layout from plugin cache")
    else:
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

    # Generic grid: 2 columns up to 4 panes, 3 columns beyond
    cols = 2 if n <= 4 else 3
    rows = -(-n // cols)  # ceil division
    row_slices = [panes[r * cols:(r + 1) * cols] for r in range(rows)]

    _build_vpaned_rows(layout, row_slices, "window0", 0)
    return layout


def _build_vpaned_rows(layout: dict, row_slices: list[list[TerminalPane]],
                       parent: str, order: int) -> None:
    """Build a vertical chain of rows; each row is a horizontal chain of terminals."""
    k = len(row_slices)
    if k == 1:
        _build_hpaned_row(layout, row_slices[0], parent, order)
        return

    current_parent = parent
    current_order = order
    for r in range(k - 1):
        node = f"row_v{r}"
        ratio = round(1.0 / (k - r), 4)
        layout[node] = {
            "type": "VPaned",
            "parent": current_parent,
            "order": str(current_order),
            "ratio": str(ratio),
        }
        _build_hpaned_row(layout, row_slices[r], node, 0)
        if r == k - 2:
            _build_hpaned_row(layout, row_slices[r + 1], node, 1)
        else:
            current_parent = node
            current_order = 1


def _build_hpaned_row(layout: dict, row_panes: list[TerminalPane],
                      parent: str, order: int) -> None:
    """Build a horizontal chain of terminals within one row."""
    m = len(row_panes)
    if m == 1:
        p = row_panes[0]
        layout[f"terminal{p.order}"] = _make_terminal(p, parent, order)
        return

    current_parent = parent
    current_order = order
    for j in range(m - 1):
        p = row_panes[j]
        node = f"col_h{p.order}"
        ratio = round(1.0 / (m - j), 4)
        layout[node] = {
            "type": "HPaned",
            "parent": current_parent,
            "order": str(current_order),
            "ratio": str(ratio),
        }
        layout[f"terminal{p.order}"] = _make_terminal(p, node, 0)
        if j == m - 2:
            last = row_panes[j + 1]
            layout[f"terminal{last.order}"] = _make_terminal(last, node, 1)
        else:
            current_parent = node
            current_order = 1


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
    qdir = shlex.quote(pane.cwd)
    # Removable media may not be mounted yet at boot — wait up to 60s
    cd_wait = (
        f'for i in $(seq 1 30); do [ -d {qdir} ] && break; sleep 2; done; '
        f'cd {qdir} 2>/dev/null || echo "warning: {pane.cwd} unavailable"'
    )

    if pane.session_id and pane.source == "claude":
        args_str = " ".join(shlex.quote(a) for a in EXTRA_ARGS)
        cmd = (
            f'{nvm_init}; {cd_wait}; '
            f'{shlex.quote(CLAUDE_BIN)} --resume {pane.session_id} {args_str}; '
            f'exec bash'
        )
        terminal["command"] = f"bash -c {shlex.quote(cmd)}"
    elif pane.session_id and pane.source == "codex":
        cmd = (
            f'{nvm_init}; {cd_wait}; '
            f'codex resume {pane.session_id}; '
            f'exec bash'
        )
        terminal["command"] = f"bash -c {shlex.quote(cmd)}"
    else:
        # Empty pane: terminator falls back to $HOME if `directory` doesn't
        # exist yet (removable media not mounted at boot) — wait then cd.
        cmd = f'{cd_wait}; exec bash'
        terminal["command"] = f"bash -c {shlex.quote(cmd)}"

    return terminal


_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def _map_codex_to_pts() -> dict[str, str]:
    """Map pts device -> codex session UUID by scanning codex processes.

    The session UUID is extracted from the open rollout file
    ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
    """
    pts_map: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["pgrep", "-x", "codex"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return pts_map
        pids = [p for p in result.stdout.strip().split("\n") if p]
    except subprocess.TimeoutExpired:
        return pts_map

    for pid in pids:
        try:
            fd0 = os.readlink(f"/proc/{pid}/fd/0")
            if not fd0.startswith("/dev/pts/"):
                continue
            fd_dir = f"/proc/{pid}/fd"
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                m = _ROLLOUT_RE.search(target)
                if m and "/.codex/sessions/" in target:
                    pts_map[fd0] = m.group(1)
                    break
        except OSError:
            continue
    return pts_map


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
