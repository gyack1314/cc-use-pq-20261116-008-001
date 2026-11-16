from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection, Row
from typing import Iterator
import json
import sqlite3

from .contracts import HarvestError, Manifest, Record, Source, canonical, content_digest
from .intake import manifest_digest


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
 key TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
 kind TEXT NOT NULL, field_map TEXT NOT NULL, trust INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS source_revisions (
 source TEXT NOT NULL REFERENCES sources(key), revision INTEGER NOT NULL,
 name TEXT NOT NULL, format TEXT NOT NULL, kind TEXT NOT NULL,
 field_map TEXT NOT NULL, trust INTEGER NOT NULL,
 declaration_hash TEXT NOT NULL, registered_at TEXT NOT NULL,
 PRIMARY KEY(source,revision)
);
CREATE TABLE IF NOT EXISTS batches (
 source TEXT NOT NULL REFERENCES sources(key), batch TEXT NOT NULL,
 captured_at TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 manifest_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 started_at TEXT NOT NULL, completed_at TEXT,
 PRIMARY KEY(source,batch)
);
CREATE TABLE IF NOT EXISTS pages (
 source TEXT NOT NULL, batch TEXT NOT NULL, ordinal INTEGER NOT NULL,
 path TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, error_detail TEXT,
 processed_at TEXT, record_count INTEGER NOT NULL DEFAULT 0,
 source_revision INTEGER NOT NULL,
 PRIMARY KEY(source,batch,ordinal),
 FOREIGN KEY(source,batch) REFERENCES batches(source,batch)
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, batch TEXT NOT NULL,
 ordinal INTEGER NOT NULL, record_id TEXT NOT NULL, identity TEXT NOT NULL,
 record_json TEXT NOT NULL, raw_json TEXT NOT NULL, raw_digest TEXT NOT NULL,
 status TEXT NOT NULL, reasons TEXT NOT NULL, version INTEGER NOT NULL,
 captured_at TEXT NOT NULL, observed_at TEXT NOT NULL,
 source_revision INTEGER NOT NULL,
 UNIQUE(source,batch,ordinal,record_id),
 FOREIGN KEY(source,batch,ordinal) REFERENCES pages(source,batch,ordinal)
);
CREATE INDEX IF NOT EXISTS observations_identity ON observations(identity);
CREATE INDEX IF NOT EXISTS observations_source_record ON observations(source,record_id,version);
CREATE TABLE IF NOT EXISTS source_records (
 source TEXT NOT NULL REFERENCES sources(key), record_id TEXT NOT NULL,
 version INTEGER NOT NULL, latest_observation INTEGER NOT NULL REFERENCES observations(id),
 PRIMARY KEY(source,record_id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
 source TEXT, batch TEXT, ordinal INTEGER, payload TEXT NOT NULL,
 occurred_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        """Bring pre-versioning databases forward: bind pages and observations
        to the source revision that was current when they were recorded."""
        page_columns = {row[1] for row in self.db.execute("PRAGMA table_info(pages)")}
        if "source_revision" not in page_columns:
            self.db.execute("ALTER TABLE pages ADD COLUMN source_revision INTEGER")
            self.db.execute("UPDATE pages SET source_revision=(SELECT revision FROM sources WHERE sources.key=pages.source)")
        observation_columns = {row[1] for row in self.db.execute("PRAGMA table_info(observations)")}
        if "source_revision" not in observation_columns:
            self.db.execute("ALTER TABLE observations ADD COLUMN source_revision INTEGER")
            self.db.execute("""UPDATE observations SET source_revision=(
                               SELECT p.source_revision FROM pages p
                               WHERE p.source=observations.source AND p.batch=observations.batch
                               AND p.ordinal=observations.ordinal)""")
        for row in self.db.execute("SELECT * FROM sources").fetchall():
            document = {"key": row["key"], "name": row["name"], "format": row["format"],
                        "kind": row["kind"], "field_map": json.loads(row["field_map"]),
                        "trust": row["trust"]}
            # Legacy rows only kept the current declaration; earlier revisions differed
            # at most in name/trust (the contract was frozen), so backfill the chain
            # with the recoverable declaration.
            for revision in range(1, row["revision"] + 1):
                self.db.execute("INSERT OR IGNORE INTO source_revisions(source,revision,name,format,kind,field_map,trust,declaration_hash,registered_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                (row["key"], revision, row["name"], row["format"], row["kind"],
                                 row["field_map"], row["trust"], content_digest(document), utc_now()))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, kind: str, source: str | None, batch: str | None,
              ordinal: int | None, payload: dict) -> None:
        self.db.execute("INSERT INTO events(event_type,source,batch,ordinal,payload,occurred_at) VALUES(?,?,?,?,?,?)",
                        (kind, source, batch, ordinal, canonical(payload), utc_now()))

    def register(self, source: Source) -> dict:
        declaration_hash = content_digest(source.document())
        with self.transaction():
            old = self.db.execute("SELECT * FROM sources WHERE key=?", (source.key,)).fetchone()
            if old:
                if old["withdrawn_at"]:
                    raise HarvestError("source_withdrawn", source.key)
                old_document = {"key": old["key"], "name": old["name"], "format": old["format"],
                                "kind": old["kind"], "field_map": json.loads(old["field_map"]),
                                "trust": old["trust"]}
                if old_document == source.document():
                    return {"source": source.key, "revision": old["revision"], "unchanged": True}
                revision = old["revision"] + 1
                self.db.execute("UPDATE sources SET name=?,format=?,kind=?,field_map=?,trust=?,revision=? WHERE key=?",
                                (source.name, source.format, source.kind, canonical(source.field_map),
                                 source.trust, revision, source.key))
                self._store_revision(source, revision, declaration_hash)
                self.event("source_revised", source.key, None, None,
                           {"revision": revision, "declaration_hash": declaration_hash})
                return {"source": source.key, "revision": revision, "unchanged": False}
            self.db.execute("INSERT INTO sources(key,name,format,kind,field_map,trust) VALUES(?,?,?,?,?,?)",
                            (source.key, source.name, source.format, source.kind,
                             canonical(source.field_map), source.trust))
            self._store_revision(source, 1, declaration_hash)
            self.event("source_registered", source.key, None, None,
                       {**source.document(), "declaration_hash": declaration_hash})
            return {"source": source.key, "revision": 1, "unchanged": False}

    def _store_revision(self, source: Source, revision: int, declaration_hash: str) -> None:
        self.db.execute("INSERT INTO source_revisions(source,revision,name,format,kind,field_map,trust,declaration_hash,registered_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (source.key, revision, source.name, source.format, source.kind,
                         canonical(source.field_map), source.trust, declaration_hash, utc_now()))

    def current_revision(self, key: str) -> int:
        row = self.db.execute("SELECT revision,withdrawn_at FROM sources WHERE key=?", (key,)).fetchone()
        if row is None:
            raise HarvestError("source_unknown", key)
        if row["withdrawn_at"]:
            raise HarvestError("source_withdrawn", key)
        return row["revision"]

    def source(self, key: str) -> Source:
        return self.source_at(key, self.current_revision(key))

    def source_at(self, key: str, revision: int) -> Source:
        row = self.db.execute("SELECT * FROM source_revisions WHERE source=? AND revision=?",
                              (key, revision)).fetchone()
        if row is None:
            raise HarvestError("source_revision_unknown", f"{key}@{revision}")
        return Source(row["source"], row["name"], row["format"], row["kind"],
                      json.loads(row["field_map"]), row["trust"])

    def revisions(self, key: str) -> dict:
        row = self.db.execute("SELECT revision,withdrawn_at FROM sources WHERE key=?", (key,)).fetchone()
        if row is None:
            raise HarvestError("source_unknown", key)
        entries = []
        for item in self.db.execute("SELECT * FROM source_revisions WHERE source=? ORDER BY revision", (key,)):
            entries.append({"revision": item["revision"], "name": item["name"],
                            "format": item["format"], "kind": item["kind"],
                            "field_map": json.loads(item["field_map"]), "trust": item["trust"],
                            "declaration_hash": item["declaration_hash"],
                            "registered_at": item["registered_at"],
                            "current": item["revision"] == row["revision"]})
        return {"source": key, "current_revision": row["revision"],
                "withdrawn_at": row["withdrawn_at"], "revisions": entries}

    def ensure_batch(self, manifest: Manifest) -> dict:
        current = self.current_revision(manifest.source)
        bindings: list[int] = []
        for page in manifest.pages:
            revision = page.source_revision if page.source_revision is not None else current
            if not 1 <= revision <= current:
                raise HarvestError("source_revision_unknown", f"{manifest.source}@{revision}")
            bindings.append(revision)
        known = {row[0] for row in self.db.execute(
            "SELECT revision FROM source_revisions WHERE source=?", (manifest.source,))}
        for revision in bindings:
            if revision not in known:
                raise HarvestError("source_revision_unknown", f"{manifest.source}@{revision}")
        digest = manifest_digest(manifest)
        with self.transaction():
            existing = self.db.execute("SELECT * FROM batches WHERE source=? AND batch=?",
                                       (manifest.source, manifest.batch)).fetchone()
            if existing:
                if existing["manifest_hash"] != digest:
                    raise HarvestError("batch_immutable", "batch identifier already has different manifest")
                return self.batch(manifest.source, manifest.batch)
            self.db.execute("INSERT INTO batches(source,batch,captured_at,manifest_hash,manifest_json,started_at) VALUES(?,?,?,?,?,?)",
                            (manifest.source, manifest.batch, manifest.captured_at, digest,
                             canonical(manifest.document()), utc_now()))
            self.db.executemany("INSERT INTO pages(source,batch,ordinal,path,digest,source_revision) VALUES(?,?,?,?,?,?)",
                                [(manifest.source, manifest.batch, page.ordinal, page.path, page.sha256, revision)
                                 for page, revision in zip(manifest.pages, bindings)])
            self.event("batch_registered", manifest.source, manifest.batch, None,
                       {"manifest_hash": digest, "page_count": len(manifest.pages),
                        "revisions": sorted(set(bindings))})
        return self.batch(manifest.source, manifest.batch)

    def batch(self, source: str, batch: str) -> dict:
        row = self.db.execute("SELECT * FROM batches WHERE source=? AND batch=?", (source, batch)).fetchone()
        if row is None:
            raise HarvestError("batch_unknown", f"{source}/{batch}")
        pages = [dict(p) for p in self.db.execute("SELECT * FROM pages WHERE source=? AND batch=? ORDER BY ordinal",
                                                 (source, batch))]
        return {"source": source, "batch": batch, "captured_at": row["captured_at"],
                "manifest_hash": row["manifest_hash"], "state": row["state"],
                "pages": pages, "completed_at": row["completed_at"]}

    def page(self, source: str, batch: str, ordinal: int) -> dict:
        row = self.db.execute("SELECT * FROM pages WHERE source=? AND batch=? AND ordinal=?",
                              (source, batch, ordinal)).fetchone()
        if row is None:
            raise HarvestError("page_unknown", f"{source}/{batch}/{ordinal}")
        return dict(row)

    def fail_page(self, source: str, batch: str, ordinal: int, error: HarvestError) -> None:
        with self.transaction():
            row = self.page(source, batch, ordinal)
            if row["state"] == "complete":
                return
            self.db.execute("UPDATE pages SET state='failed',attempts=attempts+1,error_code=?,error_detail=? WHERE source=? AND batch=? AND ordinal=?",
                            (error.code, error.detail, source, batch, ordinal))
            self.db.execute("UPDATE batches SET state='failed' WHERE source=? AND batch=?", (source, batch))
            self.event("page_failed", source, batch, ordinal, {"code": error.code, "detail": error.detail})

    def commit_page(self, source: str, batch: str, ordinal: int,
                    items: list[tuple[Record, dict, str, tuple[str, ...]]]) -> dict:
        with self.transaction():
            row = self.page(source, batch, ordinal)
            if row["state"] == "complete":
                return {"ordinal": ordinal, "unchanged": True, "records": row["record_count"]}
            captured = self.db.execute("SELECT captured_at FROM batches WHERE source=? AND batch=?",
                                       (source, batch)).fetchone()[0]
            revision = row["source_revision"]
            for record, raw, status, reasons in items:
                previous = self.db.execute("SELECT version FROM source_records WHERE source=? AND record_id=?",
                                           (source, record.source_record_id)).fetchone()
                version = previous[0] + 1 if previous else 1
                cursor = self.db.execute("INSERT INTO observations(source,batch,ordinal,record_id,identity,record_json,raw_json,raw_digest,status,reasons,version,captured_at,observed_at,source_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                         (source, batch, ordinal, record.source_record_id, record.identity,
                                          canonical(record.document()), canonical(raw), record.raw_digest,
                                          status, canonical(reasons), version, captured, utc_now(), revision))
                self.db.execute("INSERT INTO source_records(source,record_id,version,latest_observation) VALUES(?,?,?,?) ON CONFLICT(source,record_id) DO UPDATE SET version=excluded.version,latest_observation=excluded.latest_observation",
                                (source, record.source_record_id, version, cursor.lastrowid))
            self.db.execute("UPDATE pages SET state='complete',attempts=attempts+1,error_code=NULL,error_detail=NULL,processed_at=?,record_count=? WHERE source=? AND batch=? AND ordinal=?",
                            (utc_now(), len(items), source, batch, ordinal))
            self.event("page_committed", source, batch, ordinal, {"records": len(items)})
            incomplete = self.db.execute("SELECT count(*) FROM pages WHERE source=? AND batch=? AND state!='complete'",
                                         (source, batch)).fetchone()[0]
            if incomplete == 0:
                self.db.execute("UPDATE batches SET state='complete',completed_at=? WHERE source=? AND batch=?",
                                (utc_now(), source, batch))
                self.event("batch_completed", source, batch, None, {"records_last_page": len(items)})
            else:
                failed = self.db.execute("SELECT count(*) FROM pages WHERE source=? AND batch=? AND state='failed'",
                                         (source, batch)).fetchone()[0]
                self.db.execute("UPDATE batches SET state=? WHERE source=? AND batch=?",
                                ("failed" if failed else "pending", source, batch))
            return {"ordinal": ordinal, "unchanged": False, "records": len(items)}

    def withdraw(self, source: str, reason: str) -> dict:
        with self.transaction():
            row = self.db.execute("SELECT withdrawn_at FROM sources WHERE key=?", (source,)).fetchone()
            if row is None:
                raise HarvestError("source_unknown", source)
            if row[0]:
                return {"source": source, "withdrawn_at": row[0], "unchanged": True}
            at = utc_now()
            self.db.execute("UPDATE sources SET withdrawn_at=? WHERE key=?", (at, source))
            self.event("source_withdrawn", source, None, None, {"reason": reason})
            return {"source": source, "withdrawn_at": at, "unchanged": False}

    def observations(self, *, source: str | None = None, identity: str | None = None) -> list[dict]:
        conditions = []
        params = []
        if source is not None:
            conditions.append("o.source=?")
            params.append(source)
        if identity is not None:
            conditions.append("o.identity=?")
            params.append(identity)
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.execute("SELECT o.*,s.trust,s.withdrawn_at FROM observations o JOIN sources s ON s.key=o.source"
                               + clause + " ORDER BY o.id", params)
        return [{**dict(row), "record": json.loads(row["record_json"]),
                 "raw": json.loads(row["raw_json"]), "reasons": json.loads(row["reasons"])} for row in rows]

    def events(self, *, source: str | None = None) -> list[dict]:
        if source is None:
            rows = self.db.execute("SELECT * FROM events ORDER BY id")
        else:
            rows = self.db.execute("SELECT * FROM events WHERE source=? ORDER BY id", (source,))
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
