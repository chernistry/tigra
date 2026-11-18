"""Render the model as one offline HTML file (no network requests, data embedded)."""

import json
import os
import stat
import tempfile
from pathlib import Path

TEMPLATE = Path(__file__).with_name("viz.html")
PLACEHOLDER = "__GRAPH_DATA__"


def render_html(model: dict) -> str:
    # Escape <, > and & so names or notes can never close the <script> element.
    data = (
        json.dumps(model, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, data, 1)


def write_private(path: Path, text: str) -> None:
    """Write a file readable by the owner only: the outputs hold third-party data.

    The text goes to a 0600 temp file that replaces the target, so an existing
    world-readable file never holds the new data. On POSIX the output directory
    must have mode 0700 before writing.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise PermissionError(f"Choose a new output folder or an existing directory with mode 0700: {directory}")

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
