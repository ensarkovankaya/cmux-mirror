#!/usr/bin/env bash
# Auto-start tmux inside cmux terminals.
# Source this from your shell profile (e.g. ~/.zshrc) on the remote machine:
#   source /path/to/cmux.sh

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

  # Persist surface ref mapping for cmux-mirror
  mkdir -p ~/.cmux-sessions
  SURFACE_REF=$(cmux tree --all --json 2>/dev/null | python3 -c "
import sys, json
try:
    tree = json.load(sys.stdin)
    for w in tree.get('windows', []):
        for ws in w.get('workspaces', []):
            for pane in ws.get('panes', []):
                for sf in pane.get('surfaces', []):
                    if sf.get('here'):
                        print(sf['ref']); sys.exit(0)
except Exception:
    pass
" 2>/dev/null)
  if [ -n "$SURFACE_REF" ]; then
    echo "$SESSION_NAME" > ~/.cmux-sessions/"$SURFACE_REF"
  fi

  tmux new-session -A -s "$SESSION_NAME"
  # Clean up mapping on exit
  [ -n "$SURFACE_REF" ] && rm -f ~/.cmux-sessions/"$SURFACE_REF"
  cmux close-surface 2>/dev/null || cmux close-pane 2>/dev/null || exit
fi
