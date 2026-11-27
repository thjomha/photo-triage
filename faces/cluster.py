"""Grouping look-alike unnamed faces so you can name a whole group at once.

Greedy "leader" clustering on face embeddings, then two refinement passes. The threshold is
deliberately strict: it's better to get several pure groups for one person (you merge them by
giving them the same name) than one group that mixes two people.
"""
from __future__ import annotations

import os
import time

import numpy as np

from .db import kv_get, kv_set

THRESHOLD = float(os.environ.get("FACES_CLUSTER_THRESHOLD", "0.5"))
SUGGEST = float(os.environ.get("FACES_SUGGEST_THRESHOLD", "0.45"))


def vec(blob) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _norm_rows(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return m / n


def _alloc_cluster_ids(conn, count: int) -> int:
    start = int(kv_get(conn, "next_cluster_id", "1"))
    kv_set(conn, "next_cluster_id", str(start + count))
    return start


def recluster(conn, threshold: float = THRESHOLD) -> dict:
    """Group all faces that have no person yet. Cluster ids are never reused, so a page that was
    loaded before this ran can't accidentally name the wrong faces."""
    rows = conn.execute(
        "SELECT id, emb FROM faces WHERE person_id IS NULL AND ignored = 0 ORDER BY score DESC, id").fetchall()
    conn.execute("UPDATE faces SET cluster_id = NULL WHERE person_id IS NULL")
    n = len(rows)
    if n == 0:
        conn.commit()
        return {"faces": 0, "clusters": 0}
    ids = [r["id"] for r in rows]
    X = _norm_rows(np.vstack([vec(r["emb"]) for r in rows]).astype(np.float32))

    # leader pass: best faces (highest detection score) found first, so they become the centres
    cap = 256
    cents = np.zeros((cap, X.shape[1]), dtype=np.float32)
    sums = np.zeros_like(cents)
    C = 0
    assign = np.empty(n, dtype=np.int64)
    for i in range(n):
        if C:
            sims = cents[:C] @ X[i]
            j = int(np.argmax(sims))
            if sims[j] >= threshold:
                assign[i] = j
                sums[j] += X[i]
                nrm = np.linalg.norm(sums[j])
                cents[j] = sums[j] / nrm if nrm else sums[j]
                continue
        if C == cap:
            cap *= 2
            cents = np.concatenate([cents, np.zeros_like(cents)])
            sums = np.concatenate([sums, np.zeros_like(sums)])
        cents[C] = X[i]
        sums[C] = X[i]
        assign[i] = C
        C += 1

    # refinement: move every face to its closest centre, recompute centres (twice)
    cents = cents[:C].copy()
    for _ in range(2):
        for s in range(0, n, 4000):
            assign[s:s + 4000] = np.argmax(X[s:s + 4000] @ cents.T, axis=1)
        sums = np.zeros_like(cents)
        np.add.at(sums, assign, X)
        cents = _norm_rows(sums)
        cents[np.linalg.norm(sums, axis=1) == 0] = 0

    used = sorted(set(int(a) for a in assign))
    start = _alloc_cluster_ids(conn, len(used))
    remap = {old: start + k for k, old in enumerate(used)}
    conn.executemany("UPDATE faces SET cluster_id = ? WHERE id = ?",
                     [(remap[int(a)], fid) for a, fid in zip(assign, ids)])
    conn.commit()
    return {"faces": n, "clusters": len(used)}


def person_centroids(conn):
    """(ids, names, matrix) with one unit vector per named person, from their best faces."""
    ids, names, rows = [], [], []
    for p in conn.execute("SELECT id, name FROM persons ORDER BY id").fetchall():
        embs = [vec(r["emb"]) for r in conn.execute(
            "SELECT emb FROM faces WHERE person_id = ? ORDER BY score DESC LIMIT 60", (p["id"],))]
        if embs:
            ids.append(p["id"])
            names.append(p["name"])
            rows.append(np.mean(np.vstack(embs), axis=0))
    if not rows:
        return [], [], np.zeros((0, 128), dtype=np.float32)
    return ids, names, _norm_rows(np.vstack(rows).astype(np.float32))


def suggest(cents, face_embs):
    """Best-matching named person for a group of faces: (person_index, similarity) or None."""
    ids, names, mat = cents
    if not len(ids) or not len(face_embs):
        return None
    c = _norm_rows(np.mean(np.vstack(face_embs), axis=0, keepdims=True).astype(np.float32))[0]
    sims = mat @ c
    j = int(np.argmax(sims))
    return (j, float(sims[j])) if sims[j] >= SUGGEST else None
