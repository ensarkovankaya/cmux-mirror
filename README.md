# cmux-mirror

Mirror remote [cmux](https://github.com/manaflow-ai/cmux) workspace and pane structure to local cmux with matching names. Each local pane connects to the corresponding remote tmux session via SSH.

## Requirements

- [uv](https://github.com/astral-sh/uv)
- [cmux](https://github.com/manaflow-ai/cmux) on both local and remote machines
- tmux on the remote machine
- SSH access to the remote machine
- Set `CMUX_MIRROR_DEFAULT_REMOTE` env var to avoid passing the host each time (e.g. `export CMUX_MIRROR_DEFAULT_REMOTE=home`)

## How It Works

### Overview

```
  LOCAL MACHINE                              REMOTE MACHINE
 +-------------------+     SSH query      +-------------------+
 |                   | -----------------> |                   |
 |   cmux-mirror     |                    |   cmux + tmux     |
 |                   | <----------------- |                   |
 +-------------------+   tree, sessions,  +-------------------+
         |               session map
         v
 +-------------------+
 |   local cmux      |
 |                   |
 | +-workspace: dev--+ ---ssh--> tmux: cmux_v1_w-abc_p-0_s-def
 | | +-pane 0        |
 | |   +-surface A --+ ---ssh--> tmux: cmux_v1_w-abc_p-0_s-ghi
 | |   +-surface B   |
 | | +-pane 1        |
 | |   +-surface C --+ ---ssh--> tmux: cmux_v1_w-abc_p-1_s-jkl
 | +-----------------+
 +-------------------+
```

### Data Flow

```
1. FETCH                 2. MAP                    3. CREATE & CONNECT

Remote cmux              ~/.cmux-sessions/         Local cmux
+-----------+            +------------------+      +------------------+
| tree JSON | ----+      | surface:abc ->   |      | workspace: dev   |
+-----------+     |      |   cmux_v1_w-..   |      |  pane 0          |
                  +----> | surface:def ->   | ---> |   surface A -------> ssh -t host
Remote tmux       |      |   cmux_v1_w-..   |      |   surface B -------> tmux attach
+-----------+     |      +------------------+      |  pane 1          |
| sessions  | ----+       (stale entries           |   surface C -------> ...
+-----------+              filtered out)           +------------------+
```

### Incremental Sync

When a workspace already exists locally, cmux-mirror only adds the missing panes:

```
Remote has 3 surfaces        Local has 2 surfaces       After sync: 3 surfaces

+-workspace: dev---+        +-workspace: dev---+        +-workspace: dev---+
| surface A        |        | surface X        |        | surface X        |  (kept)
| surface B        |        | surface Y        |        | surface Y        |  (kept)
| surface C        |        +------------------+        | surface Z ---------> ssh
+------------------+                                    +------------------+  (added)
```

### Steps

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
# Sync remote workspaces to local (host is required)
cmux-mirror sync myhost

# Or set a default remote and omit the host
export CMUX_MIRROR_DEFAULT_REMOTE=home
cmux-mirror sync

# Custom remote PATH
cmux-mirror sync myhost --remote-path /usr/local/bin:/usr/bin:/bin

# Custom cmux socket path
cmux-mirror sync myhost --socket /tmp/cmux-debug.sock

# Show local workspace structure as ASCII tree
cmux-mirror show local

# Show remote workspace structure (via SSH)
cmux-mirror show remote myhost
cmux-mirror show remote   # uses $CMUX_MIRROR_DEFAULT_REMOTE

# Run without installing
uvx --from git+https://github.com/ensarkovankaya/cmux-mirror cmux-mirror sync myhost

# Run from a local clone
uv run cmux-mirror sync myhost
python -m cmux_mirror sync myhost
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

### Why tmux?

cmux surfaces don't support direct SSH attachment — there's no way to "connect" to a remote cmux surface from another machine. tmux bridges this gap by providing named sessions that SSH can attach to:

```
Without tmux (doesn't work):

  Local machine               Remote machine
  +-----------+               +-----------+
  | cmux      |   ssh ----X   | cmux      |
  | surface A |               | surface 1 |   <-- no way to attach via SSH
  +-----------+               +-----------+


With tmux (how cmux-mirror works):

  Local machine               Remote machine
  +-----------+               +---------------------------+
  | cmux      |               | cmux                      |
  | surface A | --ssh-------> | surface 1                 |
  |           |  tmux attach  |   +---------------------+ |
  |           |  -t session1  |   | tmux session1       | |
  |           |               |   | (shell runs here)   | |
  +-----------+               +--+---------------------+-+
```

Each remote cmux surface runs inside a tmux session. The local side attaches to that session over SSH — so you get a live terminal connection to the exact surface.

### What `cmux.sh` does

When a terminal opens inside cmux, `cmux.sh` automatically:

1. Detects the current surface using `cmux tree --all --json`
2. Starts a tmux session named `cmux_v1_w-<workspace_id>_p-<pane_index>_s-<surface_id>`
3. Persists the surface-to-session mapping in `~/.cmux-sessions/` so cmux-mirror can discover it

```
  cmux.sh startup flow:

  cmux opens surface
        |
        v
  cmux tree --all --json
        |
        v
  find current surface ref (the one with "here": true)
        |
        v
  tmux new-session -A -s "cmux_v1_w-<ws>_p-<pane>_s-<surface>"
        |
        v
  echo session_name > ~/.cmux-sessions/surface:<ref>
        |
        v
  shell is now running inside tmux (attachable via SSH)
```

This is required for the sync command to work — it connects to these tmux sessions from the local machine.
