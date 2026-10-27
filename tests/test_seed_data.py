import sqlite3
from contextlib import closing
from pathlib import Path

from src.database import connect
from src.seed_data import restore_seed


def _seed(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / "uploads").mkdir(parents=True)
    (seed / "app.db").write_bytes(b"seed db")
    (seed / "uploads" / "abc.pdf").write_bytes(b"%PDF")
    return seed


def test_empty_data_folder_gets_the_seed(tmp_path: Path):
    data = tmp_path / "data"
    assert restore_seed(data, _seed(tmp_path)) is True
    assert (data / "app.db").read_bytes() == b"seed db"
    assert (data / "uploads" / "abc.pdf").exists()


def test_database_with_no_reports_gets_the_seed(tmp_path: Path):
    """The hosted app created an empty database on a visit before the seed was pushed."""
    data = tmp_path / "data"
    connect(data / "app.db").close()
    (data / "app.db-journal").write_bytes(b"stale")
    assert restore_seed(data, _seed(tmp_path)) is True
    assert (data / "app.db").read_bytes() == b"seed db"
    assert not (data / "app.db-journal").exists()


def test_database_with_a_report_is_never_overwritten(tmp_path: Path):
    data = tmp_path / "data"
    connect(data / "app.db").close()
    with closing(sqlite3.connect(data / "app.db")) as conn:
        conn.execute("INSERT INTO documents (id, bank_name, reporting_period, original_filename, stored_path, "
                     "sha256, uploaded_at, processing_status) VALUES ('d1', 'Zemen Bank', '2025/26', 'z.pdf', "
                     "'uploads/z.pdf', 'abc', '2026-10-27T00:00:00+00:00', 'saved')")
        conn.commit()
    before = (data / "app.db").read_bytes()
    assert restore_seed(data, _seed(tmp_path)) is False
    assert (data / "app.db").read_bytes() == before
    assert not (data / "uploads").exists()


def test_unreadable_database_is_left_alone(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "app.db").write_bytes(b"not a database file at all, but someone's data" * 100)
    assert restore_seed(data, _seed(tmp_path)) is False


def test_no_seed_folder_does_nothing(tmp_path: Path):
    data = tmp_path / "data"
    assert restore_seed(data, tmp_path / "missing") is False
    assert not data.exists()
