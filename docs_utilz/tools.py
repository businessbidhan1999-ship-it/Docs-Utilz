"""File size adjustment and conversion between PDFs and images."""

from __future__ import annotations

import io
import math
import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PIL import Image

try:                       # PyMuPDF 1.24+ renamed itself; `fitz` still works
    import pymupdf as fitz
except ImportError:        # pragma: no cover — older PyMuPDF
    import fitz


Progress = Callable[[float, str], None]

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
SUPPORTED_EXT = PDF_EXT | IMAGE_EXT

# Pillow refuses to open enormous images by default, guarding against
# decompression-bomb attacks. Scans of large-format paper at 600 dpi trip that
# limit legitimately, and every file here is one the user picked themselves.
Image.MAX_IMAGE_PIXELS = None


def _noop(_p: float, _m: str) -> None:
    pass


def kind_of(path: str | os.PathLike[str]) -> str:
    """"pdf", "image", or "" for anything this module cannot handle."""
    ext = Path(path).suffix.lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    return ""


@dataclass
class CompressResult:
    path: str
    original_bytes: int
    final_bytes: int
    target_bytes: int
    method: str            # what actually did the work, for the log
    reached: bool          # False = this is the best it could manage

    @property
    def ratio(self) -> float:
        return self.final_bytes / self.original_bytes if self.original_bytes else 1.0

    def summary(self) -> str:
        return (f"{_kb(self.original_bytes)} -> {_kb(self.final_bytes)} "
                f"({self.ratio * 100:.0f}% of original) via {self.method}"
                + ("" if self.reached else " — target not reachable"))


def human_size(n: int) -> str:
    """A file size the way someone would say it out loud."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / 1024:.0f} KB"


_kb = human_size  # used throughout below, where brevity reads better


def _search_ladder(rungs: Sequence, encode: Callable[[object], bytes],
                   target: int, progress: Progress = _noop,
                   label: str = "") -> tuple[bytes, object, bool]:
    """Find the gentlest rung whose output fits `target` bytes.

    `rungs` runs from least to most aggressive, so output size is monotonically
    non-increasing along it and a binary search is valid. Returns the chosen
    bytes, the rung that produced them, and whether the target was actually met;
    when even the harshest rung is too big we hand back that harshest result,
    because a file that is smaller but still over target is more use than none.
    """
    best: tuple[bytes, object] | None = None       # smallest seen, as a fallback
    fit: tuple[bytes, object] | None = None        # smallest rung index that fits
    lo, hi = 0, len(rungs) - 1
    steps = max(1, int(math.log2(len(rungs))) + 1)
    done = 0

    while lo <= hi:
        mid = (lo + hi) // 2
        data = encode(rungs[mid])
        done += 1
        progress(min(done / steps, 0.95),
                 f"{label}trying setting {mid + 1}/{len(rungs)} -> {_kb(len(data))}")
        if best is None or len(data) < len(best[0]):
            best = (data, rungs[mid])
        if len(data) <= target:
            fit = (data, rungs[mid])
            hi = mid - 1          # same size or better may be possible, gentler
        else:
            lo = mid + 1

    if fit is not None:
        return fit[0], fit[1], True
    assert best is not None
    return best[0], best[1], False


def _open_image(path: str) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img


def _flatten(img: Image.Image) -> Image.Image:
    """Drop transparency onto white — JPEG cannot carry an alpha channel."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and
                                      "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img


def _resized(img: Image.Image, scale: float) -> Image.Image:
    if scale >= 0.999:
        return img
    w = max(int(img.width * scale), 16)
    h = max(int(img.height * scale), 16)
    return img.resize((w, h), Image.LANCZOS)


def _encode_jpeg(img: Image.Image, scale: float, quality: int) -> bytes:
    buf = io.BytesIO()
    _resized(_flatten(img), scale).save(
        buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _encode_png(img: Image.Image, scale: float, colours: int) -> bytes:
    small = _resized(img, scale)
    if colours:
        # Palette reduction is where PNG's real savings live; 256 colours is
        # usually indistinguishable on a document scan and roughly halves it.
        # Median cut gives the better palette, but Pillow only offers the
        # octree for images that still carry transparency.
        if small.mode in ("RGBA", "LA") or (small.mode == "P" and
                                            "transparency" in small.info):
            small = small.convert("RGBA").quantize(
                colours, method=Image.FASTOCTREE)
        else:
            small = small.convert("RGB").quantize(
                colours, method=Image.MEDIANCUT)
    buf = io.BytesIO()
    small.save(buf, "PNG", optimize=True)
    return buf.getvalue()


_JPEG_QUALITIES = (92, 85, 80, 74, 68, 62, 55, 48, 40, 32)
_SCALES = (1.0, 0.92, 0.85, 0.78, 0.7, 0.62, 0.55, 0.48, 0.4, 0.33, 0.27, 0.2)
_PNG_COLOURS = (0, 256, 128, 64, 32, 16)


def compress_image(in_path: str, out_path: str, target_bytes: int,
                   allow_format_change: bool = True,
                   progress: Progress = _noop) -> CompressResult:
    """Shrink one picture to `target_bytes`, keeping it as legible as possible.

    Quality is spent before pixels are: the ladder exhausts the quality range at
    full size, then starts scaling down, because a soft-but-sharp-edged page
    reads better than a crisp thumbnail of one.
    """
    original = os.path.getsize(in_path)
    out_ext = Path(out_path).suffix.lower()

    # Copy files that already fit to avoid lossy re-encoding.
    if original <= target_bytes and out_ext == Path(in_path).suffix.lower():
        Path(out_path).write_bytes(Path(in_path).read_bytes())
        progress(1.0, f"Already under target ({_kb(original)}) — copied as is")
        return CompressResult(out_path, original, original, target_bytes,
                              "copied unchanged (already under target)", True)

    img = _open_image(in_path)
    want_png = out_ext == ".png"

    if want_png:
        rungs = [(s, c) for s in _SCALES for c in _PNG_COLOURS]
        data, rung, ok = _search_ladder(
            rungs, lambda r: _encode_png(img, r[0], r[1]),
            target_bytes, progress, "PNG: ")
        method = f"PNG, {rung[0] * 100:.0f}% size" + (
            f", {rung[1]} colours" if rung[1] else ", full colour")

        # A photographic PNG has a floor it simply cannot go below. Rather than
        # hand back a 3 MB "compressed" file, offer JPEG — but only if the user
        # left the door open, since it costs transparency.
        if not ok and allow_format_change:
            jpg_path = str(Path(out_path).with_suffix(".jpg"))
            alt = compress_image(in_path, jpg_path, target_bytes, False,
                                 progress)
            if alt.final_bytes < len(data):
                return alt
            Path(jpg_path).unlink(missing_ok=True)
    else:
        rungs = [(s, q) for s in _SCALES for q in _JPEG_QUALITIES]
        data, rung, ok = _search_ladder(
            rungs, lambda r: _encode_jpeg(img, r[0], r[1]),
            target_bytes, progress, "JPEG: ")
        method = f"JPEG quality {rung[1]}, {rung[0] * 100:.0f}% size"

    Path(out_path).write_bytes(data)
    progress(1.0, f"Wrote {_kb(len(data))}")
    return CompressResult(out_path, original, len(data), target_bytes,
                          method, ok)


def _pdf_bytes(doc: "fitz.Document") -> bytes:
    return doc.tobytes(garbage=4, deflate=True, deflate_images=True,
                       deflate_fonts=True, clean=True)


def _recompress_embedded(in_path: str, scale: float, quality: int) -> bytes:
    """Re-encode the pictures inside a PDF, leaving its text alone.

    This is the compression that costs nothing you can read: body text stays
    real, selectable text at full fidelity, and only the scanned photographs and
    logos — nearly always the bulk of the file — lose detail.
    """
    doc = fitz.open(in_path)
    seen: set[int] = set()
    for pno in range(doc.page_count):
        page = doc[pno]
        for info in page.get_images(full=True):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                pix = fitz.Pixmap(doc, xref)
            except Exception:
                continue
            # Masks and tiny decorations are not worth the risk or the effort:
            # re-encoding a stencil mask as JPEG corrupts the page it clips.
            if pix.alpha or pix.n > 3 or pix.width * pix.height < 10_000:
                pix = None
                continue
            try:
                img = Image.frombytes(
                    "RGB" if pix.n == 3 else "L",
                    (pix.width, pix.height), pix.samples)
                blob = _encode_jpeg(img, scale, quality)
                if blob and len(blob) < len(pix.samples):
                    page.replace_image(xref, stream=blob)
            except Exception:
                pass
            finally:
                pix = None
    data = _pdf_bytes(doc)
    doc.close()
    return data


def _rasterise(in_path: str, dpi: int, quality: int) -> bytes:
    """Flatten every page to a JPEG. The last resort — text stops being text."""
    src = fitz.open(in_path)
    out = fitz.open()
    for page in src:
        rect = page.rect
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        img = Image.frombytes("RGB" if pix.n >= 3 else "L",
                              (pix.width, pix.height), pix.samples)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        new = out.new_page(width=rect.width, height=rect.height)
        new.insert_image(new.rect, stream=buf.getvalue())
    data = out.tobytes(garbage=4, deflate=True)
    out.close()
    src.close()
    return data


_PDF_IMAGE_RUNGS = tuple((s, q) for s in (1.0, 0.85, 0.7, 0.6, 0.5, 0.4)
                         for q in (85, 75, 65, 55, 45, 35))
_PDF_RASTER_RUNGS = tuple((d, q) for d in (200, 170, 150, 130, 110, 96, 84, 72)
                          for q in (80, 70, 60, 50, 40, 30))


def compress_pdf(in_path: str, out_path: str, target_bytes: int,
                 keep_text: bool = True,
                 progress: Progress = _noop) -> CompressResult:
    """Shrink a PDF to `target_bytes` in up to three escalating stages.

    1. Rebuild it losslessly — deduplicate, deflate, drop orphaned objects.
       Files exported by careless software often fall by half on this alone,
       with nothing whatsoever given up.
    2. Re-encode the embedded images, keeping text selectable.
    3. Only if that cannot reach the target (and only when allowed), flatten the
       pages to pictures, which always gets there but ends selectable text.
    """
    original = os.path.getsize(in_path)

    if original <= target_bytes:
        # Still worth a lossless pass, but never at the cost of growing it.
        doc = fitz.open(in_path)
        data = _pdf_bytes(doc)
        doc.close()
        if len(data) >= original:
            data = Path(in_path).read_bytes()
            method = "copied unchanged (already under target)"
        else:
            method = "lossless rebuild"
        Path(out_path).write_bytes(data)
        return CompressResult(out_path, original, len(data), target_bytes,
                              method, True)

    progress(0.05, "Rebuilding losslessly…")
    doc = fitz.open(in_path)
    lossless = _pdf_bytes(doc)
    doc.close()
    if len(lossless) <= target_bytes:
        Path(out_path).write_bytes(lossless)
        progress(1.0, f"Wrote {_kb(len(lossless))}")
        return CompressResult(out_path, original, len(lossless), target_bytes,
                              "lossless rebuild", True)

    progress(0.15, "Re-encoding embedded images…")
    data, rung, ok = _search_ladder(
        _PDF_IMAGE_RUNGS,
        lambda r: _recompress_embedded(in_path, r[0], r[1]),
        target_bytes,
        lambda f, m: progress(0.15 + f * 0.5, m), "images: ")
    method = (f"image re-encode (quality {rung[1]}, "
              f"{rung[0] * 100:.0f}% size), text preserved")

    if not ok and not keep_text:
        progress(0.7, "Still too large — flattening pages to images…")
        rdata, rrung, rok = _search_ladder(
            _PDF_RASTER_RUNGS,
            lambda r: _rasterise(in_path, r[0], r[1]),
            target_bytes,
            lambda f, m: progress(0.7 + f * 0.28, m), "pages: ")
        if len(rdata) < len(data):
            data, ok = rdata, rok
            method = (f"pages flattened to images at {rrung[0]} dpi, "
                      f"quality {rrung[1]} — text no longer selectable")

    if len(data) >= len(lossless):
        data, method, ok = lossless, "lossless rebuild", False

    Path(out_path).write_bytes(data)
    progress(1.0, f"Wrote {_kb(len(data))}")
    return CompressResult(out_path, original, len(data), target_bytes,
                          method, ok)


def compress(in_path: str, out_path: str, target_kb: int,
             keep_text: bool = True, allow_format_change: bool = True,
             progress: Progress = _noop) -> CompressResult:
    """Compress a PDF, JPEG or PNG down to roughly `target_kb` kilobytes."""
    target_bytes = max(int(target_kb), 1) * 1024
    kind = kind_of(in_path)
    if kind == "pdf":
        return compress_pdf(in_path, out_path, target_bytes, keep_text,
                            progress)
    if kind == "image":
        return compress_image(in_path, out_path, target_bytes,
                              allow_format_change, progress)
    raise ValueError(f"Cannot compress {Path(in_path).suffix or 'that'} files — "
                     "PDF, JPEG and PNG only.")


def _pad_media(data: bytes, ext: str, target: int) -> bytes:
    """Use format-defined nonvisual data, keeping pixels and PDF objects intact."""
    extra = target - len(data)
    if extra <= 0:
        return data
    if ext == ".pdf":
        # Readers look for startxref near EOF, so keep that entire trailer
        # together. Existing object/xref offsets precede this insertion.
        end = data.rfind(b"startxref")
        if end < 0:
            raise ValueError("The PDF has no cross-reference trailer.")
        return data[:end] + b" " * extra + data[end:]
    if ext == ".png":
        if extra < 12:
            raise ValueError("PNG padding needs at least 12 bytes; choose a slightly larger target.")
        if data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82":
            raise ValueError("The PNG has an unexpected end marker.")
        payload = b"\x00" * (extra - 12)
        chunk = b"npAD" + payload
        return (data[:-12] + struct.pack(">I", len(payload)) + chunk
                + struct.pack(">I", zlib.crc32(chunk)) + data[-12:])
    if ext in {".jpg", ".jpeg"}:
        if extra < 4:
            raise ValueError("JPEG padding needs at least 4 bytes; choose a slightly larger target.")
        parts = []
        while extra:
            size = min(extra, 65537)
            if 0 < extra - size < 4:
                size -= 4 - (extra - size)
            parts.append(b"\xff\xfe" + struct.pack(">H", size - 2) + b" " * (size - 4))
            extra -= size
        return data[:2] + b"".join(parts) + data[2:]
    raise ValueError("Choose a PDF, JPEG or PNG file.")


def resize_file(in_path: str, out_path: str, target_kb: int,
                keep_text: bool = True, allow_format_change: bool = True,
                progress: Progress = _noop) -> CompressResult:
    """Set an exact byte size, compressing first when necessary. Never upscale pixels."""
    src, dest = Path(in_path), Path(out_path)
    if src.resolve() == dest.resolve():
        raise ValueError("Choose a different output file to keep the original intact.")
    if not kind_of(in_path):
        raise ValueError("Choose a PDF, JPEG or PNG file.")
    if target_kb <= 0:
        raise ValueError("Target size must be greater than zero.")
    target = int(target_kb) * 1024
    original = src.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Work beside the destination: failed compression/padding leaves existing
    # outputs intact, and replacement stays atomic on the same filesystem.
    with tempfile.TemporaryDirectory(dir=dest.parent) as scratch:
        temp = Path(scratch) / dest.name
        if original > target:
            result = compress(str(src), str(temp), target_kb, keep_text,
                              allow_format_change, progress)
            data = Path(result.path).read_bytes()
            if not result.reached:
                final = dest.with_suffix(Path(result.path).suffix)
                if final.resolve() == src.resolve():
                    raise ValueError("Choose a different output file to keep the original intact.")
                os.replace(result.path, final)
                result.path = str(final)
                return result
            ext = Path(result.path).suffix.lower()
            method = result.method + ", padded to exact size"
        else:
            data = src.read_bytes()
            ext = src.suffix.lower()
            method = "nonvisual padding (quality unchanged)"
        padded = _pad_media(data, ext, target)
        temp.write_bytes(padded)
        final = dest.with_suffix(ext)
        if final.resolve() == src.resolve():
            raise ValueError("Choose a different output file to keep the original intact.")
        os.replace(temp, final)
    progress(1.0, f"Wrote {_kb(target)} exactly")
    return CompressResult(str(final), original, len(padded), target, method, True)


def pdf_to_images(pdf_path: str, out_dir: str, dpi: int = 200,
                  fmt: str = "png", pages: Iterable[int] | None = None,
                  quality: int = 92,
                  progress: Progress = _noop) -> list[str]:
    """Render pages to `out_dir` as `<stem>_page_001.png`. Returns the paths."""
    fmt = fmt.lower().lstrip(".")
    if fmt not in ("png", "jpg", "jpeg"):
        raise ValueError("Pictures can be written as PNG or JPEG.")
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(pdf_path).stem

    written: list[str] = []
    with fitz.open(pdf_path) as doc:
        wanted = list(pages) if pages is not None else list(range(doc.page_count))
        width = max(3, len(str(doc.page_count)))
        for i, pno in enumerate(wanted):
            pix = doc[pno].get_pixmap(dpi=dpi, alpha=False)
            img = Image.frombytes("RGB" if pix.n >= 3 else "L",
                                  (pix.width, pix.height), pix.samples)
            path = folder / f"{stem}_page_{pno + 1:0{width}d}.{fmt}"
            if fmt == "png":
                img.save(path, "PNG", optimize=True)
            else:
                img.convert("RGB").save(path, "JPEG", quality=quality,
                                        optimize=True, progressive=True)
            written.append(str(path))
            progress((i + 1) / len(wanted),
                     f"Page {pno + 1} -> {path.name}")
    return written


_PAGE_SIZES = {
    "a4": (595.276, 841.890),
    "letter": (612.0, 792.0),
}


def images_to_pdf(image_paths: Sequence[str], out_path: str,
                  page_size: str = "image", dpi: int = 200,
                  margin_pt: float = 0.0, quality: int = 92,
                  progress: Progress = _noop) -> str:
    """Bind pictures into one PDF, in the order given.

    `page_size` is "image" — each page cut to fit its picture exactly, which is
    what you want for scans — or "a4"/"letter", where each picture is centred on
    a standard sheet, portrait or landscape following the picture itself.
    """
    if not image_paths:
        raise ValueError("Choose at least one picture.")
    page_size = page_size.lower()
    if page_size not in _PAGE_SIZES and page_size != "image":
        raise ValueError(f"Unknown page size: {page_size}")

    out = fitz.open()
    for i, path in enumerate(image_paths):
        img = _flatten(_open_image(path))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        blob = buf.getvalue()

        # 72 pt to the inch: a 2000 px wide scan taken at 200 dpi is 10 inches
        # of paper, so it lands as 720 pt however many pixels it happens to be.
        pw, ph = img.width * 72.0 / dpi, img.height * 72.0 / dpi

        if page_size == "image":
            page = out.new_page(width=pw, height=ph)
            page.insert_image(page.rect, stream=blob)
        else:
            sw, sh = _PAGE_SIZES[page_size]
            if pw > ph:                       # landscape picture, landscape page
                sw, sh = sh, sw
            page = out.new_page(width=sw, height=sh)
            box = fitz.Rect(margin_pt, margin_pt, sw - margin_pt, sh - margin_pt)
            fit = min(box.width / pw, box.height / ph)
            w, h = pw * fit, ph * fit
            x = (sw - w) / 2
            y = (sh - h) / 2
            page.insert_image(fitz.Rect(x, y, x + w, y + h), stream=blob)

        progress((i + 1) / len(image_paths),
                 f"Added {Path(path).name} ({i + 1}/{len(image_paths)})")

    out.save(out_path, garbage=4, deflate=True)
    out.close()
    return out_path
