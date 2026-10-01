"""SQLite knowledge-graph store (stdlib ``sqlite3``, WAL, one shared connection behind an RLock).

Tables (see docs/KG_API.md §1)::

    entities(id, name, norm_name UNIQUE, type, description, mention_count, created_at, updated_at, name_vec)
    aliases(norm_name PK, entity_id)                  -- names merged into another entity keep resolving to it
    relations(id, head_id, relation, tail_id, weight, created_at, UNIQUE(head_id, relation, tail_id))
    evidence(id, relation_id, source_kind, source_ref, snippet, created_at, UNIQUE(relation_id, source_kind, source_ref))
    queue(id, kind, ref, text, hash UNIQUE, status, attempts, error, created_at, updated_at)
    meta(key PK, value)

All public methods are thread-safe. ``version`` increases on every graph write so readers
(retrieval) can cache the entity name list / name-vector matrix cheaply.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

ENTITY_TYPES = ("person", "org", "place", "concept", "file", "code", "product", "event", "other")
SOURCE_KINDS = ("chat", "file", "web")
QUEUE_STATUSES = ("pending", "running", "done", "error")
MAX_ATTEMPTS = 3
SNIPPET_MAX = 300
NAME_MAX = 80
DESC_MAX = 40
RELATION_MAX = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'other',
    description TEXT NOT NULL DEFAULT '',
    mention_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    name_vec BLOB
);
CREATE TABLE IF NOT EXISTS aliases(
    norm_name TEXT PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS relations(
    id INTEGER PRIMARY KEY,
    head_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    tail_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    weight REAL NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE(head_id, relation, tail_id)
);
CREATE INDEX IF NOT EXISTS idx_rel_head ON relations(head_id);
CREATE INDEX IF NOT EXISTS idx_rel_tail ON relations(tail_id);
CREATE TABLE IF NOT EXISTS evidence(
    id INTEGER PRIMARY KEY,
    relation_id INTEGER NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(relation_id, source_kind, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_ev_rel ON evidence(relation_id);
CREATE TABLE IF NOT EXISTS queue(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, id);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


# ----------------------------------------------------------------------------- normalisation
def normalize_name(name: Any) -> str:
    """NFKC + lowercase, drop whitespace / punctuation / control chars (CJK and symbols such as + are kept)."""
    s = unicodedata.normalize("NFKC", str(name or "")).lower()
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "Z") or cat in ("Cc", "Cf") or ch.isspace():
            continue
        out.append(ch)
    return "".join(out)


def clean_text(value: Any, limit: int) -> str:
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].strip()


def clean_relation(value: Any) -> str:
    s = clean_text(value, RELATION_MAX * 2).strip(" -—>→:：,，.。")
    return s.lower()[:RELATION_MAX].strip()


def clean_type(value: Any) -> str:
    t = str(value or "").strip().lower()
    return t if t in ENTITY_TYPES else "other"


def text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def make_snippet(text: str, needles: Sequence[str], limit: int = SNIPPET_MAX) -> str:
    """A ≤ ``limit`` char window of ``text`` around the first needle found (case-insensitive)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= limit:
        return t
    low = t.lower()
    pos = -1
    for n in needles:
        n = (n or "").strip().lower()
        if n:
            pos = low.find(n)
            if pos >= 0:
                break
    if pos < 0:
        return t[:limit - 1] + "…"
    start = max(0, pos - limit // 3)
    end = min(len(t), start + limit - 2)
    start = max(0, end - (limit - 2))
    return ("…" if start > 0 else "") + t[start:end] + ("…" if end < len(t) else "")


class KGStore:
    def __init__(self, db_path: str):
        self.path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        self.version = 0

    # ------------------------------------------------------------------ plumbing
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @contextmanager
    def _tx(self, write: bool = True):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
                if write:
                    self.version += 1

    def _q(self, sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, value))

    # ------------------------------------------------------------------ entities
    def _resolve(self, conn: sqlite3.Connection, norm: str) -> Optional[int]:
        row = conn.execute("SELECT id FROM entities WHERE norm_name=?", (norm,)).fetchone()
        if row:
            return int(row["id"])
        row = conn.execute("SELECT entity_id FROM aliases WHERE norm_name=?", (norm,)).fetchone()
        return int(row["entity_id"]) if row else None

    def _upsert(self, conn: sqlite3.Connection, name: str, etype: str = "other", description: str = "",
                mention: int = 1) -> Optional[int]:
        name = clean_text(name, NAME_MAX)
        norm = normalize_name(name)
        if not norm:
            return None
        etype = clean_type(etype)
        description = clean_text(description, DESC_MAX)
        now = time.time()
        eid = self._resolve(conn, norm)
        if eid is None:
            cur = conn.execute(
                "INSERT INTO entities(name, norm_name, type, description, mention_count, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (name, norm, etype, description, mention, now, now))
            return int(cur.lastrowid)
        conn.execute(
            "UPDATE entities SET mention_count = mention_count + ?, updated_at = ?, "
            "type = CASE WHEN type = 'other' AND ? != 'other' THEN ? ELSE type END, "
            "description = CASE WHEN description = '' THEN ? ELSE description END WHERE id = ?",
            (mention, now, etype, etype, description, eid))
        return eid

    def upsert_entity(self, name: str, etype: str = "other", description: str = "", mention: int = 1) -> Optional[int]:
        with self._tx() as conn:
            return self._upsert(conn, name, etype, description, mention)

    def _add_relation(self, conn, head_id: int, relation: str, tail_id: int, source_kind: str, source_ref: str,
                      snippet: str) -> Optional[int]:
        rel = clean_relation(relation)
        if not rel or head_id == tail_id:
            return None
        now = time.time()
        row = conn.execute("SELECT id FROM relations WHERE head_id=? AND relation=? AND tail_id=?",
                           (head_id, rel, tail_id)).fetchone()
        if row is None:
            rid = int(conn.execute("INSERT INTO relations(head_id, relation, tail_id, weight, created_at) VALUES(?,?,?,1,?)",
                                   (head_id, rel, tail_id, now)).lastrowid)
            new_rel = True
        else:
            rid = int(row["id"])
            new_rel = False
        kind = source_kind if source_kind in SOURCE_KINDS else "chat"
        cur = conn.execute("INSERT OR IGNORE INTO evidence(relation_id, source_kind, source_ref, snippet, created_at) "
                           "VALUES(?,?,?,?,?)", (rid, kind, str(source_ref or ""), (snippet or "")[:SNIPPET_MAX], now))
        if cur.rowcount and not new_rel:
            conn.execute("UPDATE relations SET weight = weight + 1 WHERE id=?", (rid,))
        return rid

    def add_relation(self, head: str, relation: str, tail: str, source_kind: str = "chat", source_ref: str = "",
                     snippet: str = "") -> Optional[int]:
        with self._tx() as conn:
            h = self._upsert(conn, head)
            t = self._upsert(conn, tail)
            if h is None or t is None:
                return None
            return self._add_relation(conn, h, relation, t, source_kind, source_ref, snippet)

    def ingest(self, result: Dict[str, Any], source_kind: str, source_ref: str, text: str) -> Tuple[int, int]:
        """Apply one extraction result atomically -> (entities touched, relations touched)."""
        ents = result.get("entities") or []
        rels = result.get("relations") or []
        ids: Dict[str, int] = {}
        n_rel = 0
        with self._tx() as conn:
            for e in ents:
                eid = self._upsert(conn, e.get("name", ""), e.get("type", "other"), e.get("description", ""))
                if eid is not None:
                    ids[normalize_name(e.get("name", ""))] = eid
            for r in rels:
                hn, tn = normalize_name(r.get("head", "")), normalize_name(r.get("tail", ""))
                if not hn or not tn:
                    continue
                h = ids.get(hn)
                if h is None:
                    h = self._upsert(conn, r["head"])
                    if h is not None:
                        ids[hn] = h
                t = ids.get(tn)
                if t is None:
                    t = self._upsert(conn, r["tail"])
                    if t is not None:
                        ids[tn] = t
                if h is None or t is None:
                    continue
                snippet = make_snippet(text, [r.get("head", ""), r.get("tail", "")])
                if self._add_relation(conn, h, r.get("relation", ""), t, source_kind, source_ref, snippet) is not None:
                    n_rel += 1
        return len(ids), n_rel

    def _entity_dict(self, row: sqlite3.Row, relation_count: Optional[int] = None) -> Dict[str, Any]:
        d = {"id": int(row["id"]), "name": row["name"], "type": row["type"], "description": row["description"],
             "mention_count": int(row["mention_count"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}
        if relation_count is not None:
            d["relation_count"] = int(relation_count)
        return d

    def get_entity(self, entity_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT e.*, (SELECT COUNT(*) FROM relations r WHERE r.head_id=e.id OR r.tail_id=e.id) AS rc "
                        "FROM entities e WHERE e.id=?", (int(entity_id),))
        if row is None:
            return None
        d = self._entity_dict(row, row["rc"])
        d["aliases"] = [r["norm_name"] for r in self._q("SELECT norm_name FROM aliases WHERE entity_id=? ORDER BY norm_name",
                                                        (int(entity_id),))]
        return d

    def find_entity(self, name: str) -> Optional[Dict[str, Any]]:
        norm = normalize_name(name)
        if not norm:
            return None
        with self._lock:
            eid = self._resolve(self._conn, norm)
        return self.get_entity(eid) if eid is not None else None

    def list_entities(self, q: str = "", limit: int = 50, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        where, args = "", []
        q = (q or "").strip()
        if q:
            norm = normalize_name(q)
            like = f"%{q.replace('%', '').replace('_', '')}%"
            where = ("WHERE e.name LIKE ? OR e.norm_name LIKE ? OR e.description LIKE ? "
                     "OR e.id IN (SELECT entity_id FROM aliases WHERE norm_name LIKE ?)")
            nlike = f"%{norm}%" if norm else like
            args = [like, nlike, like, nlike]
        total = int(self._one(f"SELECT COUNT(*) AS n FROM entities e {where}", args)["n"])
        rows = self._q(
            "SELECT e.*, (SELECT COUNT(*) FROM relations r WHERE r.head_id=e.id OR r.tail_id=e.id) AS rc "
            f"FROM entities e {where} ORDER BY rc DESC, e.mention_count DESC, e.name COLLATE NOCASE LIMIT ? OFFSET ?",
            args + [limit, offset])
        return [self._entity_dict(r, r["rc"]) for r in rows], total

    def entity_relations(self, entity_id: int, evidence_limit: int = 5) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT r.id, r.relation, r.weight, r.head_id, r.tail_id, h.name AS hname, h.type AS htype, "
            "t.name AS tname, t.type AS ttype, (SELECT COUNT(*) FROM evidence v WHERE v.relation_id=r.id) AS ec "
            "FROM relations r JOIN entities h ON h.id=r.head_id JOIN entities t ON t.id=r.tail_id "
            "WHERE r.head_id=? OR r.tail_id=? ORDER BY r.weight DESC, ec DESC, r.id", (int(entity_id), int(entity_id)))
        out = []
        for r in rows:
            ev = self._q("SELECT source_kind, source_ref, snippet FROM evidence WHERE relation_id=? ORDER BY id DESC LIMIT ?",
                         (int(r["id"]), int(evidence_limit)))
            out.append({
                "id": int(r["id"]), "relation": r["relation"], "weight": r["weight"],
                "direction": "out" if int(r["head_id"]) == int(entity_id) else "in",
                "head": {"id": int(r["head_id"]), "name": r["hname"], "type": r["htype"]},
                "tail": {"id": int(r["tail_id"]), "name": r["tname"], "type": r["ttype"]},
                "evidence_count": int(r["ec"]),
                "evidence": [{"source_kind": e["source_kind"], "source_ref": e["source_ref"], "snippet": e["snippet"]} for e in ev],
            })
        return out

    def delete_relation(self, relation_id: int) -> bool:
        with self._tx() as conn:
            return conn.execute("DELETE FROM relations WHERE id=?", (int(relation_id),)).rowcount > 0

    def delete_entity(self, entity_id: int) -> Optional[int]:
        """Delete an entity with its relations/evidence/aliases (FK cascade). -> number of relations removed, or None."""
        with self._tx() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM relations WHERE head_id=? OR tail_id=?",
                             (int(entity_id), int(entity_id))).fetchone()["n"]
            if conn.execute("DELETE FROM entities WHERE id=?", (int(entity_id),)).rowcount == 0:
                return None
            return int(n)

    def merge_entities(self, keep_id: int, merge_ids: Iterable[int]) -> Dict[str, Any]:
        keep_id = int(keep_id)
        merge_ids = [int(m) for m in merge_ids if int(m) != keep_id]
        with self._tx() as conn:
            keep = conn.execute("SELECT * FROM entities WHERE id=?", (keep_id,)).fetchone()
            if keep is None:
                raise KeyError(keep_id)
            for mid in merge_ids:
                m = conn.execute("SELECT * FROM entities WHERE id=?", (mid,)).fetchone()
                if m is None:
                    raise KeyError(mid)
            for mid in merge_ids:
                m = conn.execute("SELECT * FROM entities WHERE id=?", (mid,)).fetchone()
                for r in conn.execute("SELECT * FROM relations WHERE head_id=? OR tail_id=?", (mid, mid)).fetchall():
                    nh = keep_id if int(r["head_id"]) == mid else int(r["head_id"])
                    nt = keep_id if int(r["tail_id"]) == mid else int(r["tail_id"])
                    if nh == nt:
                        conn.execute("DELETE FROM relations WHERE id=?", (r["id"],))
                        continue
                    ex = conn.execute("SELECT id FROM relations WHERE head_id=? AND relation=? AND tail_id=? AND id!=?",
                                      (nh, r["relation"], nt, r["id"])).fetchone()
                    if ex is None:
                        conn.execute("UPDATE relations SET head_id=?, tail_id=? WHERE id=?", (nh, nt, r["id"]))
                        continue
                    moved = conn.execute(
                        "INSERT OR IGNORE INTO evidence(relation_id, source_kind, source_ref, snippet, created_at) "
                        "SELECT ?, source_kind, source_ref, snippet, created_at FROM evidence WHERE relation_id=?",
                        (ex["id"], r["id"])).rowcount
                    conn.execute("UPDATE relations SET weight = weight + ? WHERE id=?", (max(1, moved), ex["id"]))
                    conn.execute("DELETE FROM relations WHERE id=?", (r["id"],))
                conn.execute("UPDATE aliases SET entity_id=? WHERE entity_id=?", (keep_id, mid))
                conn.execute("UPDATE entities SET mention_count = mention_count + ?, updated_at=?, "
                             "description = CASE WHEN description='' THEN ? ELSE description END, "
                             "type = CASE WHEN type='other' THEN ? ELSE type END WHERE id=?",
                             (int(m["mention_count"]), time.time(), m["description"], m["type"], keep_id))
                conn.execute("DELETE FROM entities WHERE id=?", (mid,))
                conn.execute("INSERT OR REPLACE INTO aliases(norm_name, entity_id) VALUES(?, ?)", (m["norm_name"], keep_id))
        return self.get_entity(keep_id) or {}

    def clear_graph(self) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM evidence")
            conn.execute("DELETE FROM relations")
            conn.execute("DELETE FROM aliases")
            conn.execute("DELETE FROM entities")

    def counts(self) -> Dict[str, int]:
        with self._lock:
            e = self._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            r = self._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        return {"entities": int(e), "relations": int(r)}

    # ------------------------------------------------------------------ retrieval helpers
    def name_index(self) -> List[Tuple[str, int]]:
        """[(norm_name, entity_id)] incl. aliases, longest names first (for substring matching)."""
        rows = self._q("SELECT norm_name, id AS eid FROM entities UNION ALL SELECT norm_name, entity_id AS eid FROM aliases")
        out = [(r["norm_name"], int(r["eid"])) for r in rows if r["norm_name"]]
        out.sort(key=lambda x: (-len(x[0]), x[0]))
        return out

    def relations_touching(self, entity_ids: Iterable[int]) -> List[Dict[str, Any]]:
        ids = sorted({int(i) for i in entity_ids})
        if not ids:
            return []
        out: Dict[int, Dict[str, Any]] = {}
        for i in range(0, len(ids), 400):  # SQLite variable limit
            part = ids[i:i + 400]
            ph = ",".join("?" * len(part))
            rows = self._q(
                "SELECT r.id, r.relation, r.weight, r.head_id, r.tail_id, h.name AS hname, t.name AS tname, "
                "(SELECT COUNT(*) FROM evidence v WHERE v.relation_id=r.id) AS ec "
                "FROM relations r JOIN entities h ON h.id=r.head_id JOIN entities t ON t.id=r.tail_id "
                f"WHERE r.head_id IN ({ph}) OR r.tail_id IN ({ph})", part + part)
            for r in rows:
                out[int(r["id"])] = {"id": int(r["id"]), "relation": r["relation"], "weight": float(r["weight"]),
                                     "head_id": int(r["head_id"]), "tail_id": int(r["tail_id"]),
                                     "head": r["hname"], "tail": r["tname"], "evidence_count": int(r["ec"])}
        return list(out.values())

    def entities_missing_vectors(self, limit: int = 64) -> List[Tuple[int, str]]:
        return [(int(r["id"]), r["name"]) for r in
                self._q("SELECT id, name FROM entities WHERE name_vec IS NULL ORDER BY mention_count DESC, id LIMIT ?", (int(limit),))]

    def set_vectors(self, items: Sequence[Tuple[int, np.ndarray]]) -> None:
        if not items:
            return
        with self._tx() as conn:
            for eid, vec in items:
                v = np.asarray(vec, dtype=np.float32).ravel()
                conn.execute("UPDATE entities SET name_vec=? WHERE id=?", (v.tobytes(), int(eid)))
            if items:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('vec_dim', ?)",
                             (str(int(np.asarray(items[0][1]).size)),))

    def clear_vectors(self) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE entities SET name_vec=NULL")

    def vector_matrix(self) -> Tuple[List[int], Optional[np.ndarray]]:
        rows = self._q("SELECT id, name_vec FROM entities WHERE name_vec IS NOT NULL")
        if not rows:
            return [], None
        dims: Dict[int, int] = {}
        vecs = []
        for r in rows:
            v = np.frombuffer(r["name_vec"], dtype=np.float32)
            dims[v.size] = dims.get(v.size, 0) + 1
            vecs.append((int(r["id"]), v))
        dim = max(dims, key=dims.get)
        ids = [i for i, v in vecs if v.size == dim]
        mat = np.stack([v for _, v in vecs if v.size == dim]).astype(np.float32) if ids else None
        return ids, mat

    # ------------------------------------------------------------------ queue
    def enqueue(self, kind: str, ref: str, text: str) -> bool:
        return self.enqueue_many([(kind, ref, text)]) > 0

    def enqueue_many(self, items: Iterable[Tuple[str, str, str]]) -> int:
        now = time.time()
        n = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for kind, ref, text in items:
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO queue(kind, ref, text, hash, status, attempts, error, created_at, updated_at) "
                        "VALUES(?,?,?,?, 'pending', 0, '', ?, ?)",
                        (kind if kind in SOURCE_KINDS else "chat", str(ref or ""), text, text_hash(text), now, now))
                    n += cur.rowcount
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return n

    def queue_counts(self) -> Dict[str, int]:
        out = {s: 0 for s in QUEUE_STATUSES}
        for r in self._q("SELECT status, COUNT(*) AS n FROM queue GROUP BY status"):
            if r["status"] in out:
                out[r["status"]] = int(r["n"])
        return out

    def has_pending(self) -> bool:
        return self._one("SELECT 1 FROM queue WHERE status='pending' LIMIT 1") is not None

    def claim_next(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM queue WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
                if row is not None:
                    self._conn.execute("UPDATE queue SET status='running', updated_at=? WHERE id=?", (time.time(), row["id"]))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return dict(row) if row is not None else None

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM queue WHERE id=?", (int(job_id),))
        return dict(row) if row else None

    def finish_job(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE queue SET status='done', error='', updated_at=? WHERE id=?", (time.time(), int(job_id)))

    def fail_job(self, job_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE queue SET status='error', attempts=attempts+1, error=?, updated_at=? WHERE id=?",
                               ((error or "")[:500], time.time(), int(job_id)))

    def requeue_job(self, job_id: int, error: str = "") -> str:
        """attempts+1; back to pending, or error once MAX_ATTEMPTS is reached. -> new status."""
        with self._lock:
            row = self._conn.execute("SELECT attempts FROM queue WHERE id=?", (int(job_id),)).fetchone()
            if row is None:
                return ""
            attempts = int(row["attempts"]) + 1
            status = "error" if attempts >= MAX_ATTEMPTS else "pending"
            self._conn.execute("UPDATE queue SET status=?, attempts=?, error=?, updated_at=? WHERE id=?",
                               (status, attempts, (error or "")[:500], time.time(), int(job_id)))
            return status

    def reset_running(self) -> int:
        with self._lock:
            return self._conn.execute("UPDATE queue SET status='pending' WHERE status='running'").rowcount

    def clear_errors(self) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM queue WHERE status='error'").rowcount

    def clear_queue(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM queue")


__all__ = ["KGStore", "normalize_name", "clean_relation", "clean_type", "make_snippet", "text_hash",
           "ENTITY_TYPES", "MAX_ATTEMPTS"]
