"""Descriptions (who / where / when) and full-text search over them. Files are never renamed."""
from __future__ import annotations

import datetime
import os
import re
from collections import defaultdict

from .db import has_fts

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def _labels(conn):
    return [dict(r) for r in conn.execute("SELECT folder, place, note FROM folder_labels")]


def _folder_labels(labels, folder):
    mine = [l for l in labels if folder == l["folder"] or folder.startswith(l["folder"] + "/")]
    mine.sort(key=lambda l: len(l["folder"]))
    place = next((l["place"] for l in reversed(mine) if l["place"]), None)  # most specific wins
    notes = [l["note"] for l in mine if l["note"]]
    return place, notes


def _date(taken_at):
    try:
        return datetime.datetime.strptime((taken_at or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def compose(photo, people, labels):
    """Build the display summary and the text that search looks in."""
    folder_place, folder_notes = _folder_labels(labels, photo["folder"])
    place = photo["place"] or folder_place
    d = _date(photo["taken_at"])
    date_text = f"{d.day} {MONTHS[d.month - 1][:3]} {d.year}" if d else ""

    parts = []
    if people:
        parts.append(", ".join(people))
    if place:
        parts.append(place)
    if date_text:
        parts.append(date_text)
    summary = " · ".join(parts)

    words = list(people)
    words += [p for p in (photo["place"], folder_place) if p]
    if d:
        words += [str(d.year), MONTHS[d.month - 1], MONTHS[d.month - 1][:3], str(d.day), d.strftime("%Y-%m-%d")]
    words.append(photo["folder"].replace("/", " "))
    words.append(os.path.splitext(photo["path"].rsplit("/", 1)[-1])[0].replace("_", " "))
    if photo["note"]:
        words.append(photo["note"])
    words += folder_notes
    return {
        "people": list(people), "place": place, "folder_place": folder_place,
        "date_text": date_text, "summary": summary, "text": " ".join(words),
    }


def rebuild(conn, paths=None, chunk: int = 400):
    """Recompute the search text for some photos (or all of them)."""
    if paths is None:
        paths = [r[0] for r in conn.execute("SELECT path FROM photos")]
    paths = list(dict.fromkeys(paths))
    labels = _labels(conn)
    fts = has_fts(conn)
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        qs = ",".join("?" * len(part))
        photos = conn.execute(
            f"SELECT id, path, folder, taken_at, place, note FROM photos WHERE path IN ({qs})", part).fetchall()
        names = defaultdict(list)
        for r in conn.execute(
                f"SELECT f.path AS path, pe.name AS name FROM faces f JOIN persons pe ON pe.id = f.person_id "
                f"WHERE f.path IN ({qs}) ORDER BY pe.name COLLATE NOCASE", part):
            if r["name"] not in names[r["path"]]:
                names[r["path"]].append(r["name"])
        for p in photos:
            text = compose(p, names[p["path"]], labels)["text"]
            conn.execute("DELETE FROM search WHERE rowid = ?", (p["id"],))
            conn.execute("INSERT INTO search(rowid, text) VALUES (?, ?)", (p["id"], text))
    conn.commit()


def describe(conn, path):
    p = conn.execute("SELECT id, path, folder, taken_at, place, note FROM photos WHERE path = ?", (path,)).fetchone()
    if not p:
        return None
    people = [r[0] for r in conn.execute(
        "SELECT DISTINCT pe.name FROM faces f JOIN persons pe ON pe.id = f.person_id WHERE f.path = ? "
        "ORDER BY pe.name COLLATE NOCASE", (path,))]
    return compose(p, people, _labels(conn))


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(conn, q="", people=(), year_from=None, year_to=None, folder="", limit=60, offset=0):
    """Returns (total, items). Every word in q must match (as a prefix) somewhere in a photo's description."""
    fts = has_fts(conn)
    where, args, join = ["1=1"], [], ""
    tokens = re.findall(r"\w+", q or "", re.UNICODE)
    if tokens:
        if fts:
            join = "JOIN search ON search.rowid = p.id"
            where.append("search MATCH ?")
            args.append(" ".join('"%s"*' % t for t in tokens))
        else:
            join = "JOIN search ON search.rowid = p.id"
            for t in tokens:
                where.append("search.text LIKE ? ESCAPE '\\'")
                args.append("%" + _like(t) + "%")
    for pid in people:
        where.append("EXISTS (SELECT 1 FROM faces f WHERE f.path = p.path AND f.person_id = ?)")
        args.append(int(pid))
    if year_from:
        where.append("substr(p.taken_at, 1, 4) >= ?")
        args.append("%04d" % int(year_from))
    if year_to:
        where.append("substr(p.taken_at, 1, 4) <= ?")
        args.append("%04d" % int(year_to))
    if folder:
        where.append("(p.folder = ? OR p.folder LIKE ? ESCAPE '\\')")
        args += [folder, _like(folder) + "/%"]
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM photos p {join} WHERE {clause}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT p.id, p.path, p.folder, p.taken_at, p.date_source, p.place, p.note FROM photos p {join} "
        f"WHERE {clause} ORDER BY (p.taken_at IS NULL), p.taken_at DESC, p.path LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)]).fetchall()

    paths = [r["path"] for r in rows]
    names = defaultdict(list)
    if paths:
        qs = ",".join("?" * len(paths))
        for r in conn.execute(
                f"SELECT f.path AS path, pe.name AS name FROM faces f JOIN persons pe ON pe.id = f.person_id "
                f"WHERE f.path IN ({qs}) ORDER BY pe.name COLLATE NOCASE", paths):
            if r["name"] not in names[r["path"]]:
                names[r["path"]].append(r["name"])
    labels = _labels(conn)
    items = []
    for r in rows:
        c = compose(r, names[r["path"]], labels)
        items.append({
            "path": r["path"], "folder": r["folder"], "name": r["path"].rsplit("/", 1)[-1],
            "taken_at": r["taken_at"], "date_source": r["date_source"], "place": c["place"],
            "people": c["people"], "summary": c["summary"], "note": r["note"] or "",
        })
    return total, items
