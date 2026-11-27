"""Finding faces and describing them as 128 numbers.

The real backend uses OpenCV's built-in YuNet (detector) and SFace (recognizer) ONNX models.
They run on a plain CPU and need very little memory.

A 'fake' backend exists only for testing the rest of the pipeline without any models.
"""
from __future__ import annotations

import os
import zlib
from dataclasses import dataclass

import cv2
import numpy as np

from . import config


@dataclass
class Det:
    x: float          # box, as a fraction of the photo width/height
    y: float
    w: float
    h: float
    score: float
    emb: np.ndarray   # float32[128], length 1
    crop: bytes       # small JPEG for the naming screen


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)


def make_crop(img, x, y, w, h, out=128) -> bytes:
    """Square crop around a face (with some margin), as JPEG bytes."""
    ih, iw = img.shape[:2]
    side = max(w, h) * 1.6
    cx, cy = x + w / 2.0, y + h / 2.0
    x0, y0 = int(round(cx - side / 2)), int(round(cy - side / 2))
    x1, y1 = int(round(cx + side / 2)), int(round(cy + side / 2))
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(iw, x1), min(ih, y1)
    if cx1 <= cx0 or cy1 <= cy0:
        return b""
    piece = img[cy0:cy1, cx0:cx1]
    ph, pw = piece.shape[:2]
    s = max(ph, pw)
    canvas = np.zeros((s, s, 3), dtype=np.uint8)
    canvas[(s - ph) // 2:(s - ph) // 2 + ph, (s - pw) // 2:(s - pw) // 2 + pw] = piece
    canvas = cv2.resize(canvas, (out, out), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else b""


class OpenCVBackend:
    name = "opencv"

    def __init__(self, max_side: int = 1920, min_face: int = 40, score: float = 0.8):
        d = config.models_dir()
        det_path, rec_path = os.path.join(d, config.YUNET), os.path.join(d, config.SFACE)
        if not config.models_present():
            raise SystemExit(
                f"Face models not found in {d}. Run faces/setup_models.sh first "
                "(it downloads two small ONNX files)."
            )
        self.max_side, self.min_face = max_side, min_face
        self.det = cv2.FaceDetectorYN.create(det_path, "", (320, 320), score, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(rec_path, "")

    def process(self, img, path: str = ""):
        h, w = img.shape[:2]
        scale = min(1.0, self.max_side / float(max(h, w)))
        small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) \
            if scale < 1.0 else img
        sh, sw = small.shape[:2]
        self.det.setInputSize((sw, sh))
        _, found = self.det.detect(small)
        out = []
        if found is None:
            return out
        for row in found:
            x, y, fw, fh = (float(v) for v in row[:4])
            if min(fw, fh) < self.min_face:
                continue
            try:
                aligned = self.rec.alignCrop(small, row)
                feat = self.rec.feature(aligned)
            except cv2.error:
                continue
            emb = _unit(np.asarray(feat, dtype=np.float32).reshape(-1))
            crop = make_crop(small, x, y, fw, fh)
            out.append(Det(x / sw, y / sh, fw / sw, fh / sh, float(row[-1]), emb, crop))
        return out


class FakeBackend:
    """For tests. A file named 'alice+bob_001.jpg' contains the 'faces' of alice and bob."""
    name = "fake"

    def process(self, img, path: str = ""):
        stem = os.path.splitext(os.path.basename(path))[0].split("_")[0]
        if not stem or stem.startswith("noface"):
            return []
        out = []
        for i, who in enumerate(stem.split("+")):
            base = np.random.RandomState(zlib.crc32(who.encode())).randn(128).astype(np.float32)
            noise = np.random.RandomState(zlib.crc32((path + who).encode())).randn(128).astype(np.float32) * 0.03
            emb = _unit(_unit(base) + noise)
            x, y, w, h = 0.1 + 0.3 * i, 0.2, 0.2, 0.25
            ih, iw = img.shape[:2]
            crop = make_crop(img, x * iw, y * ih, w * iw, h * ih)
            out.append(Det(x, y, w, h, 0.95, emb, crop))
        return out


def get_backend(max_side: int = 1920):
    kind = os.environ.get("FACES_BACKEND", "opencv")
    if kind == "fake":
        return FakeBackend()
    return OpenCVBackend(max_side=max_side)
