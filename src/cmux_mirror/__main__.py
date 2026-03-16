"""Mirror remote cmux workspace and pane structure to local cmux.

Each local pane connects to the corresponding remote tmux session via SSH.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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

REMOTE_SCRIPT = r"""
echo '___TREE_JSON_START___'
cmux tree --all --json 2>/dev/null
echo '___TREE_JSON_END___'
echo '___TMUX_SESSIONS_START___'
tmux list-sessions -F '#{session_name}' 2>/dev/null || true
echo '___TMUX_SESSIONS_END___'
echo '___SCREEN_MAP_START___'
for ws_ref in $(cmux list-workspaces 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i ~ /^workspace:/) print $i}'); do
  surfaces=$(cmux list-pane-surfaces --workspace "$ws_ref" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i ~ /^surface:/) print $i}')
  for sf_ref in $surfaces; do
    screen_line=$(cmux read-screen --workspace "$ws_ref" --surface "$sf_ref" --lines 1 2>/dev/null || true)
    prefix=$(echo "$screen_line" | grep -o 'cmux-[A-F0-9]*' | head -1 || true)
    echo "$ws_ref|$sf_ref|$prefix"
  done
done
echo '___SCREEN_MAP_END___'
"""


class MirrorError(Exception):
    pass


@dataclass
class SurfaceInfo:
    pane_index: int
    tmux_session: str = ""


@dataclass
class WorkspaceInfo:
    title: str
    surfaces: list[SurfaceInfo] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cmux-mirror",
        description="Mirror remote cmux workspace structure to local cmux.",
    )
    parser.add_argument(
        "host",
        nargs="?",
        default="home",
        help="SSH host to mirror from (default: home)",
    )
    parser.add_argument(
        "--remote-path",
        default="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        help="PATH to prepend on the remote machine",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="cmux socket path (default: $CMUX_SOCKET_PATH or /tmp/cmux.sock)",
    )
    return parser.parse_args()


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

    tried = ", ".join(str(c) for c in SOCKET_CANDIDATES)
    raise MirrorError(
        f"cmux socket not found. Tried: {tried}. "
        "Make sure cmux is running or use --socket to specify the path."
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


def parse_remote_data(raw: str) -> tuple[dict, list[str], str]:
    tree_json_raw = extract_section(raw, "___TREE_JSON_START___", "___TREE_JSON_END___")
    tmux_sessions_raw = extract_section(
        raw, "___TMUX_SESSIONS_START___", "___TMUX_SESSIONS_END___"
    )
    screen_map_raw = extract_section(
        raw, "___SCREEN_MAP_START___", "___SCREEN_MAP_END___"
    )

    if not tree_json_raw:
        raise MirrorError("Failed to retrieve cmux tree JSON")
    if not tmux_sessions_raw:
        raise MirrorError("Failed to retrieve tmux session list")

    tree = json.loads(tree_json_raw)
    sessions = [s.strip() for s in tmux_sessions_raw.strip().splitlines() if s.strip()]

    log.debug("Parsed tree: %d windows", len(tree.get("windows", [])))
    log.debug("Parsed tmux sessions (%d): %s", len(sessions), sessions)
    log.debug("Screen map raw:\n%s", screen_map_raw)

    return tree, sessions, screen_map_raw


def map_surfaces_to_sessions(
    sessions: list[str], screen_map_raw: str
) -> tuple[dict[str, str], list[str]]:
    """Map surface refs to tmux session names using screen prefixes.

    The screen shows the tmux status bar with a truncated session name
    followed by the window index. For example, session
    "cmux-E3FD2ED9-E210-..." appears as "cmux-E3FD0" where "E3FD" is
    the truncated UUID and "0" is the tmux window number. We strip the
    last character (window index) and match with startswith.
    """
    surface_to_session: dict[str, str] = {}
    consumed: set[str] = set()

    for line in screen_map_raw.strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3 or not parts[2]:
            log.debug("Skipping screen map line (no prefix): %s", line)
            continue
        sf_ref, raw_prefix = parts[1], parts[2]

        # Strip trailing tmux window index (last char)
        prefix = raw_prefix[:-1] if len(raw_prefix) > len("cmux-") else raw_prefix
        log.debug("Surface %s: raw_prefix=%s, match_prefix=%s", sf_ref, raw_prefix, prefix)

        for ses in sessions:
            if ses in consumed:
                continue
            if ses.startswith(prefix):
                surface_to_session[sf_ref] = ses
                consumed.add(ses)
                log.debug("  Mapped %s -> %s", sf_ref, ses)
                break
        else:
            log.debug("  No session match for %s (prefix=%s)", sf_ref, prefix)

    remaining = [s for s in sessions if s not in consumed]
    log.debug(
        "Surface mapping: %d mapped, %d remaining sessions",
        len(surface_to_session), len(remaining),
    )
    log.debug("Remaining sessions: %s", remaining)
    return surface_to_session, remaining


def build_workspaces(
    tree: dict,
    sessions: list[str],
    surface_to_session: dict[str, str],
) -> list[WorkspaceInfo]:
    workspaces: list[WorkspaceInfo] = []

    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            surfaces: list[SurfaceInfo] = []
            for pane in ws.get("panes", []):
                for surface in pane.get("surfaces", []):
                    sf = SurfaceInfo(
                        pane_index=pane.get("index", 0),
                        tmux_session=surface_to_session.get(surface["ref"], ""),
                    )
                    surfaces.append(sf)
                    log.debug(
                        "  Surface ref=%s pane_index=%d -> session=%s",
                        surface["ref"], sf.pane_index, sf.tmux_session or "(none)",
                    )
            workspaces.append(WorkspaceInfo(title=ws["title"], surfaces=surfaces))
            log.debug("Workspace '%s': %d surfaces", ws["title"], len(surfaces))

    # Second pass: match unmatched surfaces by workspace UUID
    remaining = list(sessions)
    for ws_info in workspaces:
        unmatched = [s for s in ws_info.surfaces if not s.tmux_session]
        matched = [s for s in ws_info.surfaces if s.tmux_session]
        if not unmatched or not matched:
            continue
        # Extract workspace UUID (first 36 chars after "cmux-")
        ws_uuid = matched[0].tmux_session[len("cmux-") : len("cmux-") + 36]
        candidates = [s for s in remaining if s.startswith(f"cmux-{ws_uuid}-")]
        log.debug(
            "Workspace '%s': %d unmatched, %d candidates (uuid=%s)",
            ws_info.title, len(unmatched), len(candidates), ws_uuid,
        )
        for i, surface in enumerate(unmatched):
            if i < len(candidates):
                surface.tmux_session = candidates[i]
                remaining.remove(candidates[i])
                log.debug("  Fallback matched surface -> %s", candidates[i])

    log.info("Built %d workspaces", len(workspaces))
    return workspaces


def ensure_window(socket: str) -> None:
    """Ensure a cmux window exists, creating one if needed."""
    r = cmux_cmd("list-windows", socket=socket)
    if r.returncode != 0 or not r.stdout.strip():
        log.info("No cmux window found, creating one...")
        r = cmux_cmd("new-window", socket=socket)
        if r.returncode != 0:
            raise MirrorError(
                f"Could not create cmux window: {(r.stderr or r.stdout).strip()}"
            )
        time.sleep(0.5)
    else:
        log.debug("Existing cmux window found")


def create_local_workspaces(
    host: str, workspaces: list[WorkspaceInfo], *, socket: str
) -> None:
    ensure_window(socket)
    log.info("Creating local cmux workspaces and panes...")

    for ws_info in workspaces:
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

        for sf in ws_info.surfaces:
            if first_surface:
                first_surface = False
                sf_ref = first_sf_ref
                log.debug("First surface, using existing ref=%s", sf_ref)
            else:
                if sf.pane_index > current_pane_index:
                    log.debug("Creating new split (direction=right)")
                    r = cmux_cmd("new-split", "right", "--workspace", ws_ref, socket=socket)
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

            if sf.tmux_session:
                ssh_cmd = (
                    f"ssh -t -o SetEnv=TERM=xterm-256color {host} "
                    f"'PATH=/opt/homebrew/bin:/usr/local/bin:$PATH "
                    f'tmux attach-session -t "{sf.tmux_session}"\''
                )
                log.info("  -> %s", sf.tmux_session)
                log.debug("  SSH command: %s (surface=%s)", ssh_cmd, sf_ref)
                if sf_ref:
                    cmux_cmd("send", "--surface", sf_ref, ssh_cmd, socket=socket)
                    time.sleep(0.2)
                    cmux_cmd("send-key", "--surface", sf_ref, "Enter", socket=socket)
                else:
                    cmux_cmd("send", ssh_cmd, socket=socket)
                    time.sleep(0.2)
                    cmux_cmd("send-key", "Enter", socket=socket)
            else:
                log.warning("  No tmux session found, leaving empty terminal")


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


def _run() -> None:
    args = parse_args()
    setup_logging()

    log.debug("Args: host=%s, remote_path=%s, socket=%s", args.host, args.remote_path, args.socket)

    socket = resolve_socket(args.socket)

    raw = fetch_remote_data(args.host, args.remote_path)
    tree, sessions, screen_map_raw = parse_remote_data(raw)
    surface_to_session, remaining_sessions = map_surfaces_to_sessions(
        sessions, screen_map_raw
    )
    workspaces = build_workspaces(tree, remaining_sessions, surface_to_session)
    create_local_workspaces(args.host, workspaces, socket=socket)

    log.info("Done! Remote cmux structure mirrored locally.")


def main() -> None:
    try:
        _run()
    except MirrorError as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
