from pathlib import Path

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


def test_existing_database_is_never_overwritten(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "app.db").write_bytes(b"live db")
    assert restore_seed(data, _seed(tmp_path)) is False
    assert (data / "app.db").read_bytes() == b"live db"
    assert not (data / "uploads").exists()


def test_no_seed_folder_does_nothing(tmp_path: Path):
    data = tmp_path / "data"
    assert restore_seed(data, tmp_path / "missing") is False
    assert not data.exists()
