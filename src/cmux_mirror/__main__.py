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
echo '___SESSION_MAP_START___'
(for f in ~/.cmux-sessions/surface:*; do
  [ -f "$f" ] && echo "$(basename "$f")|$(cat "$f")"
done) 2>/dev/null || true
echo '___SESSION_MAP_END___'
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
        "host", nargs="?", default="home",
        help="SSH host to mirror from (default: home)",
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
        "host", nargs="?", default="home",
        help="SSH host to query (default: home)",
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


def parse_remote_data(raw: str) -> tuple[dict, list[tuple[str, int]], dict[str, str]]:
    tree_json_raw = extract_section(raw, "___TREE_JSON_START___", "___TREE_JSON_END___")
    tmux_sessions_raw = extract_section(
        raw, "___TMUX_SESSIONS_START___", "___TMUX_SESSIONS_END___"
    )
    session_map_raw = extract_section(
        raw, "___SESSION_MAP_START___", "___SESSION_MAP_END___"
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

    log.debug("Parsed tree: %d windows", len(tree.get("windows", [])))
    log.debug("Tree JSON (first 2000 chars): %s", tree_json_raw[:2000])
    log.debug("Parsed tmux sessions (%d): %s", len(sessions), sessions)
    log.debug("Parsed session map (%d entries): %s", len(session_map), session_map)

    return tree, sessions, session_map


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


def render_tree_ascii(tree: dict, surface_to_session: dict[str, str]) -> None:
    """Print remote workspace structure as an ASCII tree diagram."""
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
                    print(f"{child_prefix}{sf_prefix}Surface {ref}{session_str}")

            print()


def _run_show_remote(args: argparse.Namespace) -> None:
    """Fetch remote state and print ASCII tree."""
    raw = fetch_remote_data(args.host, args.remote_path)
    tree, sessions, session_map = parse_remote_data(raw)
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
    render_tree_ascii(tree, surface_to_session)


def _run() -> None:
    args = parse_args()
    setup_logging()

    if args.command == "show":
        if args.show_command == "remote":
            log.debug("Args: command=show remote, host=%s, remote_path=%s", args.host, args.remote_path)
            _run_show_remote(args)
        elif args.show_command == "local":
            log.debug("Args: command=show local, socket=%s", args.socket)
            _run_show_local(args)
        return

    # command == "sync"
    log.debug("Args: host=%s, remote_path=%s, socket=%s", args.host, args.remote_path, args.socket)

    socket = resolve_socket(args.socket)

    raw = fetch_remote_data(args.host, args.remote_path)
    tree, sessions, session_map = parse_remote_data(raw)
    surface_to_session = map_sessions_to_surfaces(sessions, tree, session_map)
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
