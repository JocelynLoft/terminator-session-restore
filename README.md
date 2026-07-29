# terminator-session-restore

Auto-restore [Terminator](https://gnome-terminator.org/) multi-pane layout and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) / [Codex](https://openai.com/index/codex/) sessions after reboot.

If you run multiple Claude Code sessions across Terminator panes, this tool snapshots your terminal state (pane layout, working directories, active session IDs) and restores everything on next boot — including auto-resuming each Claude conversation.

## Features

- Captures each Terminator pane's working directory and active Claude/Codex session
- Restores **one tab per captured pane**, each in its original working directory
- Auto-resumes Claude Code sessions via `claude --resume <session-id>`
- **Never reads or writes `~/.config/terminator/config`.** The layout is handed to
  Terminator as an in-memory overlay via `--config-json`; your own config, profiles
  and keybindings are used untouched
- Shell-agnostic — works with bash, zsh, fish, and wrappers that spawn a nested shell
- Restored tabs drop you back into your login shell (`$SHELL`)
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
- **Terminator 2.1+** — needs the `--config-json` flag
- **Claude Code CLI** (`claude`) installed via npm/nvm
- System tools: `xdotool`, `xwininfo` (window geometry only)

```bash
# Ubuntu/Debian
sudo apt install terminator xdotool x11-utils
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
CLAUDE_BIN = "claude"          # Claude CLI binary name
EXTRA_ARGS = []                # Extra args for claude --resume
TERMINATOR_BIN = "terminator"  # Terminator binary name
```

> **Note on `EXTRA_ARGS`:** adding `--dangerously-skip-permissions` here makes every
> session resume at boot with permission prompts disabled, unattended, in your project
> directories. Leave it empty unless you fully accept that.

## How It Works

### Snapshot

1. `/proc` is walked to find Terminator's direct children — one shell per pane
2. Each pane's cwd comes from the deepest shell in its subtree, so wrappers that
   run the interactive shell one level down (e.g. zsh-smart-suggestions) report
   the directory you actually see rather than `$HOME`
3. `~/.claude/sessions/*.json` provides active session info (pid, sessionId, cwd);
   a session is attached to a pane when its pid is anywhere in that pane's subtree
4. Codex sessions are identified from the open `rollout-*.jsonl` file
5. `xdotool` + `xwininfo` captures window geometry
6. State saved as JSON to `data/session_state.json`

### Restore

1. Reads snapshot JSON
2. Writes a partial-config overlay to `data/terminator_layout.json` — one tab per pane
3. Launches `terminator --config-json data/terminator_layout.json --geometry=...`
4. Tabs with Claude sessions run `claude --resume <session-id>` automatically,
   then hand off to `$SHELL`
5. nvm is sourced directly (bypasses the `~/.bashrc` interactive guard) to ensure
   `claude` is in PATH

### Why your Terminator config is never modified

`--config-json` (Terminator 2.1+) merges the layout into Terminator's **in-memory**
config under the reserved name `__internal_json_layout__`. Terminator's own
`Config.save()` explicitly skips that name, so the layout is never persisted — even
if you open Preferences and change a setting in a restored window. Your real config
is still loaded normally, so all your profiles, keybindings and plugins apply.

Since the working directory is restored by `cd`-ing inside each tab's command, the
layout needs no `directory` key, and no Terminator plugin is required.

## File Structure

```
terminator-session-restore/
├── session_restore.py    # Main script (single file, no dependencies)
├── install.sh            # One-click installer
├── README.md
├── LICENSE
└── data/                 # Auto-created, git-ignored
    ├── session_state.json        # Current snapshot
    ├── session_state.restored    # Backup after restore
    └── terminator_layout.json    # Generated layout overlay for Terminator
```

## Known Limitations

- **Linux only** — relies on `/proc` filesystem and X11 tools
- **Split layout inside a tab is not preserved.** Each captured pane is restored as
  its own tab with a single terminal; if you had two panes split inside one tab, you
  get two tabs back
- Tab order follows process-creation order, which usually but not always matches the
  original tab order
- Claude `--resume` requires the session history to exist in `~/.claude/`
- Snapshot overwrite protection: won't overwrite a snapshot with more sessions with one that has fewer

## License

[MIT](LICENSE)

---

# terminator-session-restore (中文)

重启电脑后自动恢复 Terminator 的标签页（每个原有面板一个标签页，并回到原工作目录），
并自动 resume 所有 Claude Code / Codex 对话。

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

- Python 3.10+、Terminator 2.1+（需要 `--config-json`）、Claude Code CLI
- 系统工具：`xdotool`, `xwininfo`

```bash
# Ubuntu/Debian
sudo apt install terminator xdotool x11-utils
```

> 本工具不会读写 `~/.config/terminator/config`：布局通过 `--config-json`
> 仅注入到 Terminator 的内存配置中。每个原有面板恢复为一个独立标签页。

## 命令

| 命令 | 说明 |
|------|------|
| `python3 session_restore.py snapshot` | 保存当前终端状态快照 |
| `python3 session_restore.py restore` | 从快照恢复终端会话 |
| `python3 session_restore.py status` | 查看快照状态 |
| `python3 session_restore.py install` | 安装开机自启 |
| `python3 session_restore.py -v restore` | 调试模式（输出详细日志） |
