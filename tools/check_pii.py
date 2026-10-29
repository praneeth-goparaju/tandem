#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCKLIST = REPO_ROOT / "tools" / "pii_blocklist.txt"
PUBLIC_TERMS = REPO_ROOT / "tools" / "personal_terms_public.txt"
PRIVATE_TERMS_NAME = Path("user") / "private_terms.txt"

HOME_PATH_RE = re.compile(r"(?<![\w/])/(?:Users|home)/(?!__HOME__)([A-Za-z0-9._-]+)")
PLACEHOLDER_USERNAMES = frozenset({"me", "you", "alice", "bob", "example", "user", "test"})
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
ALLOWED_EMAILS = frozenset({"you@example.com", "noreply@anthropic.com"})
RESERVED_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example",
    "test",
    "invalid",
    "localhost",
)
# An address at a mail or code host passes only with a placeholder local part
# (an optional +tag allowed): such an address names the provider, which
# provider-detection tests need, and never a real account.
PLACEHOLDER_LOCAL_PARTS = frozenset(
    {"you", "me", "alice", "bob", "example", "user", "test", "someone", "noreply", "no-reply"}
)
SERVICE_DOMAINS = (
    "icloud.com",
    "me.com",
    "mac.com",
    "gmail.com",
    "privaterelay.appleid.com",
    "github.com",
    "anthropic.com",
)
SKIPPED_PREFIXES = ("user/", ".venv/", ".superpowers/")
OVERLAY_PREFIX = "user/"
NUL_PROBE_BYTES = 8192

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from src.brain.atomic_io import read_regular_text  # noqa: E402


def load_blocklist(*, required: bool = True) -> list[str]:
    try:
        info = BLOCKLIST.lstat()
        raw_text = read_regular_text(BLOCKLIST)
    except FileNotFoundError:
        if not required:
            return []
        print(
            f"check-pii: blocklist {BLOCKLIST} not found. Run "
            f"`python3 tools/gen_pii_blocklist.py` to generate it from "
            f"user/profile.yaml. Refusing commit to be safe.",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError:
        print(
            "check-pii: blocklist is not a safe regular file; refusing commit.",
            file=sys.stderr,
        )
        sys.exit(1)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        print(
            "check-pii: blocklist must be owned by this user with mode 0600; "
            "refusing commit.",
            file=sys.stderr,
        )
        sys.exit(1)
    tokens: list[str] = []
    for raw in raw_text.splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            tokens.append(s)
    if not tokens and required:
        print(
            "check-pii: blocklist is empty; regenerate it before committing.",
            file=sys.stderr,
        )
        sys.exit(1)
    return tokens


def load_terms_file(path: Path) -> list[str]:
    """Read one term per line; blank lines and lines starting with # are skipped."""
    terms: list[str] = []
    for raw in read_regular_text(path).splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            terms.append(s)
    return terms


def _on_domain(domain: str, allowed: tuple[str, ...]) -> bool:
    return any(domain == name or domain.endswith(f".{name}") for name in allowed)


def _email_allowed(match: re.Match[str]) -> bool:
    address = match.group(0).lower()
    domain = match.group(1).lower()
    if address in ALLOWED_EMAILS or _on_domain(domain, RESERVED_DOMAINS):
        return True
    local = address.rpartition("@")[0].partition("+")[0]
    return local in PLACEHOLDER_LOCAL_PARTS and _on_domain(domain, SERVICE_DOMAINS)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def scan_text(text: str, *, terms: list[str], path: str) -> list[str]:
    """Return one "<path>:<line>: <rule>" per hit."""
    folded_terms = [_fold(t) for t in terms if t]
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in HOME_PATH_RE.finditer(line):
            if match.group(1).lower() not in PLACEHOLDER_USERNAMES:
                findings.append(f"{path}:{lineno}: absolute-home-path")
        for match in EMAIL_RE.finditer(line):
            if not _email_allowed(match):
                findings.append(f"{path}:{lineno}: email")
        folded = _fold(line)
        if any(term in folded for term in folded_terms):
            findings.append(f"{path}:{lineno}: private-term")
    return findings


def _git_ls_files(repo_root: Path) -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=repo_root, text=True)
    return [name for name in out.split("\0") if name]


def _walked_files(root: Path) -> list[str]:
    names: list[str] = []
    for folder, subfolders, filenames in os.walk(root):
        subfolders[:] = sorted(d for d in subfolders if d != ".git")
        base = Path(folder).relative_to(root)
        names += [(base / f).as_posix() for f in sorted(filenames)]
    return names


def _listed_files(root: Path, *, walk: bool) -> list[str]:
    return _walked_files(root) if walk else _git_ls_files(root)


def tracked_private_paths(repo_root: Path, *, walk: bool = False) -> list[str]:
    """Paths under user/, the private overlay, which must never be tracked."""
    return [n for n in _listed_files(repo_root, walk=walk) if n.startswith(OVERLAY_PREFIX)]


def tracked_files(repo_root: Path, *, walk: bool = False) -> list[Path]:
    """The text files to scan: the tracked ones, or with ``walk`` every file under ``repo_root``."""
    files: list[Path] = []
    for name in _listed_files(repo_root, walk=walk):
        if name.startswith(SKIPPED_PREFIXES):
            continue
        path = repo_root / name
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            if b"\0" in handle.read(NUL_PROBE_BYTES):
                continue
        files.append(path)
    return files


def scan_tree(*, private: bool, root: Path | None = None) -> int:
    """Scan the tracked tree of this repository, or with ``root`` every file under that folder."""
    walk = root is not None
    scan_root = root if root is not None else REPO_ROOT
    public_terms = scan_root / "tools" / "personal_terms_public.txt"
    terms = load_terms_file(public_terms if walk and public_terms.is_file() else PUBLIC_TERMS)
    if private:
        terms += load_blocklist(required=False)
        private_terms = REPO_ROOT / PRIVATE_TERMS_NAME
        if private_terms.is_file():
            terms += load_terms_file(private_terms)
        elif not BLOCKLIST.exists():
            print(
                "check-pii: no private term file (tools/pii_blocklist.txt or "
                f"{PRIVATE_TERMS_NAME}); only the public rules were used",
                file=sys.stderr,
            )
    if walk:
        private_paths = tracked_private_paths(scan_root, walk=True)
        files = tracked_files(scan_root, walk=True)
    else:
        private_paths = tracked_private_paths(scan_root)
        files = tracked_files(scan_root)
    findings = [f"{name}:0: tracked-private-overlay" for name in private_paths]
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        findings += scan_text(
            text, terms=terms, path=path.relative_to(scan_root).as_posix()
        )
    if not findings:
        return 0
    print("check-pii: personal values found in the tracked tree:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    return 1


def staged_added_lines() -> list[tuple[str, int, str]]:
    out = subprocess.check_output(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        cwd=REPO_ROOT,
        text=True,
    )
    results: list[tuple[str, int, str]] = []
    current_path: str | None = None
    new_lineno = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[len("+++ b/"):]
            continue
        if line.startswith("+++ "):
            current_path = None
            continue
        if line.startswith("@@"):
            m = hunk_re.match(line)
            if m:
                new_lineno = int(m.group(1))
            continue
        if current_path is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            results.append((current_path, new_lineno, content))
            new_lineno += 1
        elif not line.startswith("-"):
            new_lineno += 1
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for personal values.")
    parser.add_argument(
        "--tree", action="store_true", help="scan every tracked file, not the staged diff"
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="with --tree, also use the private blocklist and user/private_terms.txt",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="with --tree, scan every file in this folder (no .git needed) "
        "instead of the tracked files of this repository",
    )
    args = parser.parse_args([] if argv is None else argv)
    if args.private and not args.tree:
        parser.error("--private only works with --tree")
    if args.root is not None and not args.tree:
        parser.error("--root only works with --tree")
    if args.root is not None:
        args.root = Path(os.path.realpath(args.root))
        if not args.root.is_dir():
            parser.error(f"--root {args.root} is not a folder")
    if args.tree:
        return scan_tree(private=args.private, root=args.root)
    blocklist = load_blocklist()
    pattern = re.compile(
        "|".join(re.escape(t) for t in blocklist),
        re.IGNORECASE,
    )
    hits: list[tuple[str, int]] = []
    for path, lineno, content in staged_added_lines():
        if pattern.search(content):
            hits.append((path, lineno))
    if not hits:
        return 0
    print("check-pii: BLOCKED — staged diff contains PII tokens:", file=sys.stderr)
    for path, lineno in hits[:20]:
        print(f"  {path}:{lineno}: matched a blocklisted value", file=sys.stderr)
    if len(hits) > 20:
        print(f"  …and {len(hits) - 20} more", file=sys.stderr)
    print(
        "\nFix the staged content or, if the match is a false positive, "
        "edit tools/pii_blocklist.txt to remove the offending token.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
