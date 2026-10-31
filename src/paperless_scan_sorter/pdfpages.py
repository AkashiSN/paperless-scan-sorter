"""PDF からページごとの画像・テキスト・向きの測定値を取り出し、PDF をページの範囲ごとに分ける。

poppler（`pdfinfo` `pdftoppm` `pdftotext` `pdfseparate` `pdfunite`）と tesseract を引数配列で
起動する。判定は行わず、測定値を `PageInfo` にして返すだけ（判定は `pageplan`）。
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable
from paperless_scan_sorter.pageplan import DEFAULT_THRESHOLDS, PageInfo

logger = logging.getLogger(__name__)

_TIMEOUT = 120.0
_INK_THRESHOLD = 160  # この輝度未満の画素を「インク」と数える
_MARGIN = 0.05  # 外周のこの割合は、スキャンの縁の影を避けるため数えない

# (argv, cwd)
type Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[bytes]]


def run_subprocess(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """既定の実行関数。引数配列のまま起動し、シェルは経由しない。"""
    return subprocess.run(  # noqa: S603 - 引数配列で起動し、シェルを使わない
        argv, cwd=cwd, timeout=_TIMEOUT, capture_output=True, check=False
    )


def _run(runner: Runner, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    try:
        return runner(argv, cwd)
    except subprocess.TimeoutExpired as e:
        raise DocumentFailure(f"{argv[0]} がタイムアウトした") from e
    except FileNotFoundError as e:
        raise ServiceUnavailable(f"{argv[0]} コマンドが見つからない") from e


def _pages_in(output: str) -> int:
    m = re.search(r"^Pages:\s+(\d+)", output, re.MULTILINE)
    if m is None:
        raise DocumentFailure("pdfinfo の出力にページ数が無い")
    return int(m.group(1))


def page_count(pdf: bytes, *, runner: Runner = run_subprocess) -> int:
    """PDF のページ数を返す。読めなければ書類起因の失敗。"""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "doc.pdf").write_bytes(pdf)
        proc = _run(runner, ["pdfinfo", "doc.pdf"], cwd)
        if proc.returncode != 0:
            raise DocumentFailure("pdfinfo が PDF を読めなかった")
        return _pages_in(proc.stdout.decode("utf-8", "replace"))


@dataclass(frozen=True)
class PdfChunk:
    """元の PDF の first〜last ページ（1 から数え、両端を含む）だけを持つ PDF。"""

    first: int
    last: int
    data: bytes

    @property
    def pages(self) -> int:
        return self.last - self.first + 1


def split_pdf(
    pdf: bytes, *, pages_per_chunk: int, max_bytes: int, runner: Runner = run_subprocess
) -> list[PdfChunk]:
    """PDF を pages_per_chunk ページずつ、それぞれ max_bytes 以下の PDF に分ける。

    全体がどちらの上限にも収まれば、元の PDF をそのまま 1 つで返す。大きさの上限を超える
    範囲は半分に分け直す。1 ページだけで上限を超えるときや、読めないときは書類起因の失敗。
    分けた PDF は読ませるための一時的な写しで、元の PDF には触れない。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "doc.pdf").write_bytes(pdf)
        info = _run(runner, ["pdfinfo", "doc.pdf"], cwd)
        if info.returncode != 0:
            raise DocumentFailure("pdfinfo が PDF を読めなかった")
        pages = _pages_in(info.stdout.decode("utf-8", "replace"))
        if pages < 1:
            raise DocumentFailure("PDF にページが無い")
        if pages <= pages_per_chunk and len(pdf) <= max_bytes:
            return [PdfChunk(1, pages, pdf)]

        proc = _run(runner, ["pdfseparate", "doc.pdf", "page-%d.pdf"], cwd)
        if proc.returncode != 0:
            raise DocumentFailure("pdfseparate が PDF を分けられなかった")
        if any(not (cwd / f"page-{n}.pdf").is_file() for n in range(1, pages + 1)):
            raise DocumentFailure("分けたページの数がページ数と合わない")

        chunks: list[PdfChunk] = []

        def add(first: int, last: int) -> None:
            if first == last:
                data = (cwd / f"page-{first}.pdf").read_bytes()
            else:
                name = f"chunk-{first}-{last}.pdf"
                parts = [f"page-{n}.pdf" for n in range(first, last + 1)]
                united = _run(runner, ["pdfunite", *parts, name], cwd)
                if united.returncode != 0:
                    raise DocumentFailure("pdfunite がページをつなげなかった")
                data = (cwd / name).read_bytes()
            if len(data) <= max_bytes:
                chunks.append(PdfChunk(first, last, data))
            elif first == last:
                raise DocumentFailure(f"1 ページで PDF の上限を超える ({len(data)} bytes)")
            else:
                middle = (first + last) // 2
                add(first, middle)
                add(middle + 1, last)

        for start in range(1, pages + 1, pages_per_chunk):
            add(start, min(start + pages_per_chunk - 1, pages))
        return chunks


def ink_ratio(image: Image.Image) -> float:
    """外周 5% を除いた領域で、暗い画素の割合を返す。"""
    gray = image.convert("L")
    w, h = gray.size
    mx, my = int(w * _MARGIN), int(h * _MARGIN)
    inner = gray.crop((mx, my, w - mx, h - my))
    total = inner.width * inner.height
    if total == 0:
        return 0.0
    hist = inner.histogram()
    return sum(hist[:_INK_THRESHOLD]) / total


def dhash(image: Image.Image) -> int:
    """9x8 に縮小し、横に隣り合う画素の大小を並べた 64 ビットの知覚ハッシュ。"""
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y * 9 + x] > px[y * 9 + x + 1])
    return bits


def hamming(a: int, b: int) -> int:
    """2 つのハッシュのビットの違いの数。"""
    return (a ^ b).bit_count()


def parse_osd(output: str) -> tuple[int | None, float]:
    """`tesseract --psm 0` の出力から (Rotate, Orientation confidence) を読む。"""
    rot = re.search(r"^Rotate:\s*(\d+)", output, re.MULTILINE)
    conf = re.search(r"^Orientation confidence:\s*([\d.]+)", output, re.MULTILINE)
    if rot is None or conf is None:
        return None, 0.0
    return int(rot.group(1)), float(conf.group(1))


def _to_clockwise(tesseract_rotate: int) -> int:
    """tesseract の `Rotate:` を、正立にするため時計回りに回す角度へ変換する。

    tesseract の `Rotate:` は「正立にするために画像を時計回りに回す角度」で、`edit_pdf`
    の `rotate`（時計回りが正）と同じ向きなので、そのまま使う（実物の PDF で、反時計回りに
    90 度回したページが 90、270 度回したページが 270 と報告されることを確かめてある）。
    """
    return tesseract_rotate % 360


def _number(path: Path) -> int:
    return int(path.stem.rsplit("-", 1)[1])


def _text_per_page(
    runner: Runner, cwd: Path, name: str, pages: int, *, strict: bool
) -> list[str] | None:
    """`pdftotext` でページごとのテキストを取る。

    strict でなければ、テキスト用の PDF の不備（読めない・ページ数の不一致）は警告にして
    None を返す。本文はログに出さない。
    """
    if not strict:
        try:
            info = _run(runner, ["pdfinfo", name], cwd)
            if info.returncode != 0:
                logger.warning("テキスト用の PDF を pdfinfo が読めなかった。テキストを空にする")
                return None
            if _pages_in(info.stdout.decode("utf-8", "replace")) != pages:
                logger.warning("テキスト用の PDF のページ数が合わない。テキストを空にする")
                return None
            proc = _run(runner, ["pdftotext", "-enc", "UTF-8", name, "-"], cwd)
        except DocumentFailure as e:
            # Pages 行が無い・タイムアウトなど。ServiceUnavailable はそのまま送出する
            logger.warning("テキスト用の PDF を読めなかった（%s）。テキストを空にする", e)
            return None
        if proc.returncode != 0:
            logger.warning("テキスト用の PDF を pdftotext が読めなかった。テキストを空にする")
            return None
    else:
        proc = _run(runner, ["pdftotext", "-enc", "UTF-8", name, "-"], cwd)
        if proc.returncode != 0:
            raise DocumentFailure("pdftotext が PDF を読めなかった")
    texts = proc.stdout.decode("utf-8", "replace").split("\f")[:pages]
    return texts + [""] * (pages - len(texts))


def analyze_pdf(
    pdf: bytes,
    *,
    text_pdf: bytes | None = None,
    runner: Runner = run_subprocess,
    osd_skip_ink: float = DEFAULT_THRESHOLDS.blank_ink_review_max,
) -> list[PageInfo]:
    """全ページの測定値を返す。読めなければ書類起因の失敗。

    画像（インク・dHash・向き）は `pdf` から取る。ページごとのテキストは `text_pdf` が
    あればそちらから取り（スキャンの原本にはテキスト層が無いため、OCR 済みのアーカイブ版を
    渡す）、無ければ `pdf` から取る。インクの割合が `osd_skip_ink` 以下のページは
    白紙の削除候補になりうるので、向きを調べない。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "doc.pdf").write_bytes(pdf)
        info = _run(runner, ["pdfinfo", "doc.pdf"], cwd)
        if info.returncode != 0:
            raise DocumentFailure("pdfinfo が PDF を読めなかった")
        pages = _pages_in(info.stdout.decode("utf-8", "replace"))

        proc = _run(runner, ["pdftoppm", "-gray", "-r", "100", "-png", "doc.pdf", "page"], cwd)
        if proc.returncode != 0:
            raise DocumentFailure("pdftoppm が PDF を画像にできなかった")
        pngs = sorted(cwd.glob("page-*.png"), key=_number)
        if len(pngs) != pages:
            raise DocumentFailure("画像の枚数がページ数と合わない")

        if text_pdf is None:
            texts = _text_per_page(runner, cwd, "doc.pdf", pages, strict=True)
        else:
            (cwd / "text.pdf").write_bytes(text_pdf)
            texts = _text_per_page(runner, cwd, "text.pdf", pages, strict=False)
        if texts is None:
            texts = [""] * pages

        result: list[PageInfo] = []
        for n, png in enumerate(pngs, start=1):
            with Image.open(png) as img:
                img.load()
                ratio, h = ink_ratio(img), dhash(img)
            rotate, conf = None, 0.0
            # 白紙の削除候補になりうるページは、向きを読んでも使わないので tesseract を呼ばない
            if ratio > osd_skip_ink:
                rotate, conf = _detect_rotation(runner, cwd, n)
            result.append(PageInfo(n, ratio, texts[n - 1], h, rotate, conf))
        return result


def _detect_rotation(runner: Runner, cwd: Path, n: int) -> tuple[int | None, float]:
    """150dpi で 1 ページだけ画像にし、tesseract の OSD で向きを調べる。"""
    stem = f"osd{n}"
    proc = _run(
        runner,
        ["pdftoppm", "-gray", "-r", "150", "-png", "-f", str(n), "-l", str(n), "doc.pdf", stem],
        cwd,
    )
    images = sorted(cwd.glob(f"{stem}-*.png"))
    if proc.returncode != 0 or not images:
        return None, 0.0
    proc = _run(runner, ["tesseract", images[0].name, "stdout", "--psm", "0"], cwd)
    if proc.returncode != 0:
        return None, 0.0
    rotate, conf = parse_osd(proc.stdout.decode("utf-8", "replace"))
    if rotate is None:
        return None, 0.0
    return _to_clockwise(rotate), conf
