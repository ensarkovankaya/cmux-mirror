"""Mirror remote cmux workspace and pane structure to local cmux.

Each local pane connects to the corresponding remote tmux session via SSH.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("cmux-mirror")

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
    return parser.parse_args()


def run_command(
    cmd: list[str], *, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, check=check, timeout=timeout
    )


def ssh_exec(
    host: str, script: str, *, remote_path: str, timeout: int = 60
) -> str:
    r = run_command(
        ["ssh", host, f'export PATH="{remote_path}:$PATH"\n{script}'],
        timeout=timeout,
    )
    return r.stdout


def cmux_cmd(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return run_command(["cmux", *args], check=check, timeout=10)


def fetch_remote_data(host: str, remote_path: str) -> str:
    log.info("Fetching cmux and tmux info from remote (%s)...", host)
    try:
        return ssh_exec(host, REMOTE_SCRIPT, remote_path=remote_path, timeout=120)
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
    return "\n".join(lines)


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
    return tree, sessions, screen_map_raw


def map_surfaces_to_sessions(
    sessions: list[str], screen_map_raw: str
) -> tuple[dict[str, str], list[str]]:
    surface_to_session: dict[str, str] = {}
    consumed: set[str] = set()

    for line in screen_map_raw.strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3 or not parts[2]:
            continue
        sf_ref, prefix = parts[1], parts[2]
        prefix_start = prefix.replace("cmux-", "")

        for ses in sessions:
            if ses in consumed:
                continue
            ses_start = ses.replace("cmux-", "")[: len(prefix_start)]
            if ses_start == prefix_start:
                surface_to_session[sf_ref] = ses
                consumed.add(ses)
                break

    remaining = [s for s in sessions if s not in consumed]
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
                    surfaces.append(
                        SurfaceInfo(
                            pane_index=pane.get("index", 0),
                            tmux_session=surface_to_session.get(surface["ref"], ""),
                        )
                    )
            workspaces.append(WorkspaceInfo(title=ws["title"], surfaces=surfaces))

    # Second pass: match unmatched surfaces by workspace UUID
    remaining = list(sessions)
    for ws_info in workspaces:
        unmatched = [s for s in ws_info.surfaces if not s.tmux_session]
        matched = [s for s in ws_info.surfaces if s.tmux_session]
        if not unmatched or not matched:
            continue
        ws_uuid = matched[0].tmux_session[len("cmux-") : len("cmux-") + 36]
        candidates = [s for s in remaining if s.startswith(f"cmux-{ws_uuid}-")]
        for i, surface in enumerate(unmatched):
            if i < len(candidates):
                surface.tmux_session = candidates[i]
                remaining.remove(candidates[i])

    return workspaces


def create_local_workspaces(host: str, workspaces: list[WorkspaceInfo]) -> None:
    log.info("Creating local cmux workspaces and panes...")

    for ws_info in workspaces:
        log.info("Workspace: %s", ws_info.title)

        r = cmux_cmd("new-workspace")
        if r.returncode != 0:
            log.warning("Could not create workspace: %s", ws_info.title)
            continue
        time.sleep(0.5)

        cmux_cmd("rename-workspace", ws_info.title)
        time.sleep(0.3)

        current_pane_index = -1
        first_surface = True

        for sf in ws_info.surfaces:
            if first_surface:
                first_surface = False
            else:
                if sf.pane_index > current_pane_index:
                    cmux_cmd("new-pane", "--direction", "right")
                else:
                    cmux_cmd("new-surface")
                time.sleep(0.3)

            current_pane_index = max(current_pane_index, sf.pane_index)

            if sf.tmux_session:
                ssh_cmd = (
                    f"ssh -t {host} "
                    f"'PATH=/opt/homebrew/bin:/usr/local/bin:$PATH "
                    f'tmux attach-session -t "{sf.tmux_session}"\''
                )
                log.info("  -> %s", sf.tmux_session)
                cmux_cmd("send", ssh_cmd)
                time.sleep(0.2)
                cmux_cmd("send-key", "Enter")
            else:
                log.warning("  No tmux session found, leaving empty terminal")


def main() -> None:
    logging.basicConfig(
        format="[mirror] %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )

    args = parse_args()

    raw = fetch_remote_data(args.host, args.remote_path)
    tree, sessions, screen_map_raw = parse_remote_data(raw)
    surface_to_session, remaining_sessions = map_surfaces_to_sessions(
        sessions, screen_map_raw
    )
    workspaces = build_workspaces(tree, remaining_sessions, surface_to_session)
    create_local_workspaces(args.host, workspaces)

    log.info("Done! Remote cmux structure mirrored locally.")


if __name__ == "__main__":
    try:
        main()
    except MirrorError as e:
        log.error("%s", e)
        sys.exit(1)
