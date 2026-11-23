"""A frozen snapshot of what a source answered for a set of trials, so a gold evaluation reads the same
records and documents on every run (docs/sources-and-gold.md, "Gold evaluations").

Recording wraps a source's adapter: every record and document file a transfer fetches is also written
to a folder. Replaying wraps an adapter the same way and serves those answers in place of its source,
so a transfer from the snapshot runs exactly as one from the source (the record read, documents,
page text) and only the network is left out. A trial the source did not know is replayed as unknown;
anything the recording never asked for is an error, never a silent gap. Both work for any adapter.

The folder holds snapshot.json (what it is, when it was made, and an index of every answer), one
records/<key>.json per trial, and files/<sha256> per document file. It travels as a .tar.gz.
"""

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trialwatch.sources.registry import Adapter, DocumentRef, DownloadedFile, SourceTrial, TrialNotFound

INDEX = "snapshot.json"


class NotInSnapshot(LookupError):
    """The replay was asked for something the recording never fetched."""


class _Wrapped(Adapter):
    """An adapter that reads its records as `adapter` does; only fetching and downloading differ."""

    transfers = True

    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.name = adapter.name
        self.document_kinds = adapter.document_kinds

    def config_problems(self, config: dict) -> list[str]:
        return self.adapter.config_problems(config)

    def evidence_kinds(self, config: dict) -> list[str] | None:
        return self.adapter.evidence_kinds(config)

    def key_problem(self, key: str) -> str | None:
        return self.adapter.key_problem(key)

    def parse(self, payload: dict, listing: dict | None) -> SourceTrial:
        return self.adapter.parse(payload, listing)


class Recording(_Wrapped):
    """`adapter`, also writing each answer it gives into `folder`."""

    def __init__(self, adapter: Adapter, folder: Path, description: str, dataset: str | None = None) -> None:
        super().__init__(adapter)
        self.folder = folder
        (folder / "records").mkdir(parents=True, exist_ok=True)
        (folder / "files").mkdir(parents=True, exist_ok=True)
        self.index = {"description": description, "dataset": dataset, "made_at": None, "trials": {}}

    def _trial(self, key: str) -> dict:
        if key not in self.index["trials"]:
            self.index["trials"][key] = {"found": None, "documents": {}}
        return self.index["trials"][key]

    def fetch(self, key: str) -> dict:
        try:
            payload = self.adapter.fetch(key)
        except TrialNotFound:
            self._trial(key)["found"] = False
            raise
        record = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        (self.folder / "records" / f"{key}.json").write_text(record)
        self._trial(key)["found"] = True
        return payload

    def download(self, key: str, document: DocumentRef) -> DownloadedFile:
        file = self.adapter.download(key, document)
        sha256 = hashlib.sha256(file.content).hexdigest()
        (self.folder / "files" / sha256).write_bytes(file.content)
        self._trial(key)["documents"][document.key] = {"sha256": sha256, "media_type": file.media_type,
                                                       "file_name": file.file_name, "from": self.adapter.name}
        return file

    def finish(self) -> dict:
        """Write the index; returns it."""
        self.index["made_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        (self.folder / INDEX).write_text(json.dumps(self.index, indent=1, ensure_ascii=False, sort_keys=True))
        return self.index


class Replay(_Wrapped):
    """`adapter` answering as its source did when the snapshot in `folder` was recorded."""

    def __init__(self, adapter: Adapter, folder: Path) -> None:
        super().__init__(adapter)
        self.folder = folder
        self.index = json.loads((folder / INDEX).read_text())

    def _trial(self, key: str) -> dict:
        trial = self.index["trials"].get(key)
        if trial is None:
            raise NotInSnapshot(f"{key} is not in the snapshot")
        return trial

    def fetch(self, key: str) -> dict:
        if not self._trial(key)["found"]:
            raise TrialNotFound(key)
        return json.loads((self.folder / "records" / f"{key}.json").read_text())

    def download(self, key: str, document: DocumentRef) -> DownloadedFile:
        recorded = self._trial(key)["documents"].get(document.key)
        if recorded is None:
            raise NotInSnapshot(f"document {document.key} of {key} is not in the snapshot")
        content = (self.folder / "files" / recorded["sha256"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != recorded["sha256"]:
            raise ValueError(f"the snapshot's file for document {document.key} of {key} is damaged")
        return DownloadedFile(content=content, media_type=recorded["media_type"], file_name=recorded["file_name"])


@dataclass(frozen=True)
class Packed:
    path: Path
    sha256: str
    trials: int
    files: int


def pack(folder: Path, out: Path) -> Packed:
    """The folder as a .tar.gz, the same bytes for the same contents (sorted names, no times or owners)."""
    names = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as archive:
        for name in names:
            data = (folder / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    out.write_bytes(buffer.getvalue())
    index = json.loads((folder / INDEX).read_text())
    files = sum(1 for name in names if name.startswith("files/"))
    return Packed(out, hashlib.sha256(out.read_bytes()).hexdigest(), len(index["trials"]), files)


def unpack(archive: Path, into: Path, sha256: str | None = None) -> Path:
    """Unpack a snapshot .tar.gz into `into`, refusing it when its sha256 is not `sha256` (when given)
    or when it would write outside `into`; returns `into`."""
    if sha256 is not None:
        found = hashlib.sha256(archive.read_bytes()).hexdigest()
        if found != sha256:
            raise ValueError(f"{archive} has sha256 {found}, the lock says {sha256}")
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:gz") as packed:
        # the "data" filter refuses absolute paths, "..", links and special files
        packed.extractall(into, filter="data")
    if not (into / INDEX).is_file():
        raise ValueError(f"{archive} is not a snapshot: no {INDEX}")
    return into


# --- the lock: which snapshot the gold pipelines read ----------------------------------------------

LOCK_FILE = Path(__file__).parent.parent.parent.parent / "evals" / "snapshot.lock.json"


@dataclass(frozen=True)
class Lock:
    dataset: str
    # the GitHub release holding the archive, and the archive's name and sha256; None until one is made
    release: str | None
    file: str | None
    sha256: str | None
    made_at: str | None
    trials: int | None


def read_lock(path: Path = LOCK_FILE) -> Lock:
    data = json.loads(path.read_text())
    return Lock(data["dataset"], data.get("release"), data.get("file"), data.get("sha256"), data.get("made_at"),
                data.get("trials"))


def lock_data(dataset: str, release: str, packed: Packed, made_at: str) -> dict:
    return {"description": "The frozen CTIS snapshot the gold pipelines read (made by the Gold snapshot "
                           "workflow; commit this file to use it)",
            "dataset": dataset, "release": release, "file": packed.path.name, "sha256": packed.sha256,
            "made_at": made_at, "trials": packed.trials}
