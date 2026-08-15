"""extract_fit_from_zip 的行为契约测试（L3 重构的保护网）。"""

import io
import zipfile

import pytest

from garmin_sync import extract_fit_from_zip


def _zip(entries) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_returns_fit_content():
    path = extract_fit_from_zip(_zip([("act.fit", b"DATA")]), 1)
    try:
        assert path.read_bytes() == b"DATA"
        assert path.suffix == ".fit"
    finally:
        path.unlink()


def test_extract_picks_fit_among_other_files_case_insensitive():
    path = extract_fit_from_zip(_zip([("a.txt", b"x"), ("b.FIT", b"Y")]), 1)
    try:
        assert path.read_bytes() == b"Y"
    finally:
        path.unlink()


def test_extract_without_fit_raises():
    with pytest.raises(ValueError):
        extract_fit_from_zip(_zip([("a.txt", b"x")]), 1)
