#!/usr/bin/env bash
# =============================================================
# Promote the development branch to the production branch — the one way:
#
#     bash setup/promote_to_prod.sh            (merge + commit, locally)
#     bash setup/promote_to_prod.sh --push     (and push both branches)
#
# In a temporary worktree (this checkout is not touched): merge DEV into PROD,
# then remove every development-only path — the export-ignore list of
# .gitattributes (tests, docs, CI, test pages, dev scripts, cloud-server
# files), except the release tooling production keeps (make_release.sh,
# send_to_device.sh, setup/usb/). A path the merge added or changed there is
# removed too, so nothing dev-only can ride along. Stops on a real conflict.
#
# DEV / PROD: dev_ceravis1.1 / prod_ceravis1.1 (override with DEV=… PROD=…).
# =============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="${DEV:-dev_ceravis1.1}"; PROD="${PROD:-prod_ceravis1.1}"
PUSH=0
[ "${1:-}" = "--push" ] && PUSH=1
KEEP='^(setup/make_release\.sh|setup/send_to_device\.sh|setup/usb/)'

cd "$REPO"
# A branch checked out in another worktree (another session's) is never moved
# under it — its files would no longer match its branch. Said plainly.
BUSY="$(git worktree list --porcelain | awk -v b="branch refs/heads/$PROD" '/^worktree /{w=substr($0, 10)} $0 == b {print w}')"
if [ -n "$BUSY" ]; then
    echo "$PROD is checked out in another worktree: $BUSY" >&2
    echo "  (another session may be using it) — promote once it is free; meanwhile" >&2
    echo "  a release of what $PROD has now:  bash setup/make_release.sh --from $PROD" >&2
    exit 1
fi
DEV_ONLY=()
while read -r path _; do
    path="${path#/}"
    [[ "$path" =~ $KEEP ]] || DEV_ONLY+=("${path%/}")
done < <(grep 'export-ignore' .gitattributes | tr -d '\r')

WT="$(mktemp -d)"; rmdir "$WT"
trap 'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; git -C "$REPO" worktree prune' EXIT
git worktree add -q "$WT" "$PROD"
cd "$WT"
git merge --no-edit --no-ff "$DEV" >/dev/null 2>&1 || true
# Conflicts on dev-only paths are expected (production deleted them); any
# other conflict is real and stops here.
REAL=()
while read -r status path; do
    keep=1
    for d in "${DEV_ONLY[@]}"; do [[ "$path" == "$d" || "$path" == "$d"/* ]] && keep=0; done
    [ "$keep" = 1 ] && REAL+=("$status $path")
done < <(git status --porcelain | grep -E '^(UU|AA|DU|UD|AU|UA|DD) ' || true)
if [ ${#REAL[@]} -gt 0 ]; then
    printf 'real conflict, resolve by hand:\n  %s\n' "${REAL[@]}" >&2
    git merge --abort 2>/dev/null || true
    exit 1
fi
for d in "${DEV_ONLY[@]}"; do
    git rm -rq --cached --ignore-unmatch -- "$d" >/dev/null
    rm -rf -- "$d"
done
if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
    git commit -q --no-edit
elif ! git diff --cached --quiet; then
    git commit -q -m "prod: development-only files stay out"
fi
LEFT="$(git ls-files | grep -cE "^($(IFS='|'; echo "${DEV_ONLY[*]}" | sed 's/\./\\./g'))(/|$)" || true)"
[ "$LEFT" = 0 ] || { echo "dev-only files still in $PROD: $LEFT" >&2; exit 1; }
echo "$PROD @ $(git rev-parse --short HEAD): $DEV merged, no development-only files"
cd "$REPO"
[ "$PUSH" = 1 ] && git push origin "$DEV" "$PROD"
exit 0
