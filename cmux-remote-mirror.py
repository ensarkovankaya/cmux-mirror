#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""
Remote makinedeki cmux workspace ve pane yapısını lokal cmux'te mirror eder.
Her lokal pane, remote'daki ilgili tmux session'ına SSH ile bağlanır.

Kullanım:
    uv run cmux-remote-mirror.py [ssh-host]

Varsayılan SSH host: home
"""

import json
import re
import subprocess
import sys
import time

SSH_HOST = sys.argv[1] if len(sys.argv) > 1 else "home"
REMOTE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def log(msg: str) -> None:
    print(f"[mirror] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[mirror] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def ssh(script: str, *, timeout: int = 60) -> str:
    r = run(["ssh", SSH_HOST, f'export PATH="{REMOTE_PATH}:$PATH"\n{script}'], timeout=timeout)
    return r.stdout


def cmux(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return run(["cmux", *args], check=check, timeout=10)


# ─── 1. Remote'dan veri topla (tek SSH bağlantısı) ──────────────────────────

log(f"Remote ({SSH_HOST}) cmux ve tmux bilgileri alınıyor...")

remote_script = r"""
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

try:
    remote_data = ssh(remote_script, timeout=120)
except Exception as e:
    err(f"SSH bağlantısı başarısız: {e}")


# ─── 2. Veriyi parse et ─────────────────────────────────────────────────────

def extract_section(data: str, start_marker: str, end_marker: str) -> str:
    lines = []
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


tree_json_raw = extract_section(remote_data, "___TREE_JSON_START___", "___TREE_JSON_END___")
tmux_sessions_raw = extract_section(remote_data, "___TMUX_SESSIONS_START___", "___TMUX_SESSIONS_END___")
screen_map_raw = extract_section(remote_data, "___SCREEN_MAP_START___", "___SCREEN_MAP_END___")

if not tree_json_raw:
    err("cmux tree JSON alınamadı")
if not tmux_sessions_raw:
    err("tmux session listesi alınamadı")

tree = json.loads(tree_json_raw)
sessions = [s.strip() for s in tmux_sessions_raw.strip().splitlines() if s.strip()]


# ─── 3. Surface -> tmux session eşleştirmesi ────────────────────────────────

surface_to_session: dict[str, str] = {}

for line in screen_map_raw.strip().splitlines():
    line = line.strip()
    if not line or "|" not in line:
        continue
    parts = line.split("|")
    if len(parts) < 3 or not parts[2]:
        continue
    ws_ref, sf_ref, prefix = parts[0], parts[1], parts[2]
    prefix_start = prefix.replace("cmux-", "")

    for ses in sessions:
        ses_start = ses.replace("cmux-", "")[: len(prefix_start)]
        if ses_start == prefix_start:
            surface_to_session[sf_ref] = ses
            sessions.remove(ses)
            break


# ─── 4. Workspace yapısını oluştur ──────────────────────────────────────────

workspaces: list[dict] = []

for window in tree.get("windows", []):
    for ws in window.get("workspaces", []):
        surfaces = []
        for pane in ws.get("panes", []):
            for surface in pane.get("surfaces", []):
                surfaces.append({
                    "pane_index": pane.get("index", 0),
                    "tmux_session": surface_to_session.get(surface["ref"], ""),
                })
        workspaces.append({"title": ws["title"], "surfaces": surfaces})

# İkinci geçiş: eşleşmemiş surface'ler için workspace bazlı eşleştirme
for ws_info in workspaces:
    unmatched = [s for s in ws_info["surfaces"] if not s["tmux_session"]]
    matched = [s for s in ws_info["surfaces"] if s["tmux_session"]]
    if not unmatched or not matched:
        continue
    ws_uuid = matched[0]["tmux_session"][len("cmux-") : len("cmux-") + 36]
    remaining = [s for s in sessions if s.startswith(f"cmux-{ws_uuid}-")]
    for i, surface in enumerate(unmatched):
        if i < len(remaining):
            surface["tmux_session"] = remaining[i]
            sessions.remove(remaining[i])


# ─── 5. Lokal cmux'te workspace ve pane'leri oluştur ────────────────────────

log("Lokal cmux'te workspace ve pane'ler oluşturuluyor...")

for ws_info in workspaces:
    title = ws_info["title"]
    log(f"Workspace: {title}")

    r = cmux("new-workspace")
    if r.returncode != 0:
        log(f"UYARI: workspace oluşturulamadı: {title}")
        continue
    time.sleep(0.5)

    cmux("rename-workspace", title)
    time.sleep(0.3)

    current_pane_index = -1
    first_surface = True

    for sf in ws_info["surfaces"]:
        pane_index = sf["pane_index"]
        tmux_session = sf["tmux_session"]

        if first_surface:
            first_surface = False
        else:
            if pane_index > current_pane_index:
                cmux("new-pane", "--direction", "right")
            else:
                cmux("new-surface")
            time.sleep(0.3)

        current_pane_index = max(current_pane_index, pane_index)

        if tmux_session:
            ssh_cmd = f"ssh -t {SSH_HOST} 'PATH=/opt/homebrew/bin:/usr/local/bin:$PATH tmux attach-session -t \"{tmux_session}\"'"
            log(f"  -> {tmux_session}")
            cmux("send", ssh_cmd)
            time.sleep(0.2)
            cmux("send-key", "Enter")
        else:
            log("  UYARI: tmux session bulunamadı, boş terminal")

log("Tamamlandı! Remote cmux yapısı lokale mirror edildi.")
