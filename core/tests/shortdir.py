"""A temporary folder whose path is short enough for a unix-domain socket (the default temp folder on macOS is not)."""

import contextlib
import os
import shutil
import tempfile


@contextlib.contextmanager
def short_temp_dir():
    parent = "/tmp" if os.name == "posix" and os.path.isdir("/tmp") else None
    folder = tempfile.mkdtemp(prefix="gdv-", dir=parent)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)
