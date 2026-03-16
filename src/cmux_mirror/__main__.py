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

REMOTE_SCRIPT = r"""
echo '___TREE_JSON_START___'
cmux tree --all --json 2>/dev/null
echo '___TREE_JSON_END___'
echo '___TMUX_SESSIONS_START___'
tmux list-sessions -F '#{session_name}|#{session_created}' 2>/dev/null || true
echo '___TMUX_SESSIONS_END___'
"""

SESSION_RE = re.compile(
    r"^cmux_w-(?P<workspace>[A-Fa-f0-9-]+)_p-(?P<pane>\d+)_s-(?P<surface>[A-Fa-f0-9-]+)$"
)


def parse_session_name(name: str) -> dict | None:
    """Parse a new-format session name into its components."""
    m = SESSION_RE.match(name)
    if not m:
        return None
    return {
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


def parse_remote_data(raw: str) -> tuple[dict, list[tuple[str, int]]]:
    tree_json_raw = extract_section(raw, "___TREE_JSON_START___", "___TREE_JSON_END___")
    tmux_sessions_raw = extract_section(
        raw, "___TMUX_SESSIONS_START___", "___TMUX_SESSIONS_END___"
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

    log.debug("Parsed tree: %d windows", len(tree.get("windows", [])))
    log.debug("Tree JSON (first 2000 chars): %s", tree_json_raw[:2000])
    log.debug("Parsed tmux sessions (%d): %s", len(sessions), sessions)

    return tree, sessions


def map_sessions_to_surfaces(
    sessions: list[tuple[str, int]], tree: dict
) -> dict[str, str]:
    """Map surface refs to session names by matching surface UUIDs.

    Falls back to matching by (workspace_id, pane_index) when surface UUIDs
    don't match (e.g. after a cmux restart that assigns new surface UUIDs).
    When no UUID fields exist in the tree, uses creation-time based positional
    matching to align session workspace groups with tree workspaces.
    """
    # Build UUID -> ref mapping from tree
    uuid_to_ref: dict[str, str] = {}
    # Also build (workspace_id, pane_index) -> ref for fallback matching
    ws_pane_to_ref: dict[tuple[str, int], str] = {}
    surface_count = 0
    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            ws_id = ws.get("id", "").upper()
            for pane in ws.get("panes", []):
                pane_index = pane.get("index", 0)
                for sf in pane.get("surfaces", []):
                    surface_count += 1
                    # Log a sample surface to confirm field presence
                    if surface_count == 1:
                        log.debug("Sample surface object keys=%s values=%s", list(sf.keys()), {k: sf[k] for k in list(sf.keys())[:6]})
                    if sf.get("id"):
                        uuid_to_ref[sf["id"].upper()] = sf["ref"]
                    if ws_id:
                        ws_pane_to_ref[(ws_id, pane_index)] = sf["ref"]

    log.debug("Surface count: %d, uuid_to_ref entries: %d, ws_pane_to_ref entries: %d",
              surface_count, len(uuid_to_ref), len(ws_pane_to_ref))

    # Parse sessions, match by surface UUID
    surface_to_session: dict[str, str] = {}
    unmatched_sessions: list[tuple[str, dict, int]] = []
    for ses, created in sessions:
        parsed = parse_session_name(ses)
        if not parsed:
            continue
        sf_uuid = parsed["surface"].upper()
        ref = uuid_to_ref.get(sf_uuid)
        if ref:
            surface_to_session[ref] = ses
            log.debug("Mapped (by surface UUID) %s -> %s", ref, ses)
        else:
            log.debug("No surface UUID match for session %s (UUID=%s). Available UUIDs: %s",
                      ses, sf_uuid, list(uuid_to_ref.keys())[:10])
            unmatched_sessions.append((ses, parsed, created))

    # Fallback: match by (workspace_id, pane_index) when UUIDs are partially available
    if unmatched_sessions and uuid_to_ref:
        log.info("Surface UUID matching incomplete (%d/%d). Trying workspace+pane fallback...",
                 len(surface_to_session), surface_count)
        for ses, parsed, created in unmatched_sessions:
            ws_uuid = parsed["workspace"].upper()
            pane_idx = parsed["pane_index"]
            ref = ws_pane_to_ref.get((ws_uuid, pane_idx))
            if ref and ref not in surface_to_session:
                surface_to_session[ref] = ses
                log.debug("Mapped (by ws+pane fallback) %s -> %s (ws=%s, pane=%d)",
                          ref, ses, ws_uuid, pane_idx)
            else:
                log.debug("Fallback miss for session %s (ws=%s, pane=%d). Available ws+pane keys: %s",
                          ses, ws_uuid, pane_idx, list(ws_pane_to_ref.keys())[:10])

    # Positional fallback: sort workspace groups by creation time
    if not surface_to_session and unmatched_sessions:
        log.info("No UUID fields in tree. Using creation-time positional matching...")

        # Group sessions by workspace UUID, track earliest creation time
        ws_groups: dict[str, list[tuple[int, str, int]]] = {}
        ws_min_time: dict[str, int] = {}
        for ses, parsed, created in unmatched_sessions:
            ws_uuid = parsed["workspace"]
            ws_groups.setdefault(ws_uuid, []).append((parsed["pane_index"], ses, created))
            ws_min_time[ws_uuid] = min(ws_min_time.get(ws_uuid, float('inf')), created)

        # Sort each group by pane_index
        for group in ws_groups.values():
            group.sort(key=lambda x: x[0])

        # Sort workspace groups by earliest creation time
        sorted_ws_uuids = sorted(ws_groups.keys(), key=lambda u: ws_min_time[u])

        # Walk tree workspaces in order, match to sorted session groups
        tree_ws_list: list[tuple[str, list[tuple[int, str]]]] = []
        for window in tree.get("windows", []):
            for ws in window.get("workspaces", []):
                surfaces: list[tuple[int, str]] = []
                for pane in ws.get("panes", []):
                    for sf in pane.get("surfaces", []):
                        surfaces.append((pane.get("index", 0), sf["ref"]))
                surfaces.sort(key=lambda x: x[0])
                tree_ws_list.append((ws.get("title", "?"), surfaces))

        for i, ws_uuid in enumerate(sorted_ws_uuids):
            if i >= len(tree_ws_list):
                break
            title, tree_surfaces = tree_ws_list[i]
            group = ws_groups[ws_uuid]
            for (_, ses, _), (_, ref) in zip(group, tree_surfaces):
                surface_to_session[ref] = ses
                log.debug("Mapped (by creation-time) %s -> %s (ws='%s')", ref, ses, title)

    log.debug("Session mapping: %d mapped out of %d sessions", len(surface_to_session), len(sessions))
    return surface_to_session


def build_workspaces(
    tree: dict,
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

    log.info("Built %d workspaces", len(workspaces))
    return workspaces


def get_existing_workspace_titles(*, socket: str) -> set[str]:
    """Return titles of all existing local workspaces."""
    r = cmux_cmd("tree", "--all", "--json", socket=socket)
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    try:
        tree = json.loads(r.stdout)
    except json.JSONDecodeError:
        return set()
    titles: set[str] = set()
    for window in tree.get("windows", []):
        for ws in window.get("workspaces", []):
            title = ws.get("title", "").strip()
            if title:
                titles.add(title)
    return titles


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


def create_local_workspaces(
    host: str, workspaces: list[WorkspaceInfo], *, socket: str
) -> None:
    ensure_window(socket)
    existing_titles = get_existing_workspace_titles(socket=socket)
    log.info("Creating local cmux workspaces and panes...")

    for ws_info in workspaces:
        if ws_info.title in existing_titles:
            log.info("Workspace '%s' already exists, skipping", ws_info.title)
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
    tree, sessions = parse_remote_data(raw)
    surface_to_session = map_sessions_to_surfaces(sessions, tree)
    workspaces = build_workspaces(tree, surface_to_session)
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
