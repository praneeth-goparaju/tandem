from __future__ import annotations

import os
import resource
import sys
import threading
import time
from typing import BinaryIO, TextIO

from src.brain.media.svg import EXIT_MEMORY, EXIT_REFUSED, SvgError, output_size

MEMORY_LIMIT = 1 << 30
_POLL_SECONDS = 0.02
_RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def _exit_when_too_large() -> None:
    while True:
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT > MEMORY_LIMIT:
            os._exit(EXIT_MEMORY)
        time.sleep(_POLL_SECONDS)


def limit_memory() -> None:
    """Cap the process at `MEMORY_LIMIT`: its address space where the OS lets us, else its resident size.

    macOS refuses `RLIMIT_AS`, so there a thread ends the process once its
    resident size passes the limit.
    """
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    except (ValueError, OSError):
        threading.Thread(target=_exit_when_too_large, name="memory-limit", daemon=True).start()


def _fetch(url: str, resource_type: str) -> bytes:
    if url.startswith("data:"):
        from cairosvg.url import fetch

        return fetch(url, resource_type)
    raise SvgError("the SVG refers to something outside itself")


def render(text: str) -> bytes:
    width, height = output_size(text)
    from cairosvg.surface import PNGSurface

    return PNGSurface.convert(
        bytestring=text.encode("utf-8"), url_fetcher=_fetch, output_width=width, output_height=height,
    )


def main(
    stdin: BinaryIO | None = None, stdout: BinaryIO | None = None, stderr: TextIO | None = None,
) -> int:
    limit_memory()
    text = (stdin or sys.stdin.buffer).read().decode("utf-8")
    try:
        png = render(text)
    except SvgError as exc:
        (stderr or sys.stderr).write(str(exc))
        return EXIT_REFUSED
    except MemoryError:
        return EXIT_MEMORY
    sink = stdout or sys.stdout.buffer
    sink.write(png)
    sink.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
