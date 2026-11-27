"""Small JSON API for the face data. Listens on 127.0.0.1 only; the LAN API forwards /faces/* to it.

  python -m faces.api
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import cluster, config, db, search

HOST = os.environ.get("FACES_HOST", "127.0.0.1")
PORT = int(os.environ.get("FACES_PORT", "8788"))
MAX_BODY = 4_000_000

_cluster_lock = threading.Lock()


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def chunks(seq, n=400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def clean_name(name) -> str:
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise HttpError(400, "Enter a name.")
    return name


def int_list(v, limit=20000):
    if not isinstance(v, list):
        raise HttpError(400, "bad request")
    try:
        return [int(x) for x in v][:limit]
    except (TypeError, ValueError):
        raise HttpError(400, "bad request")


def paths_for_faces(conn, face_ids):
    out = []
    for part in chunks(face_ids):
        qs = ",".join("?" * len(part))
        out += [r[0] for r in conn.execute(f"SELECT DISTINCT path FROM faces WHERE id IN ({qs})", part)]
    return list(dict.fromkeys(out))


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------------------------------------
def status(conn):
    def js(key):
        raw = db.kv_get(conn, key)
        return json.loads(raw) if raw else None

    scan = js("scan_state")
    if scan and scan.get("state") in ("running", "listing") and time.time() - scan.get("heartbeat", 0) > 120:
        scan["state"] = "stalled"
    backend = os.environ.get("FACES_BACKEND", "opencv")
    return {
        "photos": one(conn, "SELECT COUNT(*) FROM photos"),
        "faces_scanned": one(conn, "SELECT COUNT(*) FROM photos WHERE faces_done = 1"),
        "faces": one(conn, "SELECT COUNT(*) FROM faces"),
        "named_faces": one(conn, "SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL"),
        "unnamed_faces": one(conn, "SELECT COUNT(*) FROM faces WHERE person_id IS NULL AND ignored = 0"),
        "ungrouped_faces": one(conn, "SELECT COUNT(*) FROM faces WHERE person_id IS NULL AND ignored = 0 "
                                     "AND cluster_id IS NULL"),
        "clusters": one(conn, "SELECT COUNT(DISTINCT cluster_id) FROM faces WHERE person_id IS NULL "
                              "AND ignored = 0 AND cluster_id IS NOT NULL"),
        "people": one(conn, "SELECT COUNT(*) FROM persons"),
        "scan": scan,
        "cluster": js("cluster_state"),
        "models_ok": backend == "fake" or config.models_present(),
        "models_dir": config.models_dir(),
        "fts": db.has_fts(conn),
    }


def run_cluster():
    """Runs in a background thread. Only one at a time."""
    if not _cluster_lock.acquire(blocking=False):
        return
    conn = db.connect()
    try:
        db.kv_set(conn, "cluster_state", json.dumps({"state": "running", "started": time.time()}))
        conn.commit()
        res = cluster.recluster(conn)
        db.kv_set(conn, "cluster_state", json.dumps({"state": "idle", "finished": time.time(), **res}))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        db.kv_set(conn, "cluster_state", json.dumps({"state": "error", "error": str(e)[:200]}))
        conn.commit()
    finally:
        conn.close()
        _cluster_lock.release()


def list_clusters(conn, limit, offset, min_size):
    cond = "person_id IS NULL AND ignored = 0 AND cluster_id IS NOT NULL"
    total = one(conn, f"SELECT COUNT(*) FROM (SELECT cluster_id FROM faces WHERE {cond} "
                      f"GROUP BY cluster_id HAVING COUNT(*) >= ?)", min_size)
    rows = conn.execute(
        f"SELECT cluster_id, COUNT(*) AS n, COUNT(DISTINCT path) AS photos FROM faces WHERE {cond} "
        f"GROUP BY cluster_id HAVING n >= ? ORDER BY n DESC, cluster_id LIMIT ? OFFSET ?",
        (min_size, limit, offset)).fetchall()
    cents = cluster.person_centroids(conn)
    items = []
    for r in rows:
        top = conn.execute("SELECT id, emb FROM faces WHERE cluster_id = ? AND person_id IS NULL AND ignored = 0 "
                           "ORDER BY score DESC LIMIT 30", (r["cluster_id"],)).fetchall()
        sug = cluster.suggest(cents, [cluster.vec(t["emb"]) for t in top])
        items.append({
            "id": r["cluster_id"], "faces": r["n"], "photos": r["photos"],
            "sample": [t["id"] for t in top[:6]],
            "suggestion": {"person_id": cents[0][sug[0]], "name": cents[1][sug[0]], "score": round(sug[1], 3)}
            if sug else None,
        })
    return {"total": total, "items": items}


def name_faces(conn, name, face_ids=None, cluster_id=None):
    name = clean_name(name)
    if face_ids is None:
        if cluster_id is None:
            raise HttpError(400, "bad request")
        face_ids = [r[0] for r in conn.execute(
            "SELECT id FROM faces WHERE cluster_id = ? AND person_id IS NULL AND ignored = 0", (int(cluster_id),))]
    if not face_ids:
        raise HttpError(404, "That group no longer exists. Reload the page.")
    row = conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()
    if row:
        pid = row["id"]
    else:
        conn.execute("INSERT INTO persons(name, created_at) VALUES (?, ?)", (name, time.time()))
        pid = conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()["id"]
    paths = paths_for_faces(conn, face_ids)
    for part in chunks(face_ids):
        qs = ",".join("?" * len(part))
        conn.execute(f"UPDATE faces SET person_id = ?, cluster_id = NULL, ignored = 0 WHERE id IN ({qs})", [pid] + part)
    conn.commit()
    search.rebuild(conn, paths)
    return {"person_id": pid, "assigned": len(face_ids)}


def set_faces(conn, face_ids, **cols):
    paths = paths_for_faces(conn, face_ids)
    sets = ", ".join(f"{k} = ?" for k in cols)
    for part in chunks(face_ids):
        qs = ",".join("?" * len(part))
        conn.execute(f"UPDATE faces SET {sets} WHERE id IN ({qs})", list(cols.values()) + part)
    conn.commit()
    search.rebuild(conn, paths)


def list_people(conn):
    rows = conn.execute(
        "SELECT pe.id, pe.name, COUNT(f.id) AS faces, COUNT(DISTINCT f.path) AS photos, "
        "(SELECT f2.id FROM faces f2 WHERE f2.person_id = pe.id ORDER BY f2.score DESC LIMIT 1) AS sample "
        "FROM persons pe LEFT JOIN faces f ON f.person_id = pe.id GROUP BY pe.id "
        "ORDER BY photos DESC, pe.name COLLATE NOCASE").fetchall()
    return {"items": [dict(r) for r in rows]}


def photo_detail(conn, path):
    p = conn.execute("SELECT * FROM photos WHERE path = ?", (path,)).fetchone()
    if not p:
        raise HttpError(404, "That photo hasn't been scanned yet.")
    d = search.describe(conn, path)
    faces = conn.execute(
        "SELECT f.id, f.x, f.y, f.w, f.h, f.person_id, pe.name, f.ignored FROM faces f "
        "LEFT JOIN persons pe ON pe.id = f.person_id WHERE f.path = ? ORDER BY f.x", (path,)).fetchall()
    return {
        "path": path, "folder": p["folder"], "name": path.rsplit("/", 1)[-1], "taken_at": p["taken_at"],
        "date_source": p["date_source"], "lat": p["lat"], "lon": p["lon"], "gps_place": p["place"],
        "folder_place": d["folder_place"], "place": d["place"], "note": p["note"] or "",
        "summary": d["summary"], "people": d["people"], "faces": [dict(f) for f in faces],
        "width": p["width"], "height": p["height"],
    }


def rebuild_folder(conn, folder):
    paths = [r[0] for r in conn.execute(
        "SELECT path FROM photos WHERE folder = ? OR folder LIKE ? ESCAPE '\\'", (folder, search._like(folder) + "/%"))]
    search.rebuild(conn, paths)
    return len(paths)


# ---------------------------------------------------------------------------------------------
def handle(method, path, qs, body, conn):
    """Returns (status, payload, headers). payload is a dict (JSON) or bytes."""
    q = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731
    qint = lambda k, d: int(q(k, str(d)) or d)  # noqa: E731

    if method == "GET":
        if path == "/faces/status":
            return 200, status(conn), {}
        if path == "/faces/clusters":
            return 200, list_clusters(conn, min(qint("limit", 12), 50), qint("offset", 0), max(qint("min_size", 1), 1)), {}
        m = re.fullmatch(r"/faces/cluster/(\d+)", path)
        if m:
            rows = conn.execute(
                "SELECT id, path FROM faces WHERE cluster_id = ? AND person_id IS NULL AND ignored = 0 "
                "ORDER BY score DESC LIMIT ? OFFSET ?", (int(m.group(1)), min(qint("limit", 60), 200), qint("offset", 0))
            ).fetchall()
            total = one(conn, "SELECT COUNT(*) FROM faces WHERE cluster_id = ? AND person_id IS NULL AND ignored = 0",
                        int(m.group(1)))
            return 200, {"total": total, "faces": [dict(r) for r in rows]}, {}
        m = re.fullmatch(r"/faces/crop/(\d+)", path)
        if m:
            row = conn.execute("SELECT crop FROM faces WHERE id = ?", (int(m.group(1)),)).fetchone()
            if not row or not row["crop"]:
                raise HttpError(404, "no such face")
            return 200, bytes(row["crop"]), {"content-type": "image/jpeg", "cache-control": "private, max-age=86400"}
        if path == "/faces/people":
            return 200, list_people(conn), {}
        m = re.fullmatch(r"/faces/person/(\d+)", path)
        if m:
            pid = int(m.group(1))
            person = conn.execute("SELECT id, name FROM persons WHERE id = ?", (pid,)).fetchone()
            if not person:
                raise HttpError(404, "no such person")
            rows = conn.execute("SELECT id, path FROM faces WHERE person_id = ? ORDER BY score DESC LIMIT ? OFFSET ?",
                                (pid, min(qint("limit", 60), 200), qint("offset", 0))).fetchall()
            total = one(conn, "SELECT COUNT(*) FROM faces WHERE person_id = ?", pid)
            photos = one(conn, "SELECT COUNT(DISTINCT path) FROM faces WHERE person_id = ?", pid)
            return 200, {"id": pid, "name": person["name"], "faces_total": total, "photos": photos,
                         "faces": [dict(r) for r in rows]}, {}
        if path == "/faces/photo":
            return 200, photo_detail(conn, q("path")), {}
        if path == "/faces/folder":
            row = conn.execute("SELECT place, note FROM folder_labels WHERE folder = ?", (q("folder"),)).fetchone()
            return 200, {"folder": q("folder"), "place": (row["place"] if row else "") or "",
                         "note": (row["note"] if row else "") or ""}, {}
        if path == "/faces/search":
            people = [int(x) for x in q("people").split(",") if x.strip().isdigit()]
            total, items = search.search(
                conn, q("q"), people, q("year_from") or None, q("year_to") or None, q("folder"),
                min(qint("limit", 60), 200), qint("offset", 0))
            return 200, {"total": total, "items": items}, {}
    elif method == "POST":
        if path == "/faces/cluster":
            threading.Thread(target=run_cluster, daemon=True).start()
            return 202, {"started": True}, {}
        if path == "/faces/name":
            ids = int_list(body["face_ids"]) if body.get("face_ids") is not None else None
            return 200, name_faces(conn, body.get("name"), ids, body.get("cluster_id")), {}
        if path == "/faces/ignore":
            if body.get("cluster_id") is not None:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM faces WHERE cluster_id = ? AND person_id IS NULL", (int(body["cluster_id"]),))]
            else:
                ids = int_list(body.get("face_ids"))
            set_faces(conn, ids, ignored=1, person_id=None, cluster_id=None)
            return 200, {"ignored": len(ids)}, {}
        if path == "/faces/unassign":
            ids = int_list(body.get("face_ids"))
            set_faces(conn, ids, person_id=None, cluster_id=None)
            return 200, {"unassigned": len(ids)}, {}
        if path == "/faces/people/rename":
            name = clean_name(body.get("name"))
            pid = int(body.get("id"))
            clash = conn.execute("SELECT id FROM persons WHERE name = ? AND id != ?", (name, pid)).fetchone()
            if clash:
                raise HttpError(409, f"There is already a person called {name}. Use Merge instead.")
            conn.execute("UPDATE persons SET name = ? WHERE id = ?", (name, pid))
            paths = [r[0] for r in conn.execute("SELECT DISTINCT path FROM faces WHERE person_id = ?", (pid,))]
            conn.commit()
            search.rebuild(conn, paths)
            return 200, {"ok": True}, {}
        if path == "/faces/people/merge":
            src, dst = int(body.get("from")), int(body.get("into"))
            if src == dst:
                raise HttpError(400, "Pick a different person.")
            if not conn.execute("SELECT 1 FROM persons WHERE id = ?", (dst,)).fetchone():
                raise HttpError(404, "no such person")
            paths = [r[0] for r in conn.execute("SELECT DISTINCT path FROM faces WHERE person_id = ?", (src,))]
            conn.execute("UPDATE faces SET person_id = ? WHERE person_id = ?", (dst, src))
            conn.execute("DELETE FROM persons WHERE id = ?", (src,))
            conn.commit()
            search.rebuild(conn, paths)
            return 200, {"ok": True}, {}
        if path == "/faces/people/delete":
            pid = int(body.get("id"))
            paths = [r[0] for r in conn.execute("SELECT DISTINCT path FROM faces WHERE person_id = ?", (pid,))]
            conn.execute("UPDATE faces SET person_id = NULL, cluster_id = NULL WHERE person_id = ?", (pid,))
            conn.execute("DELETE FROM persons WHERE id = ?", (pid,))
            conn.commit()
            search.rebuild(conn, paths)
            return 200, {"ok": True}, {}
        if path == "/faces/photo/note":
            note = str(body.get("note") or "").strip()[:2000]
            if conn.execute("UPDATE photos SET note = ? WHERE path = ?", (note or None, body.get("path"))).rowcount == 0:
                raise HttpError(404, "no such photo")
            conn.commit()
            search.rebuild(conn, [body.get("path")])
            return 200, {"ok": True}, {}
        if path == "/faces/folder":
            folder = str(body.get("folder") or "").strip("/")
            if not folder:
                raise HttpError(400, "bad request")
            place = str(body.get("place") or "").strip()[:200]
            note = str(body.get("note") or "").strip()[:2000]
            if place or note:
                conn.execute("INSERT INTO folder_labels(folder, place, note) VALUES (?, ?, ?) "
                             "ON CONFLICT(folder) DO UPDATE SET place = excluded.place, note = excluded.note",
                             (folder, place or None, note or None))
            else:
                conn.execute("DELETE FROM folder_labels WHERE folder = ?", (folder,))
            conn.commit()
            return 200, {"ok": True, "photos": rebuild_folder(conn, folder)}, {}
    raise HttpError(404, "not found")


class Handler(BaseHTTPRequestHandler):
    server_version = "photo-triage-faces"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status, payload, headers):
        if isinstance(payload, (bytes, bytearray)):
            data, ctype = bytes(payload), headers.get("content-type", "application/octet-stream")
        else:
            data, ctype = json.dumps(payload).encode(), "application/json"
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in headers.items():
            if k != "content-type":
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _run(self, method):
        conn = None
        try:
            u = urlparse(self.path)
            body = {}
            if method == "POST":
                n = int(self.headers.get("content-length") or 0)
                if n > MAX_BODY:
                    raise HttpError(413, "request too large")
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    raise HttpError(400, "bad json")
                if not isinstance(body, dict):
                    raise HttpError(400, "bad json")
            conn = db.connect()
            self._send(*handle(method, u.path, parse_qs(u.query), body, conn))
        except HttpError as e:
            self._send(e.status, {"error": e.message}, {})
        except (KeyError, ValueError, TypeError) as e:
            self._send(400, {"error": f"bad request ({e.__class__.__name__}: {e})"}, {})
        except Exception as e:  # noqa: BLE001
            print("error:", repr(e), file=sys.stderr, flush=True)
            self._send(500, {"error": "internal error"}, {})
        finally:
            if conn:
                conn.close()

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")


def main():
    db.connect(init=True).close()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"photo-triage face service on http://{HOST}:{PORT}, database {db.default_path()}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
