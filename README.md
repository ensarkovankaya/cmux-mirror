# cmux-mirror

Mirror remote [cmux](https://github.com/manaflow-ai/cmux) workspace and pane structure to local cmux with matching names. Each local pane connects to the corresponding remote tmux session via SSH.

## Requirements

- [uv](https://github.com/astral-sh/uv)
- [cmux](https://github.com/manaflow-ai/cmux) on both local and remote machines
- tmux on the remote machine
- SSH access to the remote machine (default host: `home`)

## How It Works

1. Connects to the remote machine via SSH and collects the cmux workspace/pane/surface structure along with the tmux session list
2. Maps each surface to its tmux session by matching surface UUIDs encoded in session names
3. Creates workspaces and panes locally in cmux with the same names
4. Sends `ssh -t <host> tmux attach-session -t <session>` to each pane

## Installation

```bash
# Run directly (no install needed)
uvx --from git+https://github.com/ensarkovankaya/cmux-mirror cmux-mirror

# Or install globally
uv tool install git+https://github.com/ensarkovankaya/cmux-mirror
```

## Usage

```bash
# Default host (home)
cmux-mirror

# Custom host
cmux-mirror myhost

# Custom remote PATH
cmux-mirror myhost --remote-path /usr/local/bin:/usr/bin:/bin

# Custom cmux socket path
cmux-mirror --socket /tmp/cmux-debug.sock

# Run without installing
uvx --from git+https://github.com/ensarkovankaya/cmux-mirror cmux-mirror

# Run from a local clone
uv run cmux-mirror
python -m cmux_mirror
```

The script connects to cmux via its Unix socket. Socket path is resolved in order:
1. `--socket` argument
2. `CMUX_SOCKET_PATH` environment variable
3. `/tmp/cmux.sock` (default)

This means you can run the script from any terminal — not just from inside cmux.

## Logs

All logs are written to `~/.cmux-mirror/logs/` with a timestamped filename. The log file includes debug-level details while stderr only shows info-level messages.

## Remote Setup

Add the following to your shell profile (e.g. `~/.zshrc`) on the remote machine. This automatically starts a tmux session for each cmux terminal, using the workspace and surface IDs as the session name:

```bash
# Auto-start tmux inside cmux terminals
if [ -n "$CMUX_WORKSPACE_ID" ] && [ -z "$TMUX" ]; then
  PANE_INDEX=$(cmux tree --all --json 2>/dev/null | python3 -c "
import sys, json
try:
    tree = json.load(sys.stdin)
    sid = '$CMUX_SURFACE_ID'
    for w in tree.get('windows', []):
        for ws in w.get('workspaces', []):
            for pane in ws.get('panes', []):
                for sf in pane.get('surfaces', []):
                    if sf.get('id') == sid:
                        print(pane.get('index', 0)); sys.exit(0)
except Exception:
    pass
print(0)
" 2>/dev/null || echo 0)
  SESSION_NAME="cmux_w-${CMUX_WORKSPACE_ID}_p-${PANE_INDEX}_s-${CMUX_SURFACE_ID}"
  tmux new-session -A -s "$SESSION_NAME"
  cmux close-surface
fi
```

This is required for the mirror script to work — it connects to these tmux sessions from the local machine.
