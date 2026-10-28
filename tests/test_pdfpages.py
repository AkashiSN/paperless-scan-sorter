from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable
from paperless_scan_sorter.pdfpages import (
    analyze_pdf,
    dhash,
    hamming,
    ink_ratio,
    page_count,
    parse_osd,
)


def _white(size: int = 200) -> Image.Image:
    return Image.new("L", (size, size), 255)


def test_ink_ratio_白紙は0() -> None:
    assert ink_ratio(_white()) == 0.0


def test_ink_ratio_中央の矩形の面積を返す() -> None:
    img = _white()
    # 数える領域（外周 5% を除いた 180x180）の 10% ≒ 57x57
    ImageDraw.Draw(img).rectangle((70, 70, 126, 126), fill=0)
    assert ink_ratio(img) == pytest.approx(0.10, abs=0.01)


def test_ink_ratio_外周だけ黒いなら0() -> None:
    img = Image.new("L", (200, 200), 0)
    ImageDraw.Draw(img).rectangle((10, 10, 189, 189), fill=255)
    assert ink_ratio(img) == 0.0


def test_dhash_同じ画像は距離0_反転は遠い() -> None:
    img = Image.effect_mandelbrot((120, 120), (-2, -1.5, 1, 1.5), 50).convert("L")
    assert hamming(dhash(img), dhash(img.copy())) == 0
    inverted = img.point(lambda v: 255 - v)
    assert hamming(dhash(img), dhash(inverted)) > 32


def test_parse_osd() -> None:
    out = (
        "Page number: 0\nOrientation in degrees: 180\nRotate: 180\n"
        "Orientation confidence: 9.85\nScript: Latin\n"
    )
    assert parse_osd(out) == (180, 9.85)
    assert parse_osd("") == (None, 0.0)


def _ok(stdout: bytes = b"", returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], returncode, stdout, b"")


def test_page_count_非0は書類起因() -> None:
    with pytest.raises(DocumentFailure):
        page_count(b"x", runner=lambda argv, cwd: _ok(returncode=1))


def test_page_count_Pages行が無ければ書類起因() -> None:
    with pytest.raises(DocumentFailure):
        page_count(b"x", runner=lambda argv, cwd: _ok(b"Title: a\n"))


def test_コマンドが無ければ_ServiceUnavailable() -> None:
    def runner(argv, cwd):  # noqa: ANN001, ANN202
        raise FileNotFoundError

    with pytest.raises(ServiceUnavailable):
        page_count(b"x", runner=runner)


def test_タイムアウトは書類起因() -> None:
    def runner(argv, cwd):  # noqa: ANN001, ANN202
        raise subprocess.TimeoutExpired(argv, 120)

    with pytest.raises(DocumentFailure):
        page_count(b"x", runner=runner)


def test_argv_はリストでシェルを使わない() -> None:
    seen: list[object] = []

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        seen.append(argv)
        assert (Path(cwd) / "doc.pdf").read_bytes() == b"pdf"
        return _ok(b"Pages:          3\n")

    assert page_count(b"pdf", runner=runner) == 3
    assert isinstance(seen[0], list)
    assert seen[0][0] == "pdfinfo"


# --- 実物のコマンドを使うテスト ---

_TOOLS = ("pdfinfo", "pdftoppm", "pdftotext", "tesseract")
needs_tools = pytest.mark.skipif(
    any(shutil.which(t) is None for t in _TOOLS), reason="poppler / tesseract が無い"
)


def _text_page() -> Image.Image:
    img = Image.new("L", (827, 1169), 255)
    font = ImageFont.load_default(size=48)
    draw = ImageDraw.Draw(img)
    lines = [
        "The quick brown fox jumps over",
        "the lazy dog while the sun sets",
        "behind the distant mountains and",
        "the river flows quietly through",
        "the green valley below the hills",
        "Invoice total amount due within",
        "thirty days of the date printed",
        "above this line of the document",
        "Please contact our office if you",
        "have any questions about this",
    ]
    for i, line in enumerate(lines):
        draw.text((40, 60 + i * 100), line, fill=0, font=font)
    return img


def _pdf(*images: Image.Image) -> bytes:
    buf = io.BytesIO()
    first, *rest = (i.convert("RGB") for i in images)
    first.save(buf, "PDF", resolution=100, save_all=True, append_images=rest)
    return buf.getvalue()


@needs_tools
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_実物_向きを時計回りの角度で返す(angle: int) -> None:
    page = _text_page().rotate(angle, expand=True) if angle else _text_page()
    (info,) = analyze_pdf(_pdf(page))
    assert (info.rotate or 0) == angle
    assert info.ink_ratio > 0.003


@needs_tools
def test_実物_白紙とテキストと複数ページ() -> None:
    blank = Image.new("L", (827, 1169), 255)
    infos = analyze_pdf(_pdf(_text_page(), blank))
    assert [i.number for i in infos] == [1, 2]
    assert infos[1].ink_ratio <= 0.003
    assert infos[1].rotate is None
    assert page_count(_pdf(_text_page(), blank)) == 2


def _text_pdf(*pages: str) -> bytes:
    """テキスト層を持つ最小の PDF（xref は poppler が復元する）。"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    for i, text in enumerate(pages):
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
            "/BaseFont /Helvetica >> >> >> "
            f"/Contents {4 + 2 * i} 0 R >>"
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    body = "%PDF-1.4\n"
    for n, o in enumerate(objs, start=1):
        body += f"{n} 0 obj\n{o}\nendobj\n"
    return (body + "trailer\n<< /Root 1 0 R >>\n%%EOF\n").encode()


@needs_tools
def test_実物_テキストがページごとに分かれる() -> None:
    infos = analyze_pdf(_text_pdf("first page", "second page"))
    assert len(infos) == 2
    assert "first" in infos[0].text
    assert "second" in infos[1].text
    assert "second" not in infos[0].text
