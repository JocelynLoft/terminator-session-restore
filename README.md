# terminator-session-restore

Auto-restore [Terminator](https://gnome-terminator.org/) multi-pane layout and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) / [Codex](https://openai.com/index/codex/) sessions after reboot.

If you run multiple Claude Code sessions across Terminator panes, this tool snapshots your terminal state (pane layout, working directories, active session IDs) and restores everything on next boot — including auto-resuming each Claude conversation.

## Features

- Captures Terminator pane layout, working directories, and active Claude/Codex sessions
- Restores multi-pane layout with correct grid arrangement (2x3, 3x2, or vertical)
- Auto-resumes Claude Code sessions via `claude --resume <session-id>`
- Empty panes restore to their original working directory
- One-click installer with autostart + optional cron

## Quick Start

```bash
git clone https://github.com/nangua1995/terminator-session-restore.git
cd terminator-session-restore
chmod +x install.sh && ./install.sh
```

The installer will:
- Check/install Python 3.10+, Terminator, and helper tools (pstree, xdotool, xwininfo)
- Configure XDG autostart for boot-time restore
- Optionally set up a per-minute cron snapshot

## Requirements

- **Linux** with X11 (uses `/proc`, `xdotool`, `xwininfo`)
- **Python 3.10+**
- **Terminator** terminal emulator
- **Claude Code CLI** (`claude`) installed via npm/nvm
- System tools: `pstree`, `xdotool`, `xwininfo`

```bash
# Ubuntu/Debian
sudo apt install terminator psmisc xdotool x11-utils
```

## Usage

### Save snapshot

```bash
python3 session_restore.py snapshot
```

```
Saved snapshot: 6 panes
  [0] /home/user/project-a [claude:610e89bb]
  [1] /home/user/project-b [claude:f800f91f]
  [2] /home/user [empty]
  ...
```

### Restore sessions

```bash
python3 session_restore.py restore
```

### Check status

```bash
python3 session_restore.py status
```

### Install/remove autostart

```bash
python3 session_restore.py install
python3 session_restore.py install --uninstall
```

### Verbose mode (for debugging)

```bash
python3 session_restore.py -v restore
# Logs to stderr and /tmp/session_restore_debug.log
```

## Configuration

Edit the constants at the top of `session_restore.py`:

```python
CLAUDE_BIN = "claude"                           # Claude CLI binary name
EXTRA_ARGS = ["--dangerously-skip-permissions"] # Extra args for claude --resume
TERMINATOR_BIN = "terminator"                   # Terminator binary name
GRID_LAYOUT = "2x3"                             # Layout for 6 panes: "2x3" / "3x2" / "vertical"
```

## How It Works

### Snapshot

1. `pstree` finds all bash children of the Terminator process
2. `~/.claude/sessions/*.json` provides active Claude session info (pid, sessionId)
3. `/proc/<pid>/fd/0` maps Claude processes to their pts device (terminal pane)
4. `/proc/<pid>/cwd` gets each pane's working directory
5. `xdotool` + `xwininfo` captures window geometry
6. State saved as JSON to `data/session_state.json`

### Restore

1. Reads snapshot JSON
2. Generates Terminator layout config (`[[recovery]]` section in `~/.config/terminator/config`)
3. Launches `terminator --layout=recovery`
4. Panes with Claude sessions run `claude --resume <session-id>` automatically
5. nvm is sourced directly (bypasses `~/.bashrc` interactive guard) to ensure `claude` is in PATH

## File Structure

```
terminator-session-restore/
├── session_restore.py    # Main script (single file, no dependencies)
├── install.sh            # One-click installer
├── README.md
├── LICENSE
└── data/                 # Auto-created snapshot directory
    ├── session_state.json      # Current snapshot
    └── session_state.restored  # Backup after restore
```

## Known Limitations

- **Linux only** — relies on `/proc` filesystem and X11 tools
- Terminator's DBus API doesn't expose `describe_layout()`, so the actual split ratios are approximated (equal splits)
- Claude `--resume` requires the session history to exist in `~/.claude/`
- Snapshot overwrite protection: won't overwrite a snapshot with more sessions with one that has fewer

## License

[MIT](LICENSE)

---

# terminator-session-restore (中文)

重启电脑后自动恢复 Terminator 终端的多面板布局，并自动 resume 所有 Claude Code / Codex 对话。

## 一键安装

```bash
git clone https://github.com/nangua1995/terminator-session-restore.git
cd terminator-session-restore
chmod +x install.sh && ./install.sh
```

## 工作流程

1. 日常使用时，cron 每分钟自动保存终端状态快照
2. 重启电脑后，开机自启自动恢复所有面板和 Claude 会话

## 依赖

- Python 3.10+、Terminator、Claude Code CLI
- 系统工具：`pstree`, `xdotool`, `xwininfo`

```bash
# Ubuntu/Debian
sudo apt install terminator psmisc xdotool x11-utils
```

## 命令

| 命令 | 说明 |
|------|------|
| `python3 session_restore.py snapshot` | 保存当前终端状态快照 |
| `python3 session_restore.py restore` | 从快照恢复终端会话 |
| `python3 session_restore.py status` | 查看快照状态 |
| `python3 session_restore.py install` | 安装开机自启 |
| `python3 session_restore.py -v restore` | 调试模式（输出详细日志） |
