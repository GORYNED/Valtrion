"""Checks that a change does not carry anything private: the names of real projects, devices, machines and people, local paths, addresses.

The repository is open source, so a test, an example, a screenshot caption, a commit message or a pull request text must use made-up
names (`Game`, `Example Phone`, `build-pc`, `dev`). This script holds no private name itself. It finds private things in three ways:

1. Patterns that are never right in a public file: a user's folder (`C:\\Users\\<name>\\`, `/Users/<name>/`, `/home/<name>/`), a private network
   address, an e-mail address, an iOS device id with a real-looking part.
2. Names you list in a file that is NOT in the repository: `$VALTRION_PRIVATE_TERMS`, else `~/.valtrion/private-terms.txt` (one name per line, any
   case, `#` starts a comment). Keep real project, device and host names there.
3. Names listed as hashes in `tools/privacy-terms.sha256`: one `sha256` of a normalized name per line (lowercase letters and digits, the words
   of a name joined: `Example-Name` becomes `examplename`). The text is cut into words, every run of up to four words is joined and hashed, so
   a name written `Example Name`, `example-name` or `ExampleName` is found without the repository ever holding it. Make a line with
   `python tools/check_privacy.py --hash "Example Name"`. (A hash hides a name from a reader, not from someone who tries guesses.)

What is checked: the lines a branch adds (`--base`, default `origin/main`), its commit messages, its branch name, and any text you give with
`--text-file` (a pull request title and body). `--all` checks every tracked text file instead. Images and other binary files cannot be read:
they are listed, and a person must look at them (a screenshot needs its names masked). A line may end with `privacy: ok` to be skipped.

Exit code 0: nothing found. 1: something found (what and where, never the private name itself). 2: the check could not run.
Standard library only; Python 3.10 or newer.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, Sequence

ROOT = Path(__file__).resolve().parent.parent
HASH_FILE = ROOT / "tools" / "privacy-terms.sha256"
SKIP_MARK = "privacy: ok"
MAX_WORDS = 4
TEXT_LIMIT = 2_000_000

#: Folder names that are placeholders, not people.
PLACEHOLDER_USERS = {"dev", "user", "users", "you", "me", "name", "username", "example", "alice", "bob", "runner", "ubuntu", "public", "default",
                     "all", "shared", "<user>", "<name>", "%username%", "$user", "your-name", "yourname", "someone", "jdoe", "ci", "agent",
                     "builder", "tester", "test", "admin", "host", "home", "owner"}
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com", "anthropic.com", "localhost"}
#: Addresses that are made up on purpose: the documentation ranges and `10.0.x.x` (what tests and examples use).
DOCUMENTATION_NETWORKS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "10.0.0.0/16")]
PRIVATE_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")]  # privacy: ok

_WINDOWS_USER = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([\w.-]+)", re.IGNORECASE)
_POSIX_USER = re.compile(r"(?<![\w.-])/(?:Users|home)/([\w.-]+)", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_UDID = re.compile(r"\b([0-9A-F]{8})-([0-9A-F]{16})\b")
_WORD = re.compile(r"[A-Za-z0-9]+")


class Finding:
    def __init__(self, where: str, rule: str, detail: str = "") -> None:
        self.where, self.rule, self.detail = where, rule, detail

    def __str__(self) -> str:
        return f"{self.where}: {self.rule}" + (f" ({self.detail})" if self.detail else "")


def normalize(term: str) -> str:
    """The form a name is hashed in: lowercase letters and digits only, the words joined."""
    return "".join(_WORD.findall(term)).lower()


def hash_term(term: str) -> str:
    return hashlib.sha256(normalize(term).encode("utf-8")).hexdigest()


def load_hashes(path: Path = HASH_FILE) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.strip().lower().removeprefix("sha256:") for line in lines if line.strip() and not line.lstrip().startswith("#")}


def private_terms_path() -> Path:
    given = os.environ.get("VALTRION_PRIVATE_TERMS")
    return Path(given).expanduser() if given else Path.home() / ".valtrion" / "private-terms.txt"


def load_private_terms(path: Path | None = None) -> list[str]:
    try:
        lines = (path or private_terms_path()).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip().lower() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def check_text(text: str, where: str, hashes: set[str], terms: Sequence[str]) -> list[Finding]:
    """Findings in one piece of text (a line, a commit message). `where` says where it is from."""
    if SKIP_MARK in text:
        return []
    found: list[Finding] = []
    for pattern in (_WINDOWS_USER, _POSIX_USER):
        for match in pattern.finditer(text):
            name = match.group(1).lower()
            if len(name) > 2 and name not in PLACEHOLDER_USERS:  # one or two letters (`/home/x`) is a stand-in, not a person
                found.append(Finding(where, "user-folder", "a real-looking user folder name in a path"))
    for match in _EMAIL.finditer(text):
        domain = match.group(1).lower()
        local = text[match.start():match.end()].split("@", 1)[0].lower()
        if local == "git" or domain in SAFE_EMAIL_DOMAINS or domain.endswith((".example", ".invalid", ".test")):
            continue  # `git@github.com:owner/repo.git` is a remote, not a person
        found.append(Finding(where, "email", "an e-mail address"))
    for match in _IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        if any(address in network for network in PRIVATE_NETWORKS) and not any(address in network for network in DOCUMENTATION_NETWORKS):
            found.append(Finding(where, "private-address", "an address of a private network"))
    for match in _UDID.finditer(text):
        if sum(1 for char in match.group(2) if char != "0") > 4:
            found.append(Finding(where, "device-id", "an iOS device id that looks real"))
    lowered = text.lower()
    for term in terms:
        if term in lowered:
            found.append(Finding(where, "private-term", "a name from your private list"))
    if hashes:
        words = [word.lower() for word in _WORD.findall(text)]
        for size in range(1, MAX_WORDS + 1):
            for start in range(len(words) - size + 1):
                if hashlib.sha256("".join(words[start:start + size]).encode("utf-8")).hexdigest() in hashes:
                    found.append(Finding(where, "private-term", "a name from the hashed list"))
    unique: dict[tuple[str, str, str], Finding] = {}
    for finding in found:
        unique.setdefault((finding.where, finding.rule, finding.detail), finding)
    return list(unique.values())


# -- git ----------------------------------------------------------------------------------------------------------------------------


def git(*arguments: str) -> str:
    done = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments[:2])} failed: {done.stderr.strip()[:200]}")
    return done.stdout


def added_lines(base: str) -> Iterator[tuple[str, int, str]]:
    """(file, line number, text) of every line the branch adds to `base`."""
    path, number = "", 0
    for line in git("diff", "--unified=0", "--no-color", "--no-ext-diff", f"{base}...HEAD").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            number = int(match.group(1)) - 1 if match else 0
        elif line.startswith("+") and not line.startswith("+++"):
            number += 1
            if path:
                yield path, number, line[1:]


def binary_files(base: str) -> list[str]:
    changed = []
    for line in git("diff", "--numstat", "--no-color", f"{base}...HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "-" and parts[1] == "-":
            changed.append(parts[2])
    return changed


def tracked_text(limit: int = TEXT_LIMIT) -> Iterator[tuple[str, int, str]]:
    for name in git("ls-files").splitlines():
        path = ROOT / name
        try:
            if path.stat().st_size > limit:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            yield name, number, line


def run(base: str | None, everything: bool, text_files: Iterable[Path], hashes: set[str], terms: Sequence[str]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    binaries: list[str] = []
    if everything:
        lines = tracked_text()
    elif base:
        lines = added_lines(base)
        for number, message in enumerate(git("log", "--format=%B%x00", f"{base}..HEAD").split("\0"), 1):
            findings += check_text(message, f"commit message {number}", hashes, terms)
        findings += check_text(git("rev-parse", "--abbrev-ref", "HEAD").strip(), "branch name", hashes, terms)
        binaries = binary_files(base)
    else:
        lines = iter(())
    for name, number, line in lines:
        findings += check_text(line, f"{name}:{number}", hashes, terms)
    for path in text_files:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            findings += check_text(line, f"{path.name}:{number}", hashes, terms)
    return findings, binaries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that a change carries no private names, paths or addresses (see the module text).")
    parser.add_argument("--base", default="origin/main", help="check what this branch adds to BASE (default origin/main)")
    parser.add_argument("--all", action="store_true", help="check every tracked text file instead of the branch's changes")
    parser.add_argument("--text-file", type=Path, action="append", default=[], help="also check this text (a pull request title and body)")
    parser.add_argument("--hash", metavar="NAME", help="print the line for tools/privacy-terms.sha256 for NAME and exit")
    arguments = parser.parse_args(argv)
    if arguments.hash is not None:
        print("sha256:" + hash_term(arguments.hash))
        return 0
    hashes, terms = load_hashes(), load_private_terms()
    try:
        findings, binaries = run(None if arguments.all else arguments.base, arguments.all, arguments.text_file, hashes, terms)
    except (RuntimeError, OSError) as error:
        print(f"The privacy check could not run: {error}", file=sys.stderr)
        return 2
    sources = f"{len(hashes)} hashed name(s)" + (f", {len(terms)} name(s) of your private list" if terms else
                                                  f", no private list ({private_terms_path()} does not exist: only the patterns and the hashes were used)")
    for finding in findings:
        print(finding)
    for name in binaries:
        print(f"{name}: binary file, look at it yourself (names, paths and devices must be masked)")
    print(f"privacy check: {len(findings)} finding(s); {sources}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
