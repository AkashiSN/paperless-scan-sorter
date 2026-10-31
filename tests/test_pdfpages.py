from __future__ import annotations

import io
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable
from paperless_scan_sorter.pdfpages import (
    Runner,
    analyze_pdf,
    dhash,
    hamming,
    ink_ratio,
    page_count,
    parse_osd,
    split_pdf,
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


def _fake_runner(
    pages_by_file: dict[str, int], texts: dict[str, bytes], calls: list[list[str]]
) -> Runner:
    """ファイルごとに pdfinfo のページ数と pdftotext の出力を返す偽の runner。"""

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        name = argv[1] if argv[0] == "pdfinfo" else argv[-2]
        if argv[0] == "pdfinfo":
            return _ok(f"Pages:          {pages_by_file[name]}\n".encode())
        if argv[0] == "pdftoppm":
            stem = argv[-1]
            for i in range(1, pages_by_file[name] + 1):
                Image.new("L", (50, 50), 255).save(Path(cwd) / f"{stem}-{i}.png")
            return _ok()
        if argv[0] == "pdftotext":
            return _ok(texts[name])
        return _ok()

    return runner


def test_text_pdfがあればテキストはそちらからpdftotextで取る() -> None:
    calls: list[list[str]] = []
    runner = _fake_runner(
        {"doc.pdf": 2, "text.pdf": 2}, {"doc.pdf": b"\f\f", "text.pdf": b"one\ftwo\f"}, calls
    )
    infos = analyze_pdf(b"img", text_pdf=b"txt", runner=runner)
    assert [i.text for i in infos] == ["one", "two"]
    totext = [c for c in calls if c[0] == "pdftotext"]
    assert [c[-2] for c in totext] == ["text.pdf"]


def test_text_pdfのページ数が違えばテキストは全部空にして警告する(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[list[str]] = []
    runner = _fake_runner(
        {"doc.pdf": 2, "text.pdf": 3},
        {"doc.pdf": b"a\fb\f", "text.pdf": b"one\ftwo\fthree\f"},
        calls,
    )
    with caplog.at_level(logging.WARNING):
        infos = analyze_pdf(b"img", text_pdf=b"txt", runner=runner)
    assert [i.text for i in infos] == ["", ""]
    assert "ページ数が合わない" in caplog.text
    assert "one" not in caplog.text


def test_text_pdfが読めなければテキストは全部空にして続ける(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[list[str]] = []
    base = _fake_runner({"doc.pdf": 2, "text.pdf": 2}, {"doc.pdf": b"a\fb\f"}, calls)

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdftotext" and argv[-2] == "text.pdf":
            return _ok(returncode=1)
        return base(argv, cwd)

    with caplog.at_level(logging.WARNING):
        infos = analyze_pdf(b"img", text_pdf=b"txt", runner=runner)
    assert [i.text for i in infos] == ["", ""]
    assert "テキスト用の PDF" in caplog.text


def test_text_pdfのpdfinfoにPages行が無ければテキストは全部空にして続ける(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[list[str]] = []
    base = _fake_runner({"doc.pdf": 2, "text.pdf": 2}, {"doc.pdf": b"a\fb\f"}, calls)

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdfinfo" and argv[1] == "text.pdf":
            return _ok(b"Title: x\n")
        return base(argv, cwd)

    with caplog.at_level(logging.WARNING):
        infos = analyze_pdf(b"img", text_pdf=b"txt", runner=runner)
    assert [i.text for i in infos] == ["", ""]
    assert "テキスト用の PDF" in caplog.text


def test_text_pdfのpdfinfoがタイムアウトしてもテキストは全部空にして続ける(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[list[str]] = []
    base = _fake_runner({"doc.pdf": 2, "text.pdf": 2}, {"doc.pdf": b"a\fb\f"}, calls)

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdfinfo" and argv[1] == "text.pdf":
            raise subprocess.TimeoutExpired(argv, 1)
        return base(argv, cwd)

    with caplog.at_level(logging.WARNING):
        infos = analyze_pdf(b"img", text_pdf=b"txt", runner=runner)
    assert [i.text for i in infos] == ["", ""]
    assert "テキスト用の PDF" in caplog.text


def test_text_pdfが無ければ画像用のPDFからテキストを取る() -> None:
    calls: list[list[str]] = []
    runner = _fake_runner({"doc.pdf": 2}, {"doc.pdf": b"a\fb\f"}, calls)
    assert [i.text for i in analyze_pdf(b"img", runner=runner)] == ["a", "b"]


def test_text_pdf側でコマンドが無ければServiceUnavailableをそのまま送出する() -> None:
    calls: list[list[str]] = []
    base = _fake_runner({"doc.pdf": 2, "text.pdf": 2}, {"doc.pdf": b"a\fb\f"}, calls)

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdftotext" and argv[-2] == "text.pdf":
            raise FileNotFoundError
        return base(argv, cwd)

    with pytest.raises(ServiceUnavailable):
        analyze_pdf(b"img", text_pdf=b"txt", runner=runner)


def test_OSDを省くしきい値を渡せる() -> None:
    calls: list[list[str]] = []
    runner = _fake_runner({"doc.pdf": 2}, {"doc.pdf": b"a\fb\f"}, calls)
    # 偽の画像は真っ白（インク 0）なので、既定では tesseract を呼ばない
    analyze_pdf(b"img", runner=runner)
    assert not any(c[0] == "tesseract" for c in calls)
    calls.clear()
    analyze_pdf(b"img", runner=runner, osd_skip_ink=-1.0)
    assert sum(c[0] == "tesseract" for c in calls) == 2


def _split_runner(pages: int, page_bytes: dict[int, int], calls: list[list[str]]) -> Runner:
    """pdfseparate は 1 ページずつ、pdfunite はつないだ大きさのファイルを作る偽の runner。

    ページ n のファイルの大きさは page_bytes[n]（無ければ 10 バイト）。
    """

    def size(n: int) -> int:
        return page_bytes.get(n, 10)

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        calls.append(list(argv))
        if argv[0] == "pdfinfo":
            return _ok(f"Pages:          {pages}\n".encode())
        if argv[0] == "pdfseparate":
            for n in range(1, pages + 1):
                (Path(cwd) / (argv[-1] % n)).write_bytes(b"p" * size(n))
            return _ok()
        if argv[0] == "pdfunite":
            total = sum(len((Path(cwd) / name).read_bytes()) for name in argv[1:-1])
            (Path(cwd) / argv[-1]).write_bytes(b"u" * total)
            return _ok()
        return _ok(returncode=1)

    return runner


def test_split_pdf_上限以下の小さい書類は分けずにそのまま返す() -> None:
    calls: list[list[str]] = []
    chunks = split_pdf(
        b"orig", pages_per_chunk=5, max_bytes=100, runner=_split_runner(3, {}, calls)
    )
    assert [(c.first, c.last, c.data) for c in chunks] == [(1, 3, b"orig")]
    assert [c[0] for c in calls] == ["pdfinfo"]


def test_split_pdf_ページ数で分ける() -> None:
    calls: list[list[str]] = []
    chunks = split_pdf(b"x", pages_per_chunk=5, max_bytes=1000, runner=_split_runner(12, {}, calls))
    assert [(c.first, c.last) for c in chunks] == [(1, 5), (6, 10), (11, 12)]
    assert [c.pages for c in chunks] == [5, 5, 2]
    assert len(chunks[0].data) == 50
    unite = [c for c in calls if c[0] == "pdfunite"]
    assert unite[0][1:-1] == [f"page-{n}.pdf" for n in range(1, 6)]
    assert all(isinstance(c, list) for c in calls)


def test_split_pdf_1ページだけ残る分はpdfseparateの出力をそのまま使う() -> None:
    calls: list[list[str]] = []
    chunks = split_pdf(b"x", pages_per_chunk=5, max_bytes=1000, runner=_split_runner(6, {}, calls))
    assert [(c.first, c.last) for c in chunks] == [(1, 5), (6, 6)]
    assert chunks[1].data == b"p" * 10
    assert sum(c[0] == "pdfunite" for c in calls) == 1


def test_split_pdf_上限を超える分はページ数を減らして分け直す() -> None:
    calls: list[list[str]] = []
    runner = _split_runner(4, {1: 40, 2: 40, 3: 10, 4: 10}, calls)
    chunks = split_pdf(b"x" * 101, pages_per_chunk=5, max_bytes=60, runner=runner)
    assert [(c.first, c.last) for c in chunks] == [(1, 1), (2, 2), (3, 4)]
    assert all(len(c.data) <= 60 for c in chunks)


def test_split_pdf_小さい書類でも全体が上限を超えれば分ける() -> None:
    calls: list[list[str]] = []
    runner = _split_runner(2, {1: 30, 2: 30}, calls)
    chunks = split_pdf(b"x" * 101, pages_per_chunk=5, max_bytes=50, runner=runner)
    assert [(c.first, c.last) for c in chunks] == [(1, 1), (2, 2)]


def test_split_pdf_1ページで上限を超えれば書類起因() -> None:
    with pytest.raises(DocumentFailure, match="1 ページ"):
        split_pdf(b"x" * 101, pages_per_chunk=5, max_bytes=50, runner=_split_runner(2, {1: 60}, []))


def test_split_pdf_ページが無ければ書類起因() -> None:
    with pytest.raises(DocumentFailure):
        split_pdf(b"x", pages_per_chunk=5, max_bytes=50, runner=_split_runner(0, {}, []))


def test_split_pdf_pdfseparateが失敗すれば書類起因() -> None:
    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdfinfo":
            return _ok(b"Pages:          9\n")
        return _ok(returncode=1)

    with pytest.raises(DocumentFailure, match="pdfseparate"):
        split_pdf(b"x", pages_per_chunk=5, max_bytes=1000, runner=runner)


def test_split_pdf_pdfuniteが失敗すれば書類起因() -> None:
    base = _split_runner(9, {}, [])

    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdfunite":
            return _ok(returncode=1)
        return base(argv, cwd)

    with pytest.raises(DocumentFailure, match="pdfunite"):
        split_pdf(b"x", pages_per_chunk=5, max_bytes=1000, runner=runner)


def test_split_pdf_pdfseparateの出力が欠ければ書類起因() -> None:
    def runner(argv, cwd):  # noqa: ANN001, ANN202
        if argv[0] == "pdfinfo":
            return _ok(b"Pages:          9\n")
        return _ok()

    with pytest.raises(DocumentFailure, match="ページ数と合わない"):
        split_pdf(b"x", pages_per_chunk=5, max_bytes=1000, runner=runner)


# --- 実物のコマンドを使うテスト ---

_TOOLS = ("pdfinfo", "pdftoppm", "pdftotext", "tesseract", "pdfseparate", "pdfunite")
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


@needs_tools
def test_実物_テキストはtext_pdfから取り画像は元のPDFから取る() -> None:
    scan = _pdf(_text_page(), Image.new("L", (827, 1169), 255))
    layered = _text_pdf("first page", "second page")
    infos = analyze_pdf(scan, text_pdf=layered)
    assert "first" in infos[0].text
    assert "second" in infos[1].text
    assert infos[0].ink_ratio > 0.003
    assert infos[1].ink_ratio <= 0.003
    assert [i.text for i in analyze_pdf(scan)] == ["", ""]


@needs_tools
def test_実物_split_pdfはページ順を保って分ける() -> None:
    pdf = _text_pdf(*(f"page number {n}" for n in range(1, 8)))
    chunks = split_pdf(pdf, pages_per_chunk=3, max_bytes=10 * 1024 * 1024)
    assert [(c.first, c.last) for c in chunks] == [(1, 3), (4, 6), (7, 7)]
    texts = [i.text for c in chunks for i in analyze_pdf(c.data)]
    assert [f"page number {n}" in t for n, t in enumerate(texts, start=1)] == [True] * 7
