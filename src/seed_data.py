"""Starting data for a hosted copy (pilot, D-087).

A free host wipes the data folder on every restart. When the folder holds no database yet,
the reports reviewed beforehand are copied in from `seed/` in the repository, so testers
always start from the same reviewed reports. A data folder that already has a database is
never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def restore_seed(data_dir: Path, seed_dir: Path = SEED_DIR) -> bool:
    """Copy seed_dir into data_dir when data_dir has no app.db; True if copied."""
    if (data_dir / "app.db").exists() or not (seed_dir / "app.db").is_file():
        return False
    shutil.copytree(seed_dir, data_dir, dirs_exist_ok=True)
    return True
