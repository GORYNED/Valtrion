"""A stand-in for git-lfs's clean filter in tests: content in, its Git LFS pointer out (a pointer passes through unchanged).

`--wrong` writes a pointer with a different oid, as a clean filter that does not give back the committed pointer would."""

import hashlib
import sys

VERSION = b"version https://git-lfs.github.com/spec/v1\n"


def main() -> int:
    data = sys.stdin.buffer.read()
    if data.startswith(VERSION) and len(data) <= 1024:
        sys.stdout.buffer.write(data)
        return 0
    oid = hashlib.sha256(data + (b"x" if "--wrong" in sys.argv else b"")).hexdigest()
    sys.stdout.buffer.write(VERSION + f"oid sha256:{oid}\nsize {len(data)}\n".encode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
