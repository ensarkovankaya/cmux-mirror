# cmux-mirror

Mirror remote [cmux](https://github.com/manaflow-ai/cmux) workspace and pane structure to local cmux with matching names. Each local pane connects to the corresponding remote tmux session via SSH.

## Requirements

- [uv](https://github.com/astral-sh/uv)
- [cmux](https://github.com/manaflow-ai/cmux) on both local and remote machines
- tmux on the remote machine
- SSH access to the remote machine (default host: `home`)

## How It Works

1. Connects to the remote machine via SSH and collects the cmux workspace/pane/surface structure, tmux session list, and file-based session mappings from `~/.cmux-sessions/`
2. Maps each surface to its tmux session using file-based mappings written by `cmux.sh` (stale mappings pointing to dead sessions are skipped)
3. Creates workspaces and panes locally in cmux with the same names. Existing workspaces are detected — if a workspace already exists but has fewer panes than the remote, only the missing panes are added (incremental sync)
4. Sends `ssh -t <host> tmux attach-session -t <session>` to each new pane

## Installation

```bash
# Run directly (no install needed)
uvx --from git+https://github.com/ensarkovankaya/cmux-mirror cmux-mirror

# Or install globally
uv tool install git+https://github.com/ensarkovankaya/cmux-mirror
```

## Usage

```bash
# Sync remote workspaces to local (default command)
cmux-mirror sync

# Sync from a custom host
cmux-mirror sync myhost

# Custom remote PATH
cmux-mirror sync myhost --remote-path /usr/local/bin:/usr/bin:/bin

# Custom cmux socket path
cmux-mirror sync --socket /tmp/cmux-debug.sock

# Show local workspace structure as ASCII tree
cmux-mirror show local

# Show remote workspace structure (via SSH)
cmux-mirror show remote
cmux-mirror show remote myhost

# Run without installing
uvx --from git+https://github.com/ensarkovankaya/cmux-mirror cmux-mirror sync

# Run from a local clone
uv run cmux-mirror sync
python -m cmux_mirror sync
```

### Socket Discovery

The script connects to cmux via its Unix socket. The socket path is resolved in order:

1. `--socket` argument
2. `CMUX_SOCKET_PATH` environment variable
3. Known candidates: `~/Library/Application Support/cmux/cmux.sock`, `/tmp/cmux.sock`, `/tmp/cmux-debug.sock`, `/tmp/cmux-staging.sock`, `/tmp/cmux-nightly.sock`
4. `~/.cmuxterm/last-socket-path` (fallback file written by cmux)

If no socket is found, cmux-mirror will automatically start cmux and wait up to 10 seconds for the socket to appear.

This means you can run the script from any terminal — not just from inside cmux.

## Logs

All logs are written to `~/.cmux-mirror/logs/` with a timestamped filename. The log file includes debug-level details while stderr only shows info-level messages.

## Remote Setup

Source [`cmux.sh`](cmux.sh) from your shell profile (e.g. `~/.zshrc`) on the remote machine:

```bash
source /path/to/cmux.sh
```

When a terminal opens inside cmux, `cmux.sh` automatically:

1. Detects the current surface using `cmux tree --all --json`
2. Starts a tmux session named `cmux_v1_w-<workspace_id>_p-<pane_index>_s-<surface_id>`
3. Persists the surface-to-session mapping in `~/.cmux-sessions/` so cmux-mirror can discover it

This is required for the sync command to work — it connects to these tmux sessions from the local machine.
