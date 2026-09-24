#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SKIP_PARTS = {".git", ".venv", ".ruff_cache", "__pycache__", "build", "dist"}
INHERITED_NON_NOREPLY_COMMITS = {"e501358aa14a0c102303691a91a6a64d23d641c1"}
TEXT_SUFFIXES = {
    "", ".py", ".md", ".toml", ".yaml", ".yml", ".json", ".sh", ".html",
    ".in", ".js", ".cjs", ".txt",
}
PATTERNS = {
    "absolute Linux user home": re.compile(
        "/ho" + r"me/(?!user\b|example\b)[A-Za-z0-9._-]+"
    ),
    "absolute macOS user home": re.compile(
        "/Us" + r"ers/(?!user\b|example\b)[A-Za-z0-9._-]+"
    ),
    "private Tailnet hostname": re.compile(
        r"[A-Za-z0-9.-]+\.tail[a-z0-9]+\.t" + r"s\.net", re.I
    ),
    "Tailnet IPv4 address": re.compile(
        r"\b100\.(?:[6-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b"
    ),
    "private signup domain": re.compile(r"\broq" + r"\.io\b", re.I),
    "hosting-provider VPS hostname": re.compile(r"\b[a-z0-9.-]+\.hstgr\.cloud\b", re.I),
    "common API token": re.compile(
        r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"
    ),
    "PEM private key": re.compile("BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY"),
}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    extra_file = root / "qa" / "leak-patterns.local"
    extra = (
        [line.strip() for line in extra_file.read_text().splitlines() if line.strip()]
        if extra_file.exists()
        else []
    )
    findings: list[str] = []
    if (root / ".git").exists():
        history = subprocess.run(
            ["git", "-C", str(root), "log", "--all", "--format=%H%x09%ae"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        for line in history:
            commit, _, email = line.partition("\t")
            if (
                email
                and not email.lower().endswith("@users.noreply.github.com")
                and commit not in INHERITED_NON_NOREPLY_COMMITS
            ):
                findings.append(f"git commit {commit[:12]}: non-noreply author email")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(
            part in SKIP_PARTS for part in path.relative_to(root).parts
        ):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            findings.append(f"unknown binary/text format: {path.relative_to(root)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"non-UTF8 file: {path.relative_to(root)}")
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(root)}: {label}")
        for needle in extra:
            if needle in text:
                findings.append(f"{path.relative_to(root)}: site-local denylist match")
    if findings:
        print("LEAK_LINT_FAILED")
        print("\n".join(findings))
        return 1
    print("LEAK_LINT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
