"""A frozen snapshot of what CTIS answered for a set of trials, so a gold evaluation reads the same
records and documents on every run (README "Gold evaluations").

Recording wraps the real CTIS client: every record and document file a transfer fetches is also
written to a folder. Replaying serves those answers to the CTIS adapter in place of the client, so a
transfer from the snapshot runs exactly as one from CTIS (record, documents, page text extraction)
and only the network is left out. A trial CTIS did not know is replayed as unknown; anything the
recording never asked for is an error, never a silent gap.

A recording can take document files from an earlier copy on disk instead of downloading them
(LocalCopyFirst): the file data/ctis/documents/<CT>/… that `source transfer --files-dir` wrote for the
same document and version. Records are always CTIS's answer at recording time; each document in the
index says where its file came from.

The folder holds snapshot.json (what it is, when it was made, and an index of every answer), one
records/<EU CT number>.json per trial, and files/<sha256> per document file. It travels as a .tar.gz.
"""

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trialwatch.sources.ctis_client import DownloadedFile, TrialNotFound

INDEX = "snapshot.json"


class NotInSnapshot(LookupError):
    """The replay was asked for something the recording never fetched."""


def _empty_index(description: str) -> dict:
    return {"description": description, "dataset": None, "made_at": None, "trials": {}}


class RecordingClient:
    """The CTIS client, also writing each answer it gives into `folder`."""

    def __init__(self, client, folder: Path, description: str, dataset: str | None = None) -> None:
        self.client = client
        self.folder = folder
        (folder / "records").mkdir(parents=True, exist_ok=True)
        (folder / "files").mkdir(parents=True, exist_ok=True)
        self.index = _empty_index(description)
        self.index["dataset"] = dataset

    def _trial(self, ct_number: str) -> dict:
        return self.index["trials"].setdefault(ct_number, {"found": None, "documents": {}})

    def retrieve(self, ct_number: str) -> dict:
        try:
            payload = self.client.retrieve(ct_number)
        except TrialNotFound:
            self._trial(ct_number)["found"] = False
            raise
        (self.folder / "records" / f"{ct_number}.json").write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self._trial(ct_number)["found"] = True
        return payload

    def download_document(self, ct_number: str, uuid: str) -> DownloadedFile:
        file = self.client.download_document(ct_number, uuid)
        sha256 = hashlib.sha256(file.content).hexdigest()
        (self.folder / "files" / sha256).write_bytes(file.content)
        origin = "CTIS"
        if hasattr(self.client, "origin"):
            origin = self.client.origin(ct_number, uuid)
        self._trial(ct_number)["documents"][uuid] = {"sha256": sha256, "media_type": file.media_type,
                                                     "file_name": file.file_name, "from": origin}
        return file

    def search(self, criteria: dict, page: int, size: int, direction: str = "ASC") -> dict:
        raise NotInSnapshot("a snapshot records trials by number, not CTIS's search")

    def finish(self) -> dict:
        """Write the index; returns it."""
        self.index["made_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        (self.folder / INDEX).write_text(json.dumps(self.index, indent=1, ensure_ascii=False, sort_keys=True))
        return self.index


class LocalCopyFirst:
    """The CTIS client, but a document's file comes from `copies` (a --files-dir folder) when it holds
    that very document and version: <CT>/<type>_<title>_<first 8 of the uuid>_v<version>.<ext>
    (ctis.file_path). Anything else is asked of CTIS. `copied_on` names the copy in the index."""

    def __init__(self, client, copies: Path, copied_on: str) -> None:
        self.client = client
        self.copies = copies
        self.copied_on = copied_on
        # (CT number, uuid) -> (document type, version), from the record retrieved just before
        self.listed: dict[tuple[str, str], tuple[str, str]] = {}
        self.origins: dict[tuple[str, str], str] = {}

    def retrieve(self, ct_number: str) -> dict:
        payload = self.client.retrieve(ct_number)
        for entry in payload.get("documents") or []:
            if isinstance(entry, dict) and entry.get("uuid"):
                self.listed[(ct_number, str(entry["uuid"]))] = (str(entry.get("documentType")),
                                                                str(entry.get("systemVersion")))
        return payload

    def _copy(self, ct_number: str, uuid: str) -> Path | None:
        listed = self.listed.get((ct_number, uuid))
        folder = self.copies / ct_number
        if listed is None or not folder.is_dir():
            return None
        document_type, version = listed
        found = []
        for path in folder.iterdir():
            name = path.name
            if name.startswith(f"{document_type}_") and f"_{uuid[:8]}_v{version}." in name:
                found.append(path)
        if len(found) != 1:
            return None
        return found[0]

    def download_document(self, ct_number: str, uuid: str) -> DownloadedFile:
        copy = self._copy(ct_number, uuid)
        if copy is None:
            self.origins[(ct_number, uuid)] = "CTIS"
            return self.client.download_document(ct_number, uuid)
        content = copy.read_bytes()
        media_type = None
        if content.startswith(b"%PDF"):
            media_type = "application/pdf"
        self.origins[(ct_number, uuid)] = f"copy of {self.copied_on}"
        return DownloadedFile(content=content, media_type=media_type, file_name=copy.name)

    def origin(self, ct_number: str, uuid: str) -> str:
        return self.origins.get((ct_number, uuid), "CTIS")

    def search(self, criteria: dict, page: int, size: int, direction: str = "ASC") -> dict:
        return self.client.search(criteria, page, size, direction)


class ReplayClient:
    """Answers as CTIS did when the snapshot in `folder` was recorded."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.index = json.loads((folder / INDEX).read_text())

    def _trial(self, ct_number: str) -> dict:
        trial = self.index["trials"].get(ct_number)
        if trial is None:
            raise NotInSnapshot(f"{ct_number} is not in the snapshot")
        return trial

    def retrieve(self, ct_number: str) -> dict:
        if not self._trial(ct_number)["found"]:
            raise TrialNotFound(ct_number)
        return json.loads((self.folder / "records" / f"{ct_number}.json").read_text())

    def download_document(self, ct_number: str, uuid: str) -> DownloadedFile:
        document = self._trial(ct_number)["documents"].get(uuid)
        if document is None:
            raise NotInSnapshot(f"document {uuid} of {ct_number} is not in the snapshot")
        content = (self.folder / "files" / document["sha256"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != document["sha256"]:
            raise ValueError(f"the snapshot's file for document {uuid} of {ct_number} is damaged")
        return DownloadedFile(content=content, media_type=document["media_type"], file_name=document["file_name"])

    def search(self, criteria: dict, page: int, size: int, direction: str = "ASC") -> dict:
        raise NotInSnapshot("a snapshot records trials by number, not CTIS's search")


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
