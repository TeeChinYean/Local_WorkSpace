"""Graph RAG retrieval: entities mentioned in the query (+ name-embedding neighbours) → 1-2 hop relations
→ a compact "【知识图谱】" block for the per-turn part of the prompt."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .store import KGStore, normalize_name

BLOCK_HEADER = "【知识图谱】"
MAX_MENTIONS = 8
EMBED_TOP_K = 3
EMBED_MIN_COS = 0.6
MIN_NAME_LEN = 2
TWO_HOP_BELOW = 4          # expand to 2 hops when 1 hop finds fewer relations than this
MAX_LINES = 40


def match_mentions(query: str, name_index: Sequence[Tuple[str, int]], max_n: int = MAX_MENTIONS) -> List[int]:
    """Substring match of normalised names (≥2 chars) in the normalised query, longest first.
    A shorter name that only occurs inside an already matched longer name is skipped."""
    qn = normalize_name(query)
    if not qn:
        return []
    found: List[int] = []
    taken: List[Tuple[int, int]] = []   # matched spans in qn
    for norm, eid in name_index:
        if len(norm) < MIN_NAME_LEN or len(norm) > len(qn):
            continue
        pos = qn.find(norm)
        ok = False
        while pos >= 0:
            span = (pos, pos + len(norm))
            if not any(a <= span[0] and span[1] <= b for a, b in taken):
                ok = True
                taken.append(span)
                break
            pos = qn.find(norm, pos + 1)
        if ok and eid not in found:
            found.append(eid)
            if len(found) >= max_n:
                break
    return found


def embedding_neighbours(query_vec: Optional[np.ndarray], ids: Sequence[int], mat: Optional[np.ndarray],
                         k: int = EMBED_TOP_K, min_cos: float = EMBED_MIN_COS) -> List[int]:
    if query_vec is None or mat is None or not len(ids):
        return []
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    if q.size != mat.shape[1]:
        return []
    qn = float(np.linalg.norm(q)) or 1.0
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ q) / (norms * qn)
    order = np.argsort(-sims)[:k]
    return [int(ids[i]) for i in order if sims[i] >= min_cos]


def rank_relations(rels: List[Dict], seeds: Sequence[int], hop1: Sequence[int]) -> List[Dict]:
    seed_set, hop1_set = set(seeds), set(hop1)

    def score(r: Dict) -> float:
        s = float(r["weight"]) + 0.5 * float(r["evidence_count"])
        ends = {r["head_id"], r["tail_id"]}
        if ends <= seed_set:
            s += 3.0
        elif ends & seed_set:
            s += 1.5
        elif ends & hop1_set:
            s += 0.5
        return s
    return sorted(rels, key=lambda r: (-score(r), r["id"]))


def format_relation(r: Dict) -> str:
    return f"- {r['head']} —{r['relation']}→ {r['tail']}"


def build_block(store: KGStore, query: str, count_tokens: Callable[[str], int], max_tokens: int = 500,
                embed_query: Optional[Callable[[str], Optional[np.ndarray]]] = None,
                name_index: Optional[Sequence[Tuple[str, int]]] = None,
                vectors: Optional[Tuple[List[int], Optional[np.ndarray]]] = None) -> str:
    names = name_index if name_index is not None else store.name_index()
    if not names:
        return ""
    seeds = match_mentions(query, names)
    if embed_query is not None:
        ids, mat = vectors if vectors is not None else store.vector_matrix()
        if mat is not None and ids:
            try:
                qv = embed_query(query)
            except Exception:
                qv = None
            for eid in embedding_neighbours(qv, ids, mat):
                if eid not in seeds:
                    seeds.append(eid)
    if not seeds:
        return ""
    rels = store.relations_touching(seeds)
    hop1 = sorted({x for r in rels for x in (r["head_id"], r["tail_id"])} - set(seeds))
    if len(rels) < TWO_HOP_BELOW and hop1:
        known = {r["id"] for r in rels}
        rels += [r for r in store.relations_touching(hop1) if r["id"] not in known]
    if not rels:
        return ""
    lines: List[str] = []
    seen = set()
    block = BLOCK_HEADER
    for r in rank_relations(rels, seeds, hop1):
        line = format_relation(r)
        if line in seen:
            continue
        candidate = block + "\n" + line
        if count_tokens(candidate) > max_tokens:
            break
        seen.add(line)
        lines.append(line)
        block = candidate
        if len(lines) >= MAX_LINES:
            break
    return block if lines else ""


__all__ = ["build_block", "match_mentions", "embedding_neighbours", "BLOCK_HEADER"]
