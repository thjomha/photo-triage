"""Scan photos for dates, GPS and faces. Safe to stop and restart: finished photos are skipped.

  python -m faces.scan                       # everything
  python -m faces.scan --folder "Camera Archives/Z33 01 Canmore 2026"    # just one folder (good first test)
  python -m faces.scan --no-faces            # only dates/GPS/search text (needs no face models)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time

from . import config, db, exif, search

STOP = False


def _on_signal(_sig, _frm):
    global STOP
    STOP = True
    print("\nStopping after the current photo…", flush=True)


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _state(conn, **kw):
    db.kv_set(conn, "scan_state", json.dumps(kw))
    conn.commit()


def upsert_photo(conn, app_path, st, width, height, taken, source, lat, lon, faces_done, face_count, error=None):
    conn.execute(
        """INSERT INTO photos (path, folder, size, mtime, width, height, taken_at, date_source, lat, lon,
                               scanned_at, face_count, faces_done, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             size = excluded.size, mtime = excluded.mtime, width = excluded.width, height = excluded.height,
             taken_at = excluded.taken_at, date_source = excluded.date_source,
             place = CASE WHEN photos.lat IS excluded.lat AND photos.lon IS excluded.lon THEN photos.place ELSE NULL END,
             lat = excluded.lat, lon = excluded.lon, scanned_at = excluded.scanned_at,
             face_count = excluded.face_count, faces_done = excluded.faces_done, error = excluded.error""",
        (app_path, app_path.rsplit("/", 1)[0] if "/" in app_path else "", st.st_size, st.st_mtime, width, height,
         taken, source, lat, lon, time.time(), face_count, faces_done, error))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default="", help="only scan below this app path, e.g. 'Camera Archives/2015'")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many photos (0 = no limit)")
    ap.add_argument("--max-side", type=int, default=1920, help="shrink photos to this size before looking for faces")
    ap.add_argument("--sleep", type=float, default=0.0, help="pause between photos, to go easy on the CPU")
    ap.add_argument("--rescan", action="store_true", help="scan again even if the photo was scanned before")
    ap.add_argument("--no-faces", action="store_true", help="only read dates and GPS (no face models needed)")
    ap.add_argument("--prune", action="store_true", help="forget photos whose files no longer exist")
    ap.add_argument("--force-prune", action="store_true", help="allow --prune to forget more than half the photos")
    args = ap.parse_args(argv)

    conn = db.connect(init=True)
    lock = open(db.default_path() + ".scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another scan is already running.", file=sys.stderr)
        return 1

    backend = None
    if not args.no_faces:
        import cv2  # noqa: F401  (imported here so --no-faces works without OpenCV)
        from .backend import get_backend
        backend = get_backend(args.max_side)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    shares = config.load_shares()
    started = time.time()
    _state(conn, state="listing", started=started, heartbeat=time.time())
    print("Listing photos…", flush=True)

    known = {r["path"]: (r["size"], r["mtime"], r["faces_done"]) for r in conn.execute(
        "SELECT path, size, mtime, faces_done FROM photos")}
    todo, seen = [], set()
    for app_path, abs_path in config.iter_photos(shares, args.folder):
        seen.add(app_path)
        try:
            st = os.stat(abs_path)
        except OSError:
            continue
        old = known.get(app_path)
        changed = old is None or old[0] != st.st_size or abs((old[1] or 0) - st.st_mtime) > 1
        need_faces = (not args.no_faces) and (old is None or changed or not old[2])
        if args.rescan or changed or need_faces:
            todo.append((app_path, abs_path, st, changed or args.rescan))
    if args.limit:
        todo = todo[:args.limit]
    total = len(todo)
    print(f"{len(seen)} photos found, {total} to scan.", flush=True)

    done = faces_found = errors = 0
    pending = []
    last_beat = 0.0
    for app_path, abs_path, st, changed in todo:
        if STOP:
            break
        name = os.path.basename(abs_path)
        meta = exif.read_exif(abs_path)
        taken, source = exif.resolve_date(name, meta["date"], st.st_mtime)
        try:
            if args.no_faces:
                if changed:
                    conn.execute("DELETE FROM faces WHERE path = ?", (app_path,))
                existing = conn.execute("SELECT faces_done, face_count, width, height FROM photos WHERE path = ?",
                                        (app_path,)).fetchone()
                keep_done = (existing["faces_done"] if existing and not changed else 0)
                upsert_photo(conn, app_path, st, existing["width"] if existing else None,
                             existing["height"] if existing else None, taken, source, meta["lat"], meta["lon"],
                             keep_done, existing["face_count"] if existing and not changed else 0)
            else:
                import cv2
                flags = cv2.IMREAD_COLOR
                img = cv2.imread(abs_path, flags)
                if img is None:
                    upsert_photo(conn, app_path, st, None, None, taken, source, meta["lat"], meta["lon"], 1, 0,
                                 "could not read image")
                    conn.execute("DELETE FROM faces WHERE path = ?", (app_path,))
                    errors += 1
                else:
                    h, w = img.shape[:2]
                    dets = backend.process(img, app_path)
                    conn.execute("DELETE FROM faces WHERE path = ?", (app_path,))
                    upsert_photo(conn, app_path, st, w, h, taken, source, meta["lat"], meta["lon"], 1, len(dets))
                    for d in dets:
                        conn.execute(
                            "INSERT INTO faces (path, x, y, w, h, score, emb, crop) VALUES (?,?,?,?,?,?,?,?)",
                            (app_path, d.x, d.y, d.w, d.h, d.score, d.emb.astype("float32").tobytes(), d.crop))
                    faces_found += len(dets)
        except Exception as e:  # one bad photo must never stop an overnight scan
            errors += 1
            print(f"  ! {app_path}: {e}", flush=True)
            upsert_photo(conn, app_path, st, None, None, taken, source, meta["lat"], meta["lon"], 1, 0, str(e)[:200])
        pending.append(app_path)
        done += 1

        if len(pending) >= 25:
            search.rebuild(conn, pending)   # also commits
            pending = []
        now = time.time()
        if now - last_beat > 5:
            last_beat = now
            rate = done / max(now - started, 1e-6)
            eta = _fmt_eta((total - done) / rate) if rate > 0 else "?"
            _state(conn, state="running", done=done, total=total, faces=faces_found, errors=errors,
                   started=started, heartbeat=now, current=app_path, eta=eta)
            print(f"[{done}/{total}] {rate:.1f} photos/s, {faces_found} faces, ETA {eta}", flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    if pending:
        search.rebuild(conn, pending)

    if args.prune and not STOP and not args.folder:
        # Only forget photos of a share that is readable right now. A drive that is unmounted or
        # empty looks exactly like "all photos deleted", and forgetting would throw away the names.
        for name, root in shares:
            prefix = (name + "/") if name else ""
            known_here = [r["path"] for r in conn.execute("SELECT path FROM photos")
                          if not prefix or r["path"].startswith(prefix)]
            label = name or root
            if not os.path.isdir(root) or not any(p in seen for p in known_here) and known_here:
                print(f"Not pruning '{label}': it looks unmounted or empty.")
                continue
            gone = [p for p in known_here if p not in seen]
            if known_here and len(gone) > len(known_here) / 2 and not args.force_prune:
                print(f"Not pruning '{label}': {len(gone)} of {len(known_here)} photos look missing. "
                      "Use --force-prune if that's right.")
                continue
            for p in gone:
                row = conn.execute("SELECT id FROM photos WHERE path = ?", (p,)).fetchone()
                if row:
                    conn.execute("DELETE FROM search WHERE rowid = ?", (row["id"],))
                conn.execute("DELETE FROM photos WHERE path = ?", (p,))
            conn.commit()
            print(f"Forgot {len(gone)} photos of '{label}' whose files are gone.")

    _state(conn, state="stopped" if STOP else "idle", done=done, total=total, faces=faces_found, errors=errors,
           started=started, finished=time.time(), heartbeat=time.time())
    print(f"Done: {done} photos, {faces_found} faces, {errors} problems, {_fmt_eta(time.time() - started)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
