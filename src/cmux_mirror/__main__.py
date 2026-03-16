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
cmux tree --all --json --id-format uuids 2>/dev/null
echo '___TREE_JSON_END___'
echo '___TMUX_SESSIONS_START___'
tmux list-sessions -F '#{session_name}' 2>/dev/null || true
echo '___TMUX_SESSIONS_END___'
"""


class MirrorError(Exception):
    pass


@dataclass
class SurfaceInfo:
    pane_index: int
    surface_uuid: str
    tmux_session: str = ""


@dataclass
class WorkspaceInfo:
    title: str
    workspace_uuid: str
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


def parse_remote_data(raw: str) -> tuple[dict, set[str]]:
    tree_json_raw = extract_section(raw, "___TREE_JSON_START___", "___TREE_JSON_END___")
    tmux_sessions_raw = extract_section(
        raw, "___TMUX_SESSIONS_START___", "___TMUX_SESSIONS_END___"
    )

    if not tree_json_raw:
        raise MirrorError("Failed to retrieve cmux tree JSON")
    if not tmux_sessions_raw:
        raise MirrorError("Failed to retrieve tmux session list")

    tree = json.loads(tree_json_raw)
    sessions = {s.strip() for s in tmux_sessions_raw.strip().splitlines() if s.strip()}

    log.debug("Parsed tree: %d windows", len(tree.get("windows", [])))
    log.debug("Parsed tmux sessions (%d): %s", len(sessions), sorted(sessions))

    return tree, sessions


def build_workspaces(
    tree: dict, sessions: set[str]
) -> list[WorkspaceInfo]:
    workspaces: list[WorkspaceInfo] = []

    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            ws_uuid = ws["id"]
            surfaces: list[SurfaceInfo] = []

            for pane in ws.get("panes", []):
                for surface in pane.get("surfaces", []):
                    sf_uuid = surface["id"]
                    expected_session = f"cmux-{ws_uuid}-{sf_uuid}"
                    matched = expected_session if expected_session in sessions else ""

                    sf = SurfaceInfo(
                        pane_index=pane.get("index", 0),
                        surface_uuid=sf_uuid,
                        tmux_session=matched,
                    )
                    surfaces.append(sf)

                    if matched:
                        log.debug("  Matched: %s", expected_session)
                    else:
                        log.debug("  No match for expected session: %s", expected_session)

            ws_info = WorkspaceInfo(
                title=ws["title"], workspace_uuid=ws_uuid, surfaces=surfaces
            )
            workspaces.append(ws_info)

            matched_count = sum(1 for s in surfaces if s.tmux_session)
            log.debug(
                "Workspace '%s' (uuid=%s): %d surfaces, %d matched",
                ws["title"], ws_uuid, len(surfaces), matched_count,
            )

    log.info("Built %d workspaces", len(workspaces))
    return workspaces


def create_local_workspaces(
    host: str, workspaces: list[WorkspaceInfo], *, socket: str
) -> None:
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
        time.sleep(0.5)

        r = cmux_cmd("rename-workspace", ws_info.title, socket=socket)
        log.debug("Renamed workspace to '%s' (exit=%d)", ws_info.title, r.returncode)
        time.sleep(0.3)

        current_pane_index = -1
        first_surface = True

        for sf in ws_info.surfaces:
            if first_surface:
                first_surface = False
                log.debug("First surface, skipping pane/surface creation")
            else:
                if sf.pane_index > current_pane_index:
                    log.debug("Creating new split (direction=right)")
                    cmux_cmd("new-split", "right", socket=socket)
                else:
                    log.debug("Creating new surface")
                    cmux_cmd("new-surface", socket=socket)
                time.sleep(0.3)

            current_pane_index = max(current_pane_index, sf.pane_index)

            if sf.tmux_session:
                ssh_cmd = (
                    f"ssh -t {host} "
                    f"'PATH=/opt/homebrew/bin:/usr/local/bin:$PATH "
                    f'tmux attach-session -t "{sf.tmux_session}"\''
                )
                log.info("  -> %s", sf.tmux_session)
                log.debug("  SSH command: %s", ssh_cmd)
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
    tree, sessions = parse_remote_data(raw)
    workspaces = build_workspaces(tree, sessions)
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
