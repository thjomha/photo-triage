"""Which folders to scan. Mirrors the LAN API: same CONFIG_FILE / PHOTO_ROOT settings and path rules."""
from __future__ import annotations

import json
import os

HIDDEN_NAMES = {"_trash", "@eaDir", "#recycle", "Thumbs.db"}
# Formats OpenCV can read. HEIC/AVIF photos are skipped.
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def is_hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("$") or name in HIDDEN_NAMES


def load_shares():
    """Returns [(share_name, absolute_root)]. share_name is '' in single-folder mode."""
    cfg = os.environ.get("CONFIG_FILE")
    if cfg and os.path.exists(cfg):
        with open(cfg, encoding="utf-8") as fh:
            data = json.load(fh)
        return [(name, os.path.abspath(p)) for name, p in (data.get("shares") or {}).items()]
    root = os.environ.get("PHOTO_ROOT")
    if root:
        return [("", os.path.abspath(root))]
    raise SystemExit("Set CONFIG_FILE (several shares) or PHOTO_ROOT (one folder), the same as for the LAN API.")


def _under(app_dir: str, prefix: str) -> bool:
    return not prefix or app_dir == prefix or app_dir.startswith(prefix + "/")


def _on_the_way(app_dir: str, prefix: str) -> bool:
    return not app_dir or prefix.startswith(app_dir + "/")


def iter_photos(shares, prefix: str = ""):
    """Yield (app_path, absolute_path) for every photo, optionally only below an app-path prefix."""
    prefix = prefix.strip("/")
    for name, root in shares:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not is_hidden(d))
            rel = os.path.relpath(dirpath, root)
            rel = "" if rel == "." else rel.replace(os.sep, "/")
            app_dir = "/".join(x for x in (name, rel) if x)
            if not (_under(app_dir, prefix) or _on_the_way(app_dir, prefix)):
                dirnames[:] = []
                continue
            if not _under(app_dir, prefix):
                continue
            for fname in sorted(filenames):
                if is_hidden(fname) or os.path.splitext(fname)[1].lower() not in PHOTO_EXT:
                    continue
                yield "/".join(x for x in (app_dir, fname) if x), os.path.join(dirpath, fname)


def models_dir() -> str:
    return os.environ.get("FACES_MODELS") or os.path.join(os.path.expanduser("~"), "photo-triage-data", "models")


YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"


def models_present() -> bool:
    """True when both ONNX models are downloaded (sizes guard against a Git-LFS pointer file)."""
    d = models_dir()
    det, rec = os.path.join(d, YUNET), os.path.join(d, SFACE)
    try:
        return os.path.getsize(det) > 100_000 and os.path.getsize(rec) > 5_000_000
    except OSError:
        return False
