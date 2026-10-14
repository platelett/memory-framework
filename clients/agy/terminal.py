from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator, TextIO

from memory_framework.errors import MemoryFrameworkError


class LaunchCancelled(MemoryFrameworkError):
    """The user ended startup before launching the next model process."""


@contextmanager
def terminal_streams() -> Iterator[tuple[TextIO, TextIO]]:
    """Use interactive stdin, or keep redirected prompt input untouched.

    A tty cannot be opened with Python's buffered update mode (r+): that
    mode requires seeking. Open separate read and write streams instead.
    """
    if sys.stdin.isatty():
        output = sys.stdout if sys.stdout.isatty() and not sys.stderr.isatty() else sys.stderr
        yield sys.stdin, output
        return
    with open("/dev/tty", "r", encoding="utf-8", buffering=1) as input_stream:
        with open("/dev/tty", "w", encoding="utf-8", buffering=1) as output_stream:
            yield input_stream, output_stream
