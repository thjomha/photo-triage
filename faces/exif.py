"""Date taken and GPS position from a photo's EXIF data (with fallbacks for the date)."""
from __future__ import annotations

import datetime
import os
import re

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # big scans are fine, we only read the header here

_MIN_YEAR = 1990


def _num(v):
    try:
        return float(v)
    except Exception:
        try:
            return v[0] / v[1]
        except Exception:
            return None


def _dms(vals, ref):
    try:
        d, m, s = (_num(x) for x in vals)
    except Exception:
        return None
    if None in (d, m, s):
        return None
    deg = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W", b"S", b"W"):
        deg = -deg
    return deg


def _valid(y, mo, d, hh=0, mm=0, ss=0):
    try:
        dt = datetime.datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None
    if dt.year < _MIN_YEAR or dt.year > datetime.date.today().year + 1:
        return None
    return dt


def parse_exif_date(s):
    """'2015:03:14 10:22:31' -> '2015-03-14T10:22:31', or None when blank/invalid (e.g. 0000:00:00)."""
    if not s:
        return None
    s = str(s).replace("\x00", "").strip()
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm, ss = (int(m.group(i) or 0) for i in (4, 5, 6))
    dt = _valid(y, mo, d, hh, mm, ss)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


_NAME_DATE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def date_from_filename(name):
    m = _NAME_DATE.search(name)
    if not m:
        return None
    dt = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return dt.strftime("%Y-%m-%d") if dt else None


def read_exif(path: str) -> dict:
    """Returns {'date': iso|None, 'lat': float|None, 'lon': float|None}. Never raises."""
    out = {"date": None, "lat": None, "lon": None}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            date = None
            try:
                sub = exif.get_ifd(0x8769)
                date = sub.get(0x9003) or sub.get(0x9004)
            except Exception:
                pass
            date = date or exif.get(0x0132)
            out["date"] = parse_exif_date(date)
            try:
                gps = exif.get_ifd(0x8825)
                if gps and 2 in gps and 4 in gps:
                    lat = _dms(gps[2], gps.get(1))
                    lon = _dms(gps[4], gps.get(3))
                    if lat is not None and lon is not None and (abs(lat) > 0.0001 or abs(lon) > 0.0001):
                        if abs(lat) <= 90 and abs(lon) <= 180:
                            out["lat"], out["lon"] = lat, lon
            except Exception:
                pass
    except Exception:
        pass
    return out


def resolve_date(filename: str, exif_date, mtime: float):
    """(iso_date, source). EXIF is trusted most; the file's own date is the last resort."""
    if exif_date:
        return exif_date, "exif"
    d = date_from_filename(filename)
    if d:
        return d, "filename"
    try:
        dt = datetime.datetime.fromtimestamp(mtime)
        if dt.year >= _MIN_YEAR:
            return dt.strftime("%Y-%m-%d"), "mtime"
    except (OSError, OverflowError, ValueError):
        pass
    return None, None
