"""Disposable, hash-bound SQLite search cache of one live source publication."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from core.catalog_json import decimal_text
from decimal import Decimal
from hashlib import file_digest
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from src.matching.catalog import CatalogError, CatalogProvider
from core.matching.contracts import NORMALIZER_VERSION, Candidate

INDEX_SCHEMA: Final = "matching-index/1"
_CANDIDATE: Final = TypeAdapter(Candidate)


@dataclass(frozen=True, slots=True)
class IndexManifest:
    path: Path
    sha256: str
    snapshot_id: str
    snapshot_sha256: str
    source_normalizer_version: str
    matching_normalizer_version: str
    generation: str
    row_count: int
    fts_trigram: bool
    environment: str
    schema: str = INDEX_SCHEMA

    @property
    def descriptor_path(self) -> Path:
        return self.path.parent / (self.snapshot_id + ".json")


def _metadata(manifest: IndexManifest) -> dict[str, str | int | bool]:
    return {
        key: value
        for key, value in asdict(manifest).items()
        if key not in ("path", "sha256")
    }


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def decimal_key(value: Decimal | None) -> str | None:
    return None if value is None else decimal_text(value)


def build_index(
    catalog: CatalogProvider, directory: Path, *, enable_fts: bool = True
) -> IndexManifest:
    """Stream source rows to a private file, then atomically expose a descriptor."""
    catalog.require_open()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    generation = uuid4().hex
    temporary = directory / (generation + ".building")
    final = directory / (generation + ".sqlite")
    descriptor_temp = directory / (generation + ".json.building")
    published = False
    try:
        with closing(sqlite3.connect(temporary)) as conn:
            conn.executescript("""PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE matching_items(listing_key TEXT PRIMARY KEY,goods_id TEXT,goods_verified INTEGER NOT NULL,
 category_id TEXT,price TEXT,price_kind TEXT,currency TEXT,tax_basis TEXT,unit TEXT,pack_quantity TEXT,
 seller_id TEXT,name TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX matching_goods ON matching_items(goods_id);
CREATE INDEX matching_category ON matching_items(category_id);
CREATE INDEX matching_price_unit ON matching_items(price,unit);
CREATE INDEX matching_seller ON matching_items(seller_id);""")
            fts = False
            if enable_fts:
                try:
                    conn.execute(
                        "CREATE VIRTUAL TABLE names_fts USING fts5(listing_key UNINDEXED,name,tokenize='trigram')"
                    )
                    fts = True
                except sqlite3.OperationalError as exc:
                    if "no such module" not in str(
                        exc
                    ) and "no such tokenizer" not in str(exc):
                        raise
            count = 0
            with conn:
                for candidate in catalog.candidates():
                    if (
                        candidate.snapshot_id != catalog.manifest.snapshot_id
                        or candidate.environment != catalog.manifest.environment
                    ):
                        raise CatalogError(
                            "candidate", "mixed source generations/environment"
                        )
                    price = candidate.price
                    conn.execute(
                        "INSERT INTO matching_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            candidate.listing_key,
                            candidate.goods_id,
                            int(candidate.goods_id_verified),
                            candidate.category_id,
                            decimal_key(price.amount),
                            price.kind,
                            price.currency,
                            price.tax_basis,
                            price.unit,
                            decimal_key(price.pack_quantity),
                            candidate.seller_id,
                            candidate.name_raw,
                            _CANDIDATE.dump_json(candidate).decode(),
                        ),
                    )
                    if fts:
                        conn.execute(
                            "INSERT INTO names_fts VALUES (?,?)",
                            (candidate.listing_key, candidate.name_raw),
                        )
                    count += 1
                provisional = IndexManifest(
                    final,
                    "",
                    catalog.manifest.snapshot_id,
                    catalog.manifest.snapshot_sha256,
                    catalog.manifest.source_normalizer_version,
                    NORMALIZER_VERSION,
                    generation,
                    count,
                    fts,
                    catalog.manifest.environment,
                )
                conn.executemany(
                    "INSERT INTO metadata VALUES (?,?)",
                    (
                        (key, json.dumps(value))
                        for key, value in _metadata(provisional).items()
                    ),
                )
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CatalogError("index", "integrity failure")
        catalog.require_open()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        manifest = IndexManifest(
            final,
            _digest(final),
            provisional.snapshot_id,
            provisional.snapshot_sha256,
            provisional.source_normalizer_version,
            provisional.matching_normalizer_version,
            generation,
            count,
            fts,
            provisional.environment,
        )
        descriptor_temp.write_text(
            json.dumps(asdict(manifest), default=str, sort_keys=True)
        )
        with descriptor_temp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(descriptor_temp, manifest.descriptor_path)
        published = True
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return manifest
    except (sqlite3.Error, OSError, ValidationError) as exc:
        raise CatalogError("index_build", str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)
        descriptor_temp.unlink(missing_ok=True)
        if not published:
            final.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class SearchIndex:
    manifest: IndexManifest
    connection: sqlite3.Connection
    catalog: CatalogProvider

    def count(self, where: str = "1", parameters: tuple[str, ...] = ()) -> int:
        self.catalog.require_open()
        return self.connection.execute(
            "SELECT count(*) FROM matching_items WHERE " + where, parameters
        ).fetchone()[0]

    def candidates(
        self, where: str = "1", parameters: tuple[str, ...] = ()
    ) -> Iterator[Candidate]:
        """Internal SQL predicates come from retrieval, values always use parameters."""
        self.catalog.require_open()
        for row in self.connection.execute(
            "SELECT payload FROM matching_items WHERE "
            + where
            + " ORDER BY listing_key",
            parameters,
        ):
            try:
                yield _CANDIDATE.validate_json(row[0], strict=True)
            except ValidationError as exc:
                raise CatalogError("index_payload", str(exc)) from exc


@contextmanager
def open_index(
    manifest: IndexManifest | Path, *, catalog: CatalogProvider
) -> Iterator[SearchIndex]:
    catalog.require_open()
    try:
        if isinstance(manifest, Path):
            descriptor = manifest.resolve()
            data = json.loads(descriptor.read_text())
            data["path"] = Path(data["path"])
            manifest = IndexManifest(**data)
            if manifest.path.parent != descriptor.parent:
                raise CatalogError(
                    "index_path", "descriptor cannot select another directory"
                )
        source = catalog.manifest
        if (
            manifest.schema,
            manifest.snapshot_id,
            manifest.snapshot_sha256,
            manifest.source_normalizer_version,
            manifest.matching_normalizer_version,
            manifest.environment,
        ) != (
            INDEX_SCHEMA,
            source.snapshot_id,
            source.snapshot_sha256,
            source.source_normalizer_version,
            NORMALIZER_VERSION,
            source.environment,
        ):
            raise CatalogError("index_generation", "source or normalizer mismatch")
        if manifest.path.is_symlink() or _digest(manifest.path) != manifest.sha256:
            raise CatalogError("index_hash", "content mismatch")
        with closing(
            sqlite3.connect(f"{manifest.path.as_uri()}?mode=ro&immutable=1", uri=True)
        ) as conn:
            metadata = {
                key: json.loads(value)
                for key, value in conn.execute("SELECT key,value FROM metadata")
            }
            if metadata != _metadata(manifest):
                raise CatalogError("index_metadata", "descriptor mismatch")
            if (
                conn.execute("SELECT count(*) FROM matching_items").fetchone()[0]
                != manifest.row_count
            ):
                raise CatalogError("index_count", "descriptor mismatch")
            yield SearchIndex(manifest, conn, catalog)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError("index", str(exc)) from exc
