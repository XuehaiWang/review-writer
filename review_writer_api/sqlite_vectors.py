"""Float32 cosine adapter. No authorization, provider calls or mutable snapshots."""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import struct
import time
from typing import Iterable

EXTENSION_VERSION = "v0.1.9"


@dataclass(frozen=True)
class VectorRow:
    chunk_row_id: str
    paper_id: str
    index_id: str
    content_sha256: str
    ordinal: int
    values: list[float]


def vector_blob(values, dimension: int) -> bytes:
    numbers = [float(x) for x in values]
    if len(numbers) != dimension or not all(math.isfinite(x) for x in numbers):
        raise ValueError("Vector dimension or finite-value validation failed.")
    if not any(numbers):
        raise ValueError("A zero vector has no cosine direction.")
    return struct.pack(f"<{dimension}f", *numbers)


class SQLiteVectors:
    def __init__(self, extension: Path | str = ""):
        self.extension = Path(extension).resolve() if extension else None

    @contextmanager
    def connect(self, path: Path, dimension: int, *, readonly=False):
        if dimension < 1 or dimension > 32768:
            raise ValueError("Invalid vector dimension.")
        target = path.resolve().as_uri() + "?mode=ro&immutable=1" if readonly else str(path)
        with closing(sqlite3.connect(target, uri=readonly)) as db:
            db.enable_load_extension(True)
            try:
                if self.extension:
                    db.load_extension(str(self.extension))
                else:
                    import sqlite_vec
                    sqlite_vec.load(db)
            finally:
                db.enable_load_extension(False)
            if db.execute("SELECT vec_version()").fetchone()[0] != EXTENSION_VERSION:
                raise RuntimeError("Unsupported sqlite-vec extension version.")
            if not readonly:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS vectors (
                        id INTEGER PRIMARY KEY, chunk_row_id TEXT NOT NULL UNIQUE,
                        paper_id TEXT NOT NULL, index_id TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        embedding BLOB NOT NULL);
                    CREATE INDEX IF NOT EXISTS vectors_index ON vectors(index_id);
                """)
            if not readonly:
                db.commit()
            yield db

    def build(self, path: Path, *, user_id: str, model: str, dimension: int,
              rows: Iterable[VectorRow], base: Path | None = None,
              replace_indexes: tuple[str, ...] = (), valid_indexes: tuple[str, ...] = ()) -> int:
        """Copy by Backup API, then apply a document delta in a private file."""
        if path.exists():
            raise FileExistsError(path)
        if base is not None:
            with self.connect(base, dimension, readonly=True) as source, closing(sqlite3.connect(path)) as dest:
                self.validate_metadata(source, user_id, model, dimension)
                source.backup(dest)
        with self.connect(path, dimension) as db:
            db.execute("DELETE FROM metadata")
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps({
                "schema": 1, "user_id": user_id, "model": model, "dimension": dimension,
                "distance": "COSINE", "type": "FLOAT32", "normalization": "none",
            }, sort_keys=True),))
            db.execute("DELETE FROM vectors WHERE index_id IN (SELECT value FROM json_each(?))",
                       (json.dumps(replace_indexes),))
            db.execute("DELETE FROM vectors WHERE index_id NOT IN (SELECT value FROM json_each(?))",
                       (json.dumps(valid_indexes),))
            db.executemany("""INSERT INTO vectors
                (chunk_row_id,paper_id,index_id,content_sha256,ordinal,embedding)
                VALUES (?,?,?,?,?,?) ON CONFLICT(chunk_row_id) DO UPDATE SET
                paper_id=excluded.paper_id,index_id=excluded.index_id,
                content_sha256=excluded.content_sha256,ordinal=excluded.ordinal,embedding=excluded.embedding""",
                ((r.chunk_row_id, r.paper_id, r.index_id, r.content_sha256, r.ordinal,
                  vector_blob(r.values, dimension)) for r in rows))
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Vector snapshot integrity check failed.")
            return db.execute("SELECT count(*) FROM vectors").fetchone()[0]

    @staticmethod
    def validate_metadata(db, user_id, model, dimension):
        meta = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
        expected = {"schema": 1, "user_id": user_id, "model": model, "dimension": dimension,
                    "distance": "COSINE", "type": "FLOAT32", "normalization": "none"}
        if meta != expected:
            raise ValueError("Vector snapshot identity or configuration mismatch.")

    def rank(self, path: Path, *, user_id: str, model: str, dimension: int,
             query: list[float], allowed_chunks: dict[str, str], limit: int,
             min_similarity: float, per_paper_limit: int = 0):
        """Authorized CURRENT chunk IDs + hashes filter before final Top-K.

        Reading TEMP tables is unnecessary: json_each keeps the snapshot truly
        read-only. Results are balanced by paper and chunk order.
        """
        if not allowed_chunks:
            return []
        with self.connect(path, dimension, readonly=True) as db:
            self.validate_metadata(db, user_id, model, dimension)
            deadline = time.monotonic() + 120
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = db.execute("""
                WITH distances AS (
                    SELECT v.chunk_row_id, v.paper_id, v.ordinal,
                           vec_distance_cosine(v.embedding, ?) AS distance
                    FROM vectors v
                    JOIN json_each(?) a ON a.key=v.chunk_row_id AND a.value=v.content_sha256
                    WHERE distance <= ?
                ), ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY paper_id ORDER BY distance,ordinal,chunk_row_id
                    ) AS paper_rank FROM distances
                )
                SELECT chunk_row_id, 1-distance FROM ranked
                WHERE ?=0 OR paper_rank<=?
                ORDER BY CASE WHEN ?>0 THEN paper_rank ELSE 0 END,
                         distance, CASE WHEN ?=0 THEN ordinal ELSE 0 END, chunk_row_id
                LIMIT ?
            """, (vector_blob(query, dimension), json.dumps(allowed_chunks), 1-min_similarity,
                  per_paper_limit, per_paper_limit, per_paper_limit, per_paper_limit, limit)).fetchall()
            return [(key, float(score)) for key, score in rows]
