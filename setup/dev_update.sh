#!/usr/bin/env bash
# =============================================================
# CERAVIS developer update — pull the code, restart only what changed
# =============================================================
# For a DEVELOPMENT CLONE (the bench, ~/ceravis, on bench_ceravis1.1 — the only
# branch it updates) when you only want to try new code. It never touches the fleet side: no enrollment, no edge_id hand-over,
# no keys, no systemd units, no sudoers, no apt/pip, no AI engine builds, and
# it never restarts the fleet agent (the agent restarts ITSELF when its own
# code changes, keeping its identity — the edge_id cannot change).
#
#   bash setup/dev_update.sh            pull, then restart what the change needs
#   bash setup/dev_update.sh --dry-run  say what it would do, change nothing
#
# What a change needs, by the files it touches:
#   edge/static/**          nothing — pages are served from disk: reload the page
#   edge/fleet/**           nothing here — the agent restarts itself on new code
#   other edge/ code        restart ceravis (the app; ~30 s of no live view)
#   units, setup/, deps,    NOT applied here — named, and left for the full
#   models, env files       `bash setup/upgrade.sh`
# An installed RELEASE (client1, prodtest) is updated with setup/update.sh or a
# patch (setup/patch_device.sh), never with this.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

[ -d "$REPO/.git" ] || { echo "Not a development clone ($REPO): use setup/update.sh on a release." >&2; exit 2; }
cd "$REPO"

# The bench runs bench_ceravis1.1 and nothing else (CLAUDE.md, "Branches").
BRANCH=bench_ceravis1.1
ON="$(git rev-parse --abbrev-ref HEAD)"
if [ "$ON" != "$BRANCH" ]; then
    echo "This clone is on '$ON', not $BRANCH — nothing done. The bench runs $BRANCH only:" >&2
    echo "    git switch $BRANCH && bash setup/dev_update.sh" >&2
    exit 2
fi
FROM="$(git rev-parse HEAD)"
git fetch -q origin "$BRANCH"
TO="$(git rev-parse "origin/$BRANCH")"
if [ "$FROM" = "$TO" ]; then
    echo "Already up to date on $BRANCH ($(git log --oneline -1 HEAD))."
    exit 0
fi
git merge-base --is-ancestor "$FROM" "$TO" \
    || { echo "origin/$BRANCH is not ahead of this copy (local commits?) — nothing done." >&2; exit 2; }

CHANGED="$(git diff --name-only "$FROM" "$TO")"
echo "== $BRANCH: $(git rev-parse --short "$FROM") -> $(git rev-parse --short "$TO") =="
git log --oneline "$FROM..$TO" | sed 's/^/   /'

# Sort the changed files by what they need.
RESTART_APP=0; AGENT=0; PAGES=0; FULL=()
while IFS= read -r f; do
    case "$f" in
        edge/static/*)                                        PAGES=1 ;;
        edge/infra/*|setup/*|cloud/*|edge/requirements*.txt|edge/models/*|*.service|*.timer|*.path)
                                                              FULL+=("$f") ;;
        edge/fleet/*)                                         AGENT=1 ;;
        edge/*.py|edge/*/*)                                   RESTART_APP=1 ;;
    esac
done <<< "$CHANGED"

if [ "${#FULL[@]}" -gt 0 ]; then
    echo
    echo "NOTE: these need the full upgrade (units, setup, dependencies, models) and are"
    echo "      NOT applied by this script — run 'bash setup/upgrade.sh' for them:"
    printf '        %s\n' "${FULL[@]}"
fi
if [ "$DRY" = 1 ]; then
    echo
    echo "(dry run) would pull; restart ceravis: $([ "$RESTART_APP" = 1 ] && echo yes || echo no)"
    exit 0
fi

git pull -q --ff-only origin "$BRANCH"
find edge -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo
if [ "$RESTART_APP" = 1 ]; then
    echo "Restarting the app (ceravis)..."
    sudo systemctl restart ceravis
    HEALTH=""
    for _ in $(seq 1 90); do
        HEALTH="$(curl -fsS -m 2 http://127.0.0.1:8000/health 2>/dev/null || true)"
        [ -n "$HEALTH" ] && break
        sleep 1
    done
    if [ -n "$HEALTH" ]; then
        echo "  the app answers again: $HEALTH"
    else
        echo "  WARNING: the app is not answering yet — journalctl -u ceravis -n 50"
    fi
else
    echo "The app keeps running: no change in its Python code."
fi
[ "$AGENT" = 1 ] && echo "The fleet agent restarts itself on its new code (its identity and edge_id are kept)."
[ "$PAGES" = 1 ] && echo "Pages changed: reload them in the browser (Ctrl+Shift+R)."
echo "Done. Nothing on the fleet side was touched."
