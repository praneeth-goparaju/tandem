#!/bin/bash
# Build the single-commit public tree from the committed files of this repository.
#
# Usage: make_public_tree.sh [--author "Name <email>"] <out_dir>
#
# docs/RELEASE.md describes what it checks and in which order.

set -euo pipefail

# An exported git variable would send the commands below to another repository,
# or put another name or date on the public commit.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR \
    GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL \
    GIT_AUTHOR_DATE GIT_COMMITTER_DATE

usage() {
    echo "usage: $0 [--author \"Name <email>\"] <out_dir>" >&2
    exit 2
}

AUTHOR=""
AUTHOR_GIVEN=0
if [ "$#" -ge 1 ] && [ "$1" = "--author" ]; then
    [ "$#" -ge 2 ] || usage
    AUTHOR="$2"
    AUTHOR_GIVEN=1
    shift 2
fi
if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    usage
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUT="$(python3 -I -B -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1")"
MAIN_CHECKOUT="$(cd "$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)/.." && pwd -P)"

lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# macOS volumes ignore case, so compare the paths without it.
for INSIDE in "$REPO" "$MAIN_CHECKOUT"; do
    case "$(lower "$OUT/")" in
        "$(lower "$INSIDE")"/*)
            echo "$1 is inside the source repository $INSIDE; give a folder outside it" >&2
            exit 2
            ;;
    esac
done
if [ -e "$OUT" ] && [ ! -d "$OUT" ]; then
    echo "$1 exists and is not a folder" >&2
    exit 2
fi
if [ -d "$OUT" ] && [ -n "$(ls -A "$OUT")" ]; then
    echo "$1 is not empty; give an empty or missing folder" >&2
    exit 2
fi

if [ "$AUTHOR_GIVEN" -eq 1 ]; then
    if [[ "$AUTHOR" =~ ^([^\<\>]*[^\<\>[:space:]])[[:space:]]+\<([^\<\>[:space:]]+@[^\<\>[:space:]]+)\>$ ]]; then
        NAME="${BASH_REMATCH[1]}"
        EMAIL="${BASH_REMATCH[2]}"
    else
        echo "--author must look like \"Name <email>\"" >&2
        exit 2
    fi
else
    NAME="$(git -C "$REPO" config user.name || true)"
    EMAIL="$(git -C "$REPO" config user.email || true)"
    if [ -z "$NAME" ] || [ -z "$EMAIL" ]; then
        echo "git has no user.name or user.email; set them, or pass --author \"Name <email>\"" >&2
        exit 2
    fi
fi

PRIVATE_TERMS=0
for TERMS_FILE in tools/pii_blocklist.txt user/private_terms.txt; do
    if [ ! -f "$REPO/$TERMS_FILE" ]; then
        echo "$TERMS_FILE is missing in $REPO; the private scan cannot run without it" >&2
        exit 2
    fi
    COUNT="$(grep -c -v -E '^[[:space:]]*(#|$)' "$REPO/$TERMS_FILE" || true)"
    if [ "$COUNT" -eq 0 ]; then
        echo "$TERMS_FILE has no terms in it; the private scan needs them" >&2
        exit 2
    fi
    PRIVATE_TERMS=$((PRIVATE_TERMS + COUNT))
done

# check_pii.py scans files only, so a list of lines goes into a scratch folder.
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
scan_lines() {
    rm -rf "$SCRATCH/scan"
    mkdir "$SCRATCH/scan"
    cat > "$SCRATCH/scan/$1"
    python3 -I -B "$REPO/tools/check_pii.py" --tree --private --root "$SCRATCH/scan" 2> "$SCRATCH/findings" \
        && return 0
    sed -n 's/^  /  /p' "$SCRATCH/findings" >&2
    return 1
}

if [ "$AUTHOR_GIVEN" -eq 0 ]; then
    if ! printf '%s <%s>\n' "$NAME" "$EMAIL" | scan_lines identity; then
        echo "The commit identity from git config (user.name, user.email) matches the" >&2
        echo "public rules or your private terms. Set a neutral identity for this" >&2
        echo "repository, or pass --author \"Name <email>\" to use one on purpose." >&2
        exit 2
    fi
fi

NAMES="$(git -C "$REPO" -c core.quotePath=false ls-tree -r -z --name-only --full-tree HEAD | tr '\0' '\n')"
if printf '%s\n' "$NAMES" | grep -q -E '^(\.venv|\.worktrees)/'; then
    echo "HEAD tracks files under .venv/ or .worktrees/; remove them from the commit first" >&2
    exit 2
fi
if ! printf '%s\n' "$NAMES" | scan_lines file-names; then
    echo "The file names of HEAD match the public rules or your private terms." >&2
    echo "Line N above is the Nth name of: git ls-tree -r --name-only HEAD" >&2
    echo "Rename the file in $REPO, commit, and run this script again. Nothing was extracted." >&2
    exit 1
fi

SOURCE_BRANCH="$(git -C "$REPO" symbolic-ref --short -q HEAD || echo "detached HEAD")"
SOURCE_SHA="$(git -C "$REPO" rev-parse --short HEAD)"
if [ -n "$(git -C "$REPO" --no-optional-locks status --porcelain)" ]; then
    echo "warning: $REPO has uncommitted changes; only the committed files of HEAD are published" >&2
fi

mkdir -p "$OUT"

git -C "$REPO" archive HEAD | tar -x -C "$OUT"

if ! python3 -I -B "$REPO/tools/check_pii.py" --tree --private --root "$OUT"; then
    echo "The scan did not pass. Nothing was committed." >&2
    echo "Fix the cause in $REPO, commit, and run this script again on an empty folder." >&2
    echo "The extracted files are still in $OUT for inspection." >&2
    exit 1
fi

git -C "$OUT" -c core.hooksPath=/dev/null init -q -b main
git -C "$OUT" -c core.hooksPath=/dev/null -c core.excludesFile=/dev/null add -A -f
TZ=UTC git -C "$OUT" -c core.hooksPath=/dev/null -c commit.gpgsign=false \
    -c "user.name=$NAME" -c "user.email=$EMAIL" commit -q -m "Initial public release"

if [ "$(git -C "$OUT" rev-parse 'HEAD^{tree}')" != "$(git -C "$REPO" rev-parse 'HEAD^{tree}')" ]; then
    echo "The built tree differs from HEAD of $REPO (for example an export-ignore rule)." >&2
    echo "Do not push it. The folder $OUT is left for inspection." >&2
    exit 1
fi

echo "Public tree ready in $OUT"
echo "Source: $SOURCE_BRANCH at $SOURCE_SHA"
echo "Private terms applied: $PRIVATE_TERMS"
echo "Commit: $(git -C "$OUT" rev-parse --short HEAD)"
echo "Author: $(git -C "$OUT" log -1 --format='%an <%ae>')"
echo "Date: $(TZ=UTC git -C "$OUT" log -1 --format='%ad' --date=iso)"
echo "Next: do a final read of the names (see docs/RELEASE.md), then create the"
echo "public GitHub repository and push main to it. This script does not push."
