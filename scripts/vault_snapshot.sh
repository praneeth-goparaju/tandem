#!/bin/bash
# Nightly snapshot of the Obsidian vault into a separate git directory.
#
# Usage: vault_snapshot.sh <vault_path> <git_dir>
#
# The vault itself is never touched and gets no .git folder. All history lives
# in <git_dir>, which git is pointed at with --git-dir and --work-tree.
# Prints the new commit id, or "no changes" when nothing changed since the
# last snapshot. Any git failure stops the script with a non-zero exit code.

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <vault_path> <git_dir>" >&2
    exit 2
fi

VAULT_PATH="$1"
GIT_DIR="$2"

if [ ! -d "$VAULT_PATH" ]; then
    echo "vault path not found: $VAULT_PATH" >&2
    exit 1
fi

# Notes the snapshot should skip: Obsidian window layout files and the trash.
EXCLUDES=(
    ".obsidian/workspace*.json"
    ".trash/"
)

# Git runs with a fixed identity so it works where no git user is configured.
vault_git() {
    git -c user.name=vault-snapshot -c user.email=vault-snapshot@localhost \
        --git-dir="$GIT_DIR" --work-tree="$VAULT_PATH" "$@"
}

# First run: create the git directory.
if [ ! -d "$GIT_DIR" ]; then
    git init --bare --quiet "$GIT_DIR"
fi

# Add any missing exclusion line. Lines already in the file are kept as they are.
EXCLUDE_FILE="$GIT_DIR/info/exclude"
mkdir -p "$GIT_DIR/info"
touch "$EXCLUDE_FILE"
if [ -s "$EXCLUDE_FILE" ] && [ -n "$(tail -c 1 "$EXCLUDE_FILE")" ]; then
    echo >> "$EXCLUDE_FILE"
fi
for pattern in "${EXCLUDES[@]}"; do
    if ! grep -qxF -- "$pattern" "$EXCLUDE_FILE"; then
        echo "$pattern" >> "$EXCLUDE_FILE"
    fi
done

vault_git add -A

# Plain assignment, so set -e stops the script if git status fails.
changes="$(vault_git status --porcelain)"
if [ -z "$changes" ]; then
    echo "no changes"
    exit 0
fi

vault_git commit --quiet -m "vault snapshot $(date '+%Y-%m-%d %H:%M')"
vault_git rev-parse HEAD
