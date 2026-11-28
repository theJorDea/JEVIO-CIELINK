"""Persistent offline demo assignments, not provider coverage data."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from cielink.domain import Address


class DemoAddressStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path or os.getenv('CIELINK_DEMO_ADDRESS_DB') or
                         Path(__file__).resolve().parents[1] / 'local-data' / 'demo-addresses.sqlite3')

    def resolve(self, parts: dict[str, str]) -> Address:
        key = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = sha256(key.casefold().replace('ё', 'е').encode('utf-8')).hexdigest()
        house_id = 'demo-' + digest
        status = 'unavailable' if int(digest, 16) % 5 == 0 else 'available'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS demo_addresses ('
                         'house_id TEXT PRIMARY KEY, address_json TEXT NOT NULL, '
                         "coverage TEXT NOT NULL CHECK(coverage IN ('available', 'unavailable')))")
            conn.execute('INSERT INTO demo_addresses VALUES (?, ?, ?) ON CONFLICT(house_id) DO NOTHING',
                         (house_id, key, status))
        return Address(house_id=house_id, **parts)

    def status(self, house_id: str) -> str:
        if not self.path.is_file():
            return 'unknown'
        try:
            with closing(sqlite3.connect(self.path)) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='demo_addresses'").fetchone():
                    return 'unknown'
                row = conn.execute('SELECT coverage FROM demo_addresses WHERE house_id=?', (house_id,)).fetchone()
        except sqlite3.Error:
            return 'error'
        return row[0] if row else 'unknown'
