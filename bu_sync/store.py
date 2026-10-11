"""SQLite state: canonical hires, per-(hire, system) ledger, hash-chained audit.

The ledger is what makes the pipeline resumable and idempotent: a crash between
two systems resumes at the first system whose ledger row isn't APPLIED for the
current desired-state hash.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS hires(hire_id TEXT PRIMARY KEY, canonical TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS ledger(
  hire_id TEXT, system TEXT, desired_hash TEXT, applied_payload TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, last_error TEXT, decided_by TEXT, updated_at TEXT,
  PRIMARY KEY(hire_id, system));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, hire_id TEXT, system TEXT, event TEXT,
  detail TEXT, prev_hash TEXT, row_hash TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # canonical hires
    def save_hire(self, hire_id: str, canonical: dict):
        self.db.execute("INSERT INTO hires VALUES(?,?,?) ON CONFLICT(hire_id) DO UPDATE SET "
                        "canonical=excluded.canonical, updated_at=excluded.updated_at",
                        (hire_id, json.dumps(canonical, default=str), _now()))
        self.db.commit()

    def known_hire_ids(self) -> set[str]:
        return {r["hire_id"] for r in self.db.execute("SELECT hire_id FROM hires")}

    # ledger
    def get_ledger(self, hire_id: str, system: str) -> dict | None:
        r = self.db.execute("SELECT * FROM ledger WHERE hire_id=? AND system=?", (hire_id, system)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["applied_payload"] = json.loads(d["applied_payload"]) if d["applied_payload"] else None
        return d

    def put_ledger(self, hire_id, system, *, desired_hash, status, applied_payload=None,
                   attempts=0, last_error=None, decided_by=None):
        prev = self.get_ledger(hire_id, system)
        if applied_payload is None and prev:                # keep last known-good write
            applied_payload = prev["applied_payload"]
        self.db.execute(
            "INSERT OR REPLACE INTO ledger VALUES(?,?,?,?,?,?,?,?,?)",
            (hire_id, system, desired_hash, json.dumps(applied_payload) if applied_payload else None,
             str(getattr(status, "value", status)), attempts, last_error, decided_by, _now()))
        self.db.commit()

    # kv (alert de-duplication)
    def kv_get(self, k):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def kv_set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
        self.db.commit()

    # audit (append-only, hash-chained so tampering is detectable)
    def audit(self, hire_id, system, event, detail=None):
        last = self.db.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["row_hash"] if last else "GENESIS"
        ts, d = _now(), json.dumps(detail or {}, sort_keys=True, default=str)
        h = hashlib.sha256("|".join([prev, ts, hire_id, system or "", event, d]).encode()).hexdigest()
        self.db.execute("INSERT INTO audit(ts,hire_id,system,event,detail,prev_hash,row_hash) VALUES(?,?,?,?,?,?,?)",
                        (ts, hire_id, system, event, d, prev, h))
        self.db.commit()

    def audit_rows(self, hire_id: str | None = None):
        q, a = "SELECT * FROM audit", ()
        if hire_id:
            q, a = q + " WHERE hire_id=?", (hire_id,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", a)]

    def verify_audit(self) -> bool:
        prev = "GENESIS"
        for r in self.db.execute("SELECT * FROM audit ORDER BY id"):
            h = hashlib.sha256("|".join([prev, r["ts"], r["hire_id"], r["system"] or "", r["event"], r["detail"]]).encode()).hexdigest()
            if r["prev_hash"] != prev or r["row_hash"] != h:
                return False
            prev = r["row_hash"]
        return True
