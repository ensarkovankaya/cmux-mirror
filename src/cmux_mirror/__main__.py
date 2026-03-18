"""Mirror remote cmux workspace and pane structure to local cmux.

Each local pane connects to the corresponding remote tmux session via SSH.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("cmux-mirror")

SOCKET_CANDIDATES = [
    Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock",
    Path("/tmp/cmux.sock"),
    Path("/tmp/cmux-debug.sock"),
    Path("/tmp/cmux-staging.sock"),
    Path("/tmp/cmux-nightly.sock"),
]

LAST_SOCKET_PATH_FILE = Path.home() / ".cmuxterm" / "last-socket-path"
REMOTE_SESSION_DIR = Path.home() / ".cmux-remote-sessions"

REMOTE_SCRIPT = r"""
echo '___TREE_JSON_START___'
cmux tree --all --json 2>/dev/null
echo '___TREE_JSON_END___'
echo '___TMUX_SESSIONS_START___'
tmux list-sessions -F '#{session_name}|#{session_created}' 2>/dev/null || true
echo '___TMUX_SESSIONS_END___'
echo '___SESSION_MAP_START___'
(for f in ~/.cmux-sessions/surface:*; do
  [ -f "$f" ] && echo "$(basename "$f")|$(cat "$f")"
done) 2>/dev/null || true
echo '___SESSION_MAP_END___'
echo '___PANE_DIMS_START___'
for f in ~/.cmux-sessions/surface:*; do
  [ -f "$f" ] || continue
  SESSION=$(cat "$f")
  DIMS=$(tmux display-message -t "$SESSION" -p '#{pane_width}x#{pane_height}' 2>/dev/null)
  [ -n "$DIMS" ] && echo "$(basename "$f")|$DIMS"
done
echo '___PANE_DIMS_END___'
"""

SESSION_RE = re.compile(
    r"^cmux_v(?P<version>\d+)_w-(?P<workspace>[A-Fa-f0-9-]+)_p-(?P<pane>\d+)_s-(?P<surface>[A-Fa-f0-9-]+)$"
)


def parse_session_name(name: str) -> dict | None:
    """Parse a new-format session name into its components."""
    m = SESSION_RE.match(name)
    if not m:
        return None
    return {
        "version": int(m.group("version")),
        "workspace": m.group("workspace"),
        "pane_index": int(m.group("pane")),
        "surface": m.group("surface"),
    }


class MirrorError(Exception):
    pass


@dataclass
class SurfaceInfo:
    pane_index: int
    tmux_session: str = ""
    split_direction: str = "right"


@dataclass
class WorkspaceInfo:
    title: str
    surfaces: list[SurfaceInfo] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cmux-mirror",
        description="Mirror remote cmux workspace structure to local cmux.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- sync subcommand ---
    sync_parser = subparsers.add_parser(
        "sync", help="Mirror remote cmux structure locally"
    )
    sync_parser.add_argument(
        "host", nargs="?", default=None,
        help="SSH host to mirror from (falls back to $CMUX_MIRROR_DEFAULT_REMOTE)",
    )
    sync_parser.add_argument(
        "--remote-path",
        default="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        help="PATH to prepend on the remote machine",
    )
    sync_parser.add_argument(
        "--socket", default=None,
        help="cmux socket path (default: $CMUX_SOCKET_PATH or /tmp/cmux.sock)",
    )

    # --- show subcommand ---
    show_parser = subparsers.add_parser(
        "show", help="Display workspace structure as ASCII tree"
    )
    show_subparsers = show_parser.add_subparsers(dest="show_command")

    # show remote
    show_remote_parser = show_subparsers.add_parser(
        "remote", help="Display remote workspace structure (via SSH)"
    )
    show_remote_parser.add_argument(
        "host", nargs="?", default=None,
        help="SSH host to query (falls back to $CMUX_MIRROR_DEFAULT_REMOTE)",
    )
    show_remote_parser.add_argument(
        "--remote-path",
        default="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        help="PATH to prepend on the remote machine",
    )

    # show local
    show_local_parser = show_subparsers.add_parser(
        "local", help="Display local workspace structure"
    )
    show_local_parser.add_argument(
        "--socket", default=None,
        help="cmux socket path (default: $CMUX_SOCKET_PATH or auto-discover)",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    if args.command == "show" and args.show_command is None:
        show_parser.print_help()
        sys.exit(0)
    return args


def resolve_host(host_arg: str | None) -> str:
    if host_arg:
        return host_arg
    env = os.environ.get("CMUX_MIRROR_DEFAULT_REMOTE")
    if env:
        return env
    raise MirrorError(
        "No remote host specified. Provide a host argument "
        "(e.g. cmux-mirror sync user@host:22) or set "
        "$CMUX_MIRROR_DEFAULT_REMOTE."
    )


def resolve_socket(socket_arg: str | None) -> str:
    # Explicit path: --socket flag or CMUX_SOCKET_PATH env
    explicit = socket_arg or os.environ.get("CMUX_SOCKET_PATH")
    if explicit:
        p = Path(explicit)
        if not p.is_socket():
            raise MirrorError(
                f"cmux socket not found at {explicit}. "
                "Make sure cmux is running and the socket path is correct."
            )
        log.debug("Using explicit cmux socket: %s", explicit)
        return explicit

    # Auto-discovery: try known candidates
    for candidate in SOCKET_CANDIDATES:
        if candidate.is_socket():
            log.debug("Discovered cmux socket: %s", candidate)
            return str(candidate)

    # Fallback: read last-socket-path file
    if LAST_SOCKET_PATH_FILE.is_file():
        last_path = LAST_SOCKET_PATH_FILE.read_text().strip()
        if last_path and Path(last_path).is_socket():
            log.debug("Using last recorded socket: %s", last_path)
            return last_path

    # Auto-start cmux if not running
    log.info("cmux is not running — starting it automatically…")
    try:
        subprocess.Popen(
            ["cmux", "~"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise MirrorError(
            "cmux executable not found. Install cmux or use --socket to specify the path."
        )

    # Poll for the socket to appear
    all_candidates = list(SOCKET_CANDIDATES)
    if LAST_SOCKET_PATH_FILE.is_file():
        last_path = LAST_SOCKET_PATH_FILE.read_text().strip()
        if last_path:
            all_candidates.append(Path(last_path))

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for candidate in all_candidates:
            if candidate.is_socket():
                log.debug("cmux socket appeared: %s", candidate)
                return str(candidate)
        time.sleep(0.5)

    tried = ", ".join(str(c) for c in SOCKET_CANDIDATES)
    raise MirrorError(
        f"cmux was started but socket did not appear within 10 seconds. Tried: {tried}."
    )


def run_command(
    cmd: list[str], *, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    log.debug("Running command: %s", cmd)
    r = subprocess.run(
        cmd, capture_output=True, text=True, check=check, timeout=timeout
    )
    log.debug("  exit=%d stdout=%r stderr=%r", r.returncode, r.stdout[:200], r.stderr[:200])
    return r


def ssh_exec(
    host: str, script: str, *, remote_path: str, timeout: int = 60
) -> str:
    log.debug("SSH exec on %s (timeout=%ds)", host, timeout)
    r = run_command(
        ["ssh", host, f'export PATH="{remote_path}:$PATH"\n{script}'],
        timeout=timeout,
    )
    return r.stdout


def cmux_cmd(
    *args: str, socket: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return run_command(["cmux", "--socket", socket, *args], check=check, timeout=10)


def fetch_remote_data(host: str, remote_path: str) -> str:
    log.info("Fetching cmux and tmux info from remote (%s)...", host)
    try:
        data = ssh_exec(host, REMOTE_SCRIPT, remote_path=remote_path, timeout=120)
        log.debug("Remote data length: %d bytes", len(data))
        return data
    except Exception as e:
        raise MirrorError(f"SSH connection failed: {e}") from e


def extract_section(data: str, start_marker: str, end_marker: str) -> str:
    lines: list[str] = []
    inside = False
    for line in data.splitlines():
        if start_marker in line:
            inside = True
            continue
        if end_marker in line:
            inside = False
            continue
        if inside:
            lines.append(line)
    result = "\n".join(lines)
    log.debug("Section [%s]: %d lines, %d bytes", start_marker, len(lines), len(result))
    return result


def parse_remote_data(
    raw: str,
) -> tuple[dict, list[tuple[str, int]], dict[str, str], dict[str, tuple[int, int]]]:
    tree_json_raw = extract_section(raw, "___TREE_JSON_START___", "___TREE_JSON_END___")
    tmux_sessions_raw = extract_section(
        raw, "___TMUX_SESSIONS_START___", "___TMUX_SESSIONS_END___"
    )
    session_map_raw = extract_section(
        raw, "___SESSION_MAP_START___", "___SESSION_MAP_END___"
    )
    pane_dims_raw = extract_section(
        raw, "___PANE_DIMS_START___", "___PANE_DIMS_END___"
    )

    if not tree_json_raw:
        raise MirrorError("Failed to retrieve cmux tree JSON")
    if not tmux_sessions_raw:
        raise MirrorError("Failed to retrieve tmux session list")

    tree = json.loads(tree_json_raw)
    sessions: list[tuple[str, int]] = []
    for line in tmux_sessions_raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, ts = line.rsplit("|", 1)
            sessions.append((name.strip(), int(ts.strip())))
        else:
            sessions.append((line, 0))

    # Parse file-based session mapping: surface_ref -> session_name
    session_map: dict[str, str] = {}
    if session_map_raw:
        for line in session_map_raw.strip().splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            ref, session_name = line.split("|", 1)
            session_map[ref.strip()] = session_name.strip()

    # Parse pane dimensions: surface_ref -> (pane_w, pane_h)
    # Format: surface:ref|WxH
    surface_dims: dict[str, tuple[int, int]] = {}
    if pane_dims_raw:
        for line in pane_dims_raw.strip().splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) >= 2 and "x" in parts[1]:
                try:
                    ref = parts[0].strip()
                    pw, ph = parts[1].split("x", 1)
                    surface_dims[ref] = (int(pw), int(ph))
                except ValueError:
                    pass

    log.debug("Parsed tree: %d windows", len(tree.get("windows", [])))
    log.debug("Tree JSON (first 2000 chars): %s", tree_json_raw[:2000])
    log.debug("Parsed tmux sessions (%d): %s", len(sessions), sessions)
    log.debug("Parsed session map (%d entries): %s", len(session_map), session_map)
    log.debug("Parsed pane dims (%d entries): %s", len(surface_dims), surface_dims)

    return tree, sessions, session_map, surface_dims


def map_sessions_to_surfaces(
    sessions: list[tuple[str, int]],
    tree: dict,
    session_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """Map surface refs to session names using file-based mapping.

    Uses ~/.cmux-sessions/ files written by cmux.sh. Stale mappings
    (pointing to tmux sessions that no longer exist) are skipped.
    """
    surface_to_session: dict[str, str] = {}

    if not session_map:
        log.info("No file-based session mappings found")
        return surface_to_session

    # Build set of live tmux session names for stale detection
    live_sessions = {name for name, _ in sessions}

    for ref, session_name in session_map.items():
        if session_name not in live_sessions:
            log.debug("Skipping stale mapping %s -> %s (session not in tmux)", ref, session_name)
            continue
        surface_to_session[ref] = session_name
        log.debug("Mapped (by session file) %s -> %s", ref, session_name)

    log.info("File-based mapping: %d surfaces mapped (%d stale skipped)",
             len(surface_to_session), len(session_map) - len(surface_to_session))
    return surface_to_session


def _infer_split_direction(
    prev_dims: tuple[int, int],
    curr_dims: tuple[int, int],
    ref_width: int,
    ref_height: int,
) -> str:
    """Infer split direction between two panes using their dimensions.

    Each dims tuple is (pane_w, pane_h). ref_width/ref_height are the max
    dimensions across ALL surfaces (approximating the full terminal size).

    Heuristic order:
    1. If prev pane spans full ref height -> horizontal split -> RIGHT
    2. If prev pane spans full ref width -> vertical split -> DOWN
    3. If both panes have same width (±1) -> they share a column -> DOWN
    4. If both panes have same height (±1) -> they share a row -> RIGHT
    5. Fallback -> RIGHT
    """
    pw, ph = prev_dims
    cw, ch = curr_dims

    if abs(ph - ref_height) <= 1:
        return "right"
    if abs(pw - ref_width) <= 1:
        return "down"
    if abs(pw - cw) <= 1:
        return "down"
    if abs(ph - ch) <= 1:
        return "right"
    return "right"


def infer_split_directions(
    tree: dict,
    surface_dims: dict[str, tuple[int, int]],
) -> dict[str, dict[int, str]]:
    """Infer split direction per pane using surface dimensions.

    Uses max width/height across ALL surfaces as the "full terminal" reference,
    since each cmux surface runs in its own tmux session (so window dims == pane dims).

    Returns: {workspace_title: {pane_index: "right" or "down"}}
    """
    # Compute global reference dimensions from all surfaces
    if surface_dims:
        ref_width = max(w for w, h in surface_dims.values())
        ref_height = max(h for w, h in surface_dims.values())
    else:
        ref_width = ref_height = 0

    log.debug("Global ref dims: %dx%d (from %d surfaces)", ref_width, ref_height, len(surface_dims))

    directions: dict[str, dict[int, str]] = {}
    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            title = ws.get("title", "")
            # Collect one dims per pane, keyed by pane index
            pane_dims: list[tuple[int, tuple[int, int]]] = []
            for pane in ws.get("panes", []):
                pane_idx = pane.get("index", 0)
                for surface in pane.get("surfaces", []):
                    ref = surface.get("ref", "")
                    key = f"surface:{ref}" if not ref.startswith("surface:") else ref
                    if key in surface_dims:
                        pane_dims.append((pane_idx, surface_dims[key]))
                        break

            # Sort by pane index
            pane_dims.sort(key=lambda x: x[0])

            ws_dirs: dict[int, str] = {}
            for i in range(1, len(pane_dims)):
                prev_idx, prev_d = pane_dims[i - 1]
                curr_idx, curr_d = pane_dims[i]
                direction = _infer_split_direction(prev_d, curr_d, ref_width, ref_height)
                ws_dirs[curr_idx] = direction
                log.debug(
                    "Workspace '%s' pane %d->%d: %s (prev=%dx%d curr=%dx%d ref=%dx%d)",
                    title, prev_idx, curr_idx, direction,
                    prev_d[0], prev_d[1], curr_d[0], curr_d[1], ref_width, ref_height,
                )

            directions[title] = ws_dirs

    return directions


def build_workspaces(
    tree: dict,
    surface_to_session: dict[str, str],
    split_directions: dict[str, dict[int, str]] | None = None,
) -> list[WorkspaceInfo]:
    workspaces: list[WorkspaceInfo] = []

    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            ws_title = ws.get("title", "")
            ws_dirs = (split_directions or {}).get(ws_title, {})
            surfaces: list[SurfaceInfo] = []
            for pane in ws.get("panes", []):
                pane_idx = pane.get("index", 0)
                pane_direction = ws_dirs.get(pane_idx, "right")
                for surface in pane.get("surfaces", []):
                    sf = SurfaceInfo(
                        pane_index=pane_idx,
                        tmux_session=surface_to_session.get(surface["ref"], ""),
                        split_direction=pane_direction,
                    )
                    surfaces.append(sf)
                    log.debug(
                        "  Surface ref=%s pane_index=%d -> session=%s",
                        surface["ref"], sf.pane_index, sf.tmux_session or "(none)",
                    )
            workspaces.append(WorkspaceInfo(title=ws["title"], surfaces=surfaces))
            log.debug("Workspace '%s': %d surfaces", ws["title"], len(surfaces))

    log.info("Built %d workspaces", len(workspaces))
    return workspaces


def get_existing_workspace_info(*, socket: str) -> dict[str, dict]:
    """Return info about existing local workspaces: {title: {ref, pane_count, surface_refs}}."""
    r = cmux_cmd("tree", "--all", "--json", socket=socket)
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        tree = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    info: dict[str, dict] = {}
    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            title = ws.get("title", "").strip()
            if not title:
                continue
            surface_refs: list[str] = []
            for pane in ws.get("panes", []):
                for surface in pane.get("surfaces", []):
                    ref = surface.get("ref", "")
                    if ref:
                        surface_refs.append(ref)
            ws_ref = ws.get("ref", "")
            info[title] = {
                "ref": ws_ref,
                "pane_count": len(surface_refs),
                "surface_refs": surface_refs,
            }
    return info


def ensure_window(socket: str) -> None:
    """Ensure a cmux window exists, creating one if needed."""
    r = cmux_cmd("list-windows", socket=socket)
    if r.returncode != 0 or not r.stdout.strip() or "No windows" in r.stdout:
        log.info("No cmux window found, creating one...")
        r = cmux_cmd("new-window", socket=socket)
        if r.returncode != 0:
            raise MirrorError(
                f"Could not create cmux window: {(r.stderr or r.stdout).strip()}"
            )
        time.sleep(0.5)
    else:
        log.debug("Existing cmux window found")


def _send_ssh_to_surface(
    host: str, sf: SurfaceInfo, sf_ref: str, *, socket: str
) -> None:
    """Send SSH+tmux attach command to a surface and persist the mapping."""
    if not sf.tmux_session:
        log.warning("  No tmux session found, leaving empty terminal")
        return

    ssh_cmd = (
        f"exec ssh -t -o SetEnv=TERM=xterm-256color {host} "
        f"'PATH=/opt/homebrew/bin:/usr/local/bin:$PATH "
        f'tmux attach-session -t "{sf.tmux_session}"\''
    )
    log.info("  -> %s", sf.tmux_session)
    log.debug("  SSH command: %s (surface=%s)", ssh_cmd, sf_ref)
    if sf_ref:
        cmux_cmd("send", "--surface", sf_ref, ssh_cmd, socket=socket)
        time.sleep(0.2)
        cmux_cmd("send-key", "--surface", sf_ref, "Enter", socket=socket)
        # Persist remote session mapping for show local
        REMOTE_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        (REMOTE_SESSION_DIR / sf_ref).write_text(sf.tmux_session)
        log.debug("Saved remote mapping %s -> %s", sf_ref, sf.tmux_session)
    else:
        cmux_cmd("send", ssh_cmd, socket=socket)
        time.sleep(0.2)
        cmux_cmd("send-key", "Enter", socket=socket)


def _add_missing_panes(
    host: str,
    ws_info: WorkspaceInfo,
    existing: dict,
    *,
    socket: str,
) -> None:
    """Add missing panes to an existing workspace that has fewer surfaces than remote."""
    ws_ref = existing["ref"]
    local_count = existing["pane_count"]
    remote_count = len(ws_info.surfaces)

    log.info(
        "Workspace '%s' exists with %d surface(s), remote has %d — adding %d missing",
        ws_info.title, local_count, remote_count, remote_count - local_count,
    )

    # Select the workspace so splits go to the right place
    cmux_cmd("select-workspace", "--workspace", ws_ref, socket=socket)
    time.sleep(0.3)

    # Clean stale remote session mappings for this workspace's existing surfaces
    for old_ref in existing["surface_refs"]:
        stale = REMOTE_SESSION_DIR / old_ref
        if stale.exists():
            stale.unlink()
            log.debug("Removed stale remote mapping %s", old_ref)

    # Track the highest pane index we've seen so far (from the existing surfaces)
    # and seed pane_surface_map from existing surfaces
    current_pane_index = -1
    pane_surface_map: dict[int, str] = {}
    for i, sf in enumerate(ws_info.surfaces[:local_count]):
        current_pane_index = max(current_pane_index, sf.pane_index)
        if i < len(existing["surface_refs"]) and sf.pane_index not in pane_surface_map:
            pane_surface_map[sf.pane_index] = existing["surface_refs"][i]

    # Re-persist remote session mappings for existing surfaces (without re-sending SSH)
    for i, sf in enumerate(ws_info.surfaces[:local_count]):
        if i < len(existing["surface_refs"]) and sf.tmux_session:
            REMOTE_SESSION_DIR.mkdir(parents=True, exist_ok=True)
            (REMOTE_SESSION_DIR / existing["surface_refs"][i]).write_text(sf.tmux_session)
            log.debug("Re-persisted mapping %s -> %s", existing["surface_refs"][i], sf.tmux_session)

    # Now add splits for the new surfaces
    for sf in ws_info.surfaces[local_count:]:
        if sf.pane_index > current_pane_index:
            split_target = pane_surface_map.get(current_pane_index, "")
            split_args = ["new-split", sf.split_direction, "--workspace", ws_ref]
            if split_target:
                split_args += ["--surface", split_target]
            log.debug("Creating new split (direction=%s, target=%s)", sf.split_direction, split_target)
            r = cmux_cmd(*split_args, socket=socket)
        else:
            log.debug("Creating new surface")
            r = cmux_cmd("new-surface", "--workspace", ws_ref, socket=socket)
        sf_ref = ""
        if r.returncode == 0:
            for word in r.stdout.split():
                if word.startswith("surface:"):
                    sf_ref = word
                    break
        log.debug("New surface ref=%s", sf_ref)
        time.sleep(0.3)

        current_pane_index = max(current_pane_index, sf.pane_index)
        if sf.pane_index not in pane_surface_map and sf_ref:
            pane_surface_map[sf.pane_index] = sf_ref
        _send_ssh_to_surface(host, sf, sf_ref, socket=socket)


def create_local_workspaces(
    host: str, workspaces: list[WorkspaceInfo], *, socket: str
) -> None:
    ensure_window(socket)
    existing_info = get_existing_workspace_info(socket=socket)
    log.info("Creating local cmux workspaces and panes...")

    for ws_info in workspaces:
        existing = existing_info.get(ws_info.title)
        if existing:
            if existing["pane_count"] >= len(ws_info.surfaces):
                log.info("Workspace '%s' already exists with enough panes, skipping", ws_info.title)
                continue
            _add_missing_panes(host, ws_info, existing, socket=socket)
            continue

        log.info("Workspace: %s (%d surfaces)", ws_info.title, len(ws_info.surfaces))

        r = cmux_cmd("new-workspace", socket=socket)
        if r.returncode != 0:
            log.warning(
                "Could not create workspace: %s — %s",
                ws_info.title,
                (r.stderr or r.stdout).strip(),
            )
            continue
        # Parse workspace ref from "OK workspace:N"
        ws_ref = r.stdout.strip().split()[-1] if r.stdout.strip() else ""
        log.debug("Created workspace ref=%s", ws_ref)
        time.sleep(0.5)

        # Select the new workspace so subsequent commands target it
        cmux_cmd("select-workspace", "--workspace", ws_ref, socket=socket)
        time.sleep(0.3)

        cmux_cmd("rename-workspace", "--workspace", ws_ref, ws_info.title, socket=socket)
        log.debug("Renamed workspace to '%s'", ws_info.title)
        time.sleep(0.3)

        # Get the first surface ref from the newly created workspace
        r = cmux_cmd("list-pane-surfaces", "--workspace", ws_ref, socket=socket)
        first_sf_ref = ""
        if r.returncode == 0:
            for word in r.stdout.split():
                if word.startswith("surface:"):
                    first_sf_ref = word
                    break
        log.debug("First surface ref=%s", first_sf_ref)

        current_pane_index = -1
        first_surface = True
        pane_surface_map: dict[int, str] = {}

        for sf in ws_info.surfaces:
            if first_surface:
                first_surface = False
                sf_ref = first_sf_ref
                log.debug("First surface, using existing ref=%s", sf_ref)
            else:
                if sf.pane_index > current_pane_index:
                    split_target = pane_surface_map.get(current_pane_index, "")
                    split_args = ["new-split", sf.split_direction, "--workspace", ws_ref]
                    if split_target:
                        split_args += ["--surface", split_target]
                    log.debug("Creating new split (direction=%s, target=%s)", sf.split_direction, split_target)
                    r = cmux_cmd(*split_args, socket=socket)
                else:
                    log.debug("Creating new surface")
                    r = cmux_cmd("new-surface", "--workspace", ws_ref, socket=socket)
                # Parse surface ref from "OK surface:N workspace:M"
                sf_ref = ""
                if r.returncode == 0:
                    for word in r.stdout.split():
                        if word.startswith("surface:"):
                            sf_ref = word
                            break
                log.debug("New surface ref=%s", sf_ref)
                time.sleep(0.3)

            current_pane_index = max(current_pane_index, sf.pane_index)
            if sf.pane_index not in pane_surface_map and sf_ref:
                pane_surface_map[sf.pane_index] = sf_ref
            _send_ssh_to_surface(host, sf, sf_ref, socket=socket)


def setup_logging() -> None:
    log_dir = Path.home() / ".cmux-mirror" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.log"

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(logging.Formatter("[mirror] %(message)s"))

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("[mirror] %(asctime)s %(levelname)s %(message)s")
    )

    root = logging.getLogger("cmux-mirror")
    root.setLevel(logging.DEBUG)
    root.addHandler(stderr_handler)
    root.addHandler(file_handler)

    log.info("Log file: %s", log_file)


def render_tree_ascii(
    tree: dict,
    surface_to_session: dict[str, str],
    remote_session_map: dict[str, str] | None = None,
) -> None:
    """Print workspace structure as an ASCII tree diagram."""
    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            title = ws.get("title", "(untitled)")
            ws_id = ws.get("id", "")
            id_str = f" [{ws_id}]" if ws_id else ""
            print(f"Workspace: {title}{id_str}")

            panes = ws.get("panes", [])
            for pi, pane in enumerate(panes):
                is_last_pane = pi == len(panes) - 1
                pane_prefix = "\u2514\u2500\u2500 " if is_last_pane else "\u251c\u2500\u2500 "
                child_prefix = "    " if is_last_pane else "\u2502   "
                pane_index = pane.get("index", 0)
                print(f"{pane_prefix}Pane {pane_index}")

                surfaces = pane.get("surfaces", [])
                for si, sf in enumerate(surfaces):
                    is_last_sf = si == len(surfaces) - 1
                    sf_prefix = "\u2514\u2500\u2500 " if is_last_sf else "\u251c\u2500\u2500 "
                    ref = sf.get("ref", "?")
                    session = surface_to_session.get(ref)
                    session_str = f" \u2500\u2500 tmux: {session}" if session else ""
                    remote_session = remote_session_map.get(ref) if remote_session_map else None
                    remote_str = f" \u2500\u2500 remote: {remote_session}" if remote_session else ""
                    print(f"{child_prefix}{sf_prefix}Surface {ref}{session_str}{remote_str}")

            print()


def _run_show_remote(args: argparse.Namespace) -> None:
    """Fetch remote state and print ASCII tree."""
    raw = fetch_remote_data(args.host, args.remote_path)
    tree, sessions, session_map, _surface_dims = parse_remote_data(raw)
    surface_to_session = map_sessions_to_surfaces(sessions, tree, session_map)
    render_tree_ascii(tree, surface_to_session)


def _run_show_local(args: argparse.Namespace) -> None:
    """Query local cmux and print ASCII tree."""
    socket = resolve_socket(args.socket)

    # Get tree JSON
    r = cmux_cmd("tree", "--all", "--json", socket=socket)
    if r.returncode != 0 or not r.stdout.strip():
        raise MirrorError("Failed to retrieve local cmux tree")
    tree = json.loads(r.stdout)

    # Get live tmux sessions
    sessions: list[tuple[str, int]] = []
    try:
        r = run_command(
            ["tmux", "list-sessions", "-F", "#{session_name}|#{session_created}"],
            check=False,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    name, ts = line.rsplit("|", 1)
                    sessions.append((name.strip(), int(ts.strip())))
                else:
                    sessions.append((line, 0))
    except Exception:
        pass

    # Read local session mapping files
    session_map: dict[str, str] = {}
    session_dir = Path.home() / ".cmux-sessions"
    if session_dir.is_dir():
        for f in session_dir.glob("surface:*"):
            if f.is_file():
                session_name = f.read_text().strip()
                if session_name:
                    session_map[f.name] = session_name

    surface_to_session = map_sessions_to_surfaces(sessions, tree, session_map)

    # Read remote session mapping files
    remote_session_map: dict[str, str] = {}
    if REMOTE_SESSION_DIR.is_dir():
        for f in REMOTE_SESSION_DIR.iterdir():
            if f.is_file() and f.name.startswith("surface:"):
                remote_name = f.read_text().strip()
                if remote_name:
                    remote_session_map[f.name] = remote_name

    render_tree_ascii(tree, surface_to_session, remote_session_map)


def _run() -> None:
    args = parse_args()
    setup_logging()

    if args.command == "show":
        if args.show_command == "remote":
            args.host = resolve_host(args.host)
            log.debug("Args: command=show remote, host=%s, remote_path=%s", args.host, args.remote_path)
            _run_show_remote(args)
        elif args.show_command == "local":
            log.debug("Args: command=show local, socket=%s", args.socket)
            _run_show_local(args)
        return

    # command == "sync"
    host = resolve_host(args.host)
    log.debug("Args: host=%s, remote_path=%s, socket=%s", host, args.remote_path, args.socket)

    socket = resolve_socket(args.socket)

    raw = fetch_remote_data(host, args.remote_path)
    tree, sessions, session_map, surface_dims = parse_remote_data(raw)
    surface_to_session = map_sessions_to_surfaces(sessions, tree, session_map)
    split_directions = infer_split_directions(tree, surface_dims)
    workspaces = build_workspaces(tree, surface_to_session, split_directions)
    create_local_workspaces(host, workspaces, socket=socket)

    log.info("Done! Remote cmux structure mirrored locally.")


def main() -> None:
    try:
        _run()
    except MirrorError as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
