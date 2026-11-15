"""Read a LinkedIn data export as it arrives: the ZIP file, or the folder it unpacks to.

Files are read by name into memory; nothing is extracted to disk, so archive paths can
never point outside a folder. LinkedIn sends two variants (a basic archive within minutes,
a complete one within a day); both keep the CSV files at the top level. A folder that holds
one unpacked export folder also works.
"""

import zipfile
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024 * 1024  # far above any real export file; refuses archive bombs


class ExportError(ValueError):
    """The path is not a readable LinkedIn data export."""


class Export:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.label = self.path.name.removesuffix(".zip").removesuffix(".zip")
        if self.path.is_dir():
            found = (p for pattern in ("*.csv", "*/*.csv", "*/*/*.csv") for p in self.path.glob(pattern))
            self._zip, self._files = None, _index(p.relative_to(self.path).as_posix() for p in found)
        elif zipfile.is_zipfile(self.path):
            self._zip = zipfile.ZipFile(self.path)
            self._files = _index(i.filename for i in self._zip.infolist() if not i.is_dir())
        elif self.path.exists():
            raise ExportError(f"{self.path} is neither a ZIP file nor a folder.")
        else:
            raise FileNotFoundError(f"{self.path} does not exist.")
        if "connections.csv" not in self._files:
            raise ExportError(f"{self.path} has no Connections.csv, so it is not a LinkedIn data export.")

    def text(self, name: str) -> str | None:
        """Contents of an export file by its name (case-insensitive), or None if absent."""
        member = self._files.get(name.lower())
        if member is None:
            return None
        if self._zip is None:
            data = (self.path / member).read_bytes()
        else:
            if self._zip.getinfo(member).file_size > MAX_FILE_BYTES:
                raise ExportError(f"{member} is larger than {MAX_FILE_BYTES >> 20} MB.")
            data = self._zip.read(member)
        return data.decode("utf-8-sig")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()

    def __enter__(self) -> "Export":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _index(paths) -> dict[str, str]:
    """{lowercase file name: path}, keeping the shallowest copy of each name."""
    index: dict[str, str] = {}
    for path in sorted(paths, key=lambda p: (p.count("/"), p)):
        index.setdefault(path.rsplit("/", 1)[-1].lower(), path)
    return index
