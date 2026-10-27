"""Starting data for a hosted copy (pilot, D-087).

A free host wipes the data folder on every restart. When the folder holds no saved report
yet, the reports reviewed beforehand are copied in from `seed/` in the repository, so testers
always start from the same reviewed reports. A database with a saved or deleted report is
never touched.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def restore_seed(data_dir: Path, seed_dir: Path = SEED_DIR) -> bool:
    """Copy seed_dir into data_dir when data_dir has no saved report; True if copied."""
    db_path = data_dir / "app.db"
    if not (seed_dir / "app.db").is_file() or _has_reports(db_path):
        return False
    for suffix in ("-journal", "-wal", "-shm"):  # left by the empty database; would not match the seed
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    shutil.copytree(seed_dir, data_dir, dirs_exist_ok=True)
    return True


def _has_reports(db_path: Path) -> bool:
    """An empty database (e.g. created by a visit before the seed existed) has no reports."""
    if not db_path.exists():
        return False
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return any(conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]
                       for table in ("documents", "report_deletions") if table in tables)
    except sqlite3.DatabaseError:  # unreadable: leave it alone rather than overwrite it
        return True
