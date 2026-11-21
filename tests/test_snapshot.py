"""Frozen CTIS snapshots (sources/snapshot.py): a transfer recorded once replays as the same records and
documents, through the same adapter and stages, with nothing fetched; a snapshot is checked against
its lock, and an archive can't write outside its folder."""

import io
import json
import tarfile

import pytest
from test_evidence import CT, PAGES, PageTexts, payload
from test_sources_ctis import FakeCtis

from trialwatch.db import core
from trialwatch.sources import ctis, snapshot, stages
from trialwatch.sources.ctis_client import TrialNotFound

UNKNOWN = "1999-000000-00-77"


class KnowsOneTrial(FakeCtis):
    """CTIS knowing CT only, as CTIS answers an unknown number."""

    def retrieve(self, ct_number):
        if ct_number != CT:
            raise TrialNotFound(ct_number)
        return super().retrieve(ct_number)


def transfer(conn, adapter, keys):
    source = core.registered_source(conn, "ctis")
    return stages.transfer(conn, source, adapter, keys, extract=PageTexts(PAGES))


def recorded(conn, tmp_path):
    """CT transferred from (fake) CTIS while recorded, packed; returns the archive."""
    core.act_as(conn, "test-person")
    folder = tmp_path / "recording"
    recording = snapshot.RecordingClient(KnowsOneTrial(payload(CT)), folder, "test", dataset="test/v1")
    result = transfer(conn, ctis.CtisAdapter(recording), [CT, UNKNOWN])
    assert (result.statuses[CT], result.statuses[UNKNOWN]) == ("complete", "failed")
    recording.finish()
    return snapshot.pack(folder, tmp_path / "snap.tar.gz")


def stored(conn):
    rows = conn.execute(
        "SELECT r.content_hash, d.file_sha256 FROM core.trial_record_current r "
        "JOIN core.trial_document l ON l.trial_id = r.trial_id JOIN core.document_current d ON d.id = l.document_id "
        "ORDER BY 1, 2").fetchall()
    return rows


def test_a_replayed_transfer_stores_what_the_recorded_one_did(conn, tmp_path):
    packed = recorded(conn, tmp_path)
    assert (packed.trials, packed.files) == (2, 1)
    before = stored(conn)
    replay = snapshot.ReplayClient(snapshot.unpack(packed.path, tmp_path / "unpacked", packed.sha256))
    # a fresh trial table is not needed: the same answers give the same record and document again
    result = transfer(conn, ctis.CtisAdapter(replay), [CT, UNKNOWN])
    assert (result.statuses[CT], result.statuses[UNKNOWN]) == ("complete", "failed")
    assert stored(conn) == before
    with pytest.raises(snapshot.NotInSnapshot):
        replay.retrieve("1999-000000-00-55")
    with pytest.raises(TrialNotFound):
        replay.retrieve(UNKNOWN)


def test_packing_gives_the_same_bytes_for_the_same_recording(conn, tmp_path):
    packed = recorded(conn, tmp_path)
    again = snapshot.pack(tmp_path / "recording", tmp_path / "again.tar.gz")
    assert again.sha256 == packed.sha256


def test_a_snapshot_that_is_not_the_locked_one_is_refused(conn, tmp_path):
    packed = recorded(conn, tmp_path)
    with pytest.raises(ValueError, match="the lock says"):
        snapshot.unpack(packed.path, tmp_path / "x", "0" * 64)
    # a damaged file inside is caught when it is read
    folder = snapshot.unpack(packed.path, tmp_path / "y")
    index = json.loads((folder / snapshot.INDEX).read_text())
    sha = index["trials"][CT]["documents"].popitem()[1]["sha256"]
    (folder / "files" / sha).write_bytes(b"%PDF changed")
    replay = snapshot.ReplayClient(folder)
    uuid = next(iter(replay.index["trials"][CT]["documents"]))
    with pytest.raises(ValueError, match="damaged"):
        replay.download_document(CT, uuid)


def test_an_archive_cannot_write_outside_its_folder(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as packed:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 2
        packed.addfile(info, io.BytesIO(b"hi"))
    with pytest.raises(tarfile.TarError):
        snapshot.unpack(archive, tmp_path / "into")
    assert not (tmp_path / "outside.txt").exists()


def test_the_committed_lock_names_the_bruckner_dataset():
    lock = snapshot.read_lock()
    assert lock.dataset == "bruckner/2026-03-16"


def test_a_recording_takes_an_earlier_copy_of_the_same_version_and_asks_ctis_otherwise(conn, tmp_path):
    core.act_as(conn, "test-person")
    copies = tmp_path / "documents"
    entry = payload(CT)["documents"][0]
    # the copy `source transfer --files-dir` wrote for this document and version, with older bytes
    copy = ctis.file_path(copies, CT, entry)
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"%PDF-1.7 the copy of 13 November")
    live = KnowsOneTrial(payload(CT))
    recording = snapshot.RecordingClient(snapshot.LocalCopyFirst(live, copies, "2026-11-13"), tmp_path / "rec", "test")
    transfer(conn, ctis.CtisAdapter(recording), [CT])
    index = recording.finish()
    document = index["trials"][CT]["documents"][entry["uuid"]]
    assert document["from"] == "copy of 2026-11-13" and live.downloads == []
    folder = tmp_path / "rec"
    assert (folder / "files" / document["sha256"]).read_bytes() == b"%PDF-1.7 the copy of 13 November"

    # a version the copy does not hold comes from CTIS
    newer = payload(CT)
    newer["documents"][0]["systemVersion"] = "2"
    live = KnowsOneTrial(newer)
    recording = snapshot.RecordingClient(snapshot.LocalCopyFirst(live, copies, "2026-11-13"), tmp_path / "rec2", "test")
    transfer(conn, ctis.CtisAdapter(recording), [CT])
    assert recording.finish()["trials"][CT]["documents"][entry["uuid"]]["from"] == "CTIS"
    assert len(live.downloads) == 1
