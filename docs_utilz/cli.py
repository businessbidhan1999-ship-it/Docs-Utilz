"""Batch watermark removal, compression, and PDF/image conversion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import engine
from . import tools
from .engine import Settings

SUBCOMMANDS = ("compress", "pdf2img", "img2pdf")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Remove a watermark from a PDF or picture, entirely "
                    "offline.")
    p.add_argument("pdf", help="input PDF or picture (.pdf/.png/.jpg/.jpeg)")
    p.add_argument("watermark", help="cropped image of the watermark")
    p.add_argument("-o", "--output", help="output file (default: *_clean.*)")
    p.add_argument("--mode", choices=["raster", "smart"], default="raster",
                   help="raster = always works; smart = keeps text selectable")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--threshold", type=float, default=0.62,
                   help="match confidence 0-1 (lower finds more)")
    p.add_argument("--verify", type=float, default=0.42,
                   help="shape agreement required 0-1 (lower finds more)")
    p.add_argument("--fill", default="clean",
                   choices=[k for k, _, _ in engine.FILL_MODES],
                   help="how to erase; " + " | ".join(
                       f"{k}: {engine.FILL_LABELS[k].lower()}"
                       for k, _, _ in engine.FILL_MODES))
    p.add_argument("--targets", default="images,graphics",
                   help="smart mode only: images,graphics,text")
    p.add_argument("--rotations", action="store_true",
                   help="also search rotated copies (slower)")
    p.add_argument("--pages", default="",
                   help="e.g. 1-3,7 (default: all pages)")
    p.add_argument("--dry-run", action="store_true",
                   help="report detections without writing a file")
    return p


def parse_pages(text: str, total: int) -> tuple[int, ...] | None:
    if not text.strip():
        return None
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            out.update(range(int(a) - 1, int(b)))
        else:
            out.add(int(part) - 1)
    return tuple(sorted(p for p in out if 0 <= p < total))


def run_watermark(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if not Path(args.pdf).is_file():
        print(f"No such file: {args.pdf}", file=sys.stderr)
        return 2
    if not Path(args.watermark).is_file():
        print(f"No such image: {args.watermark}", file=sys.stderr)
        return 2

    total = engine.page_count(args.pdf)

    s = Settings(
        mode=args.mode,
        dpi=args.dpi,
        threshold=args.threshold,
        verify_iou=args.verify,
        fill=args.fill,
        angles=((-45.0, -30.0, 0.0, 30.0, 45.0) if args.rotations else (0.0,)),
        smart_targets=tuple(t.strip() for t in args.targets.split(",")
                            if t.strip()),
        pages=parse_pages(args.pages, total),
    )

    if args.dry_run:
        pages = s.pages or range(total)
        grand = 0
        for pno in pages:
            _, _, matches = engine.preview(args.pdf, args.watermark, s, pno)
            grand += len(matches)
            best = max((m.score for m in matches), default=0.0)
            print(f"page {pno + 1:>4}: {len(matches):>3} match(es)"
                  f"  best score {best:.2f}")
        print(f"total: {grand} watermark instance(s)")
        return 0

    suffix = ".pdf" if engine.is_pdf(args.pdf) else Path(args.pdf).suffix
    out = args.output or f"{Path(args.pdf).with_suffix('')}_clean{suffix}"
    if Path(out).resolve() == Path(args.pdf).resolve():
        print("Refusing to overwrite the input file.", file=sys.stderr)
        return 2

    rep = engine.process(args.pdf, args.watermark, out, s,
                         lambda f, m: print(f"[{f * 100:5.1f}%] {m}"))
    print(f"\nRemoved {rep['hits']} watermark instance(s) "
          f"across {rep['processed']} page(s).")
    print(f"Saved: {out}")
    return 0

# Compression


def run_compress(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="docs-utilz compress",
        description="Shrink PDFs, JPEGs and PNGs to a target size.")
    p.add_argument("files", nargs="+", help="files to compress")
    p.add_argument("-t", "--target", type=int, default=1000,
                   help="target size in KB (default: 1000)")
    p.add_argument("--outdir", help="where to write (default: beside each "
                                    "original, named *_small)")
    p.add_argument("--suffix", default="_small",
                   help="added to each name (default: _small)")
    p.add_argument("--flatten", action="store_true",
                   help="PDF: allow flattening pages to pictures if that is "
                        "the only way to reach the target (ends selectable "
                        "text)")
    p.add_argument("--no-format-change", action="store_true",
                   help="PNG: never fall back to JPEG")
    args = p.parse_args(argv)

    if args.outdir:
        Path(args.outdir).mkdir(parents=True, exist_ok=True)

    failures = 0
    for src in args.files:
        path = Path(src)
        if not path.is_file():
            print(f"No such file: {src}", file=sys.stderr)
            failures += 1
            continue
        if not tools.kind_of(src):
            print(f"Skipping {path.name} — only PDF, JPEG and PNG.",
                  file=sys.stderr)
            failures += 1
            continue

        name = f"{path.stem}{args.suffix}{path.suffix}"
        dest = Path(args.outdir) / name if args.outdir else path.with_name(name)
        if dest.resolve() == path.resolve():
            print(f"Refusing to overwrite {path.name}.", file=sys.stderr)
            failures += 1
            continue

        res = tools.compress(str(path), str(dest), args.target,
                             keep_text=not args.flatten,
                             allow_format_change=not args.no_format_change,
                             progress=lambda f, m: print(f"  {m}"))
        print(f"{path.name}: {res.summary()}")
        print(f"  -> {res.path}")
        if not res.reached:
            failures += 1
    return 1 if failures else 0


def run_pdf2img(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="docs-utilz pdf2img",
        description="Render the pages of a PDF as pictures.")
    p.add_argument("pdf")
    p.add_argument("--outdir", help="default: <pdf name>_pages next to it")
    p.add_argument("--format", default="png", choices=["png", "jpg", "jpeg"])
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--quality", type=int, default=92, help="JPEG only")
    p.add_argument("--pages", default="", help="e.g. 1-3,7 (default: all)")
    args = p.parse_args(argv)

    if not Path(args.pdf).is_file():
        print(f"No such PDF: {args.pdf}", file=sys.stderr)
        return 2

    outdir = args.outdir or f"{Path(args.pdf).with_suffix('')}_pages"
    total = engine.page_count(args.pdf)
    written = tools.pdf_to_images(
        args.pdf, outdir, args.dpi, args.format,
        parse_pages(args.pages, total), args.quality,
        progress=lambda f, m: print(f"[{f * 100:5.1f}%] {m}"))
    print(f"\nWrote {len(written)} picture(s) into {outdir}")
    return 0


def run_img2pdf(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="docs-utilz img2pdf",
        description="Bind pictures into a single PDF, in the order given.")
    p.add_argument("images", nargs="+")
    p.add_argument("-o", "--output", help="default: <first picture>.pdf")
    p.add_argument("--size", default="image", choices=["image", "a4", "letter"],
                   help="page size (default: each page fits its picture)")
    p.add_argument("--dpi", type=int, default=200,
                   help="what the pictures' pixels mean in inches")
    p.add_argument("--margin", type=float, default=0.0,
                   help="margin in points, for a4/letter")
    p.add_argument("--quality", type=int, default=92)
    args = p.parse_args(argv)

    missing = [i for i in args.images if not Path(i).is_file()]
    if missing:
        print("No such picture: " + ", ".join(missing), file=sys.stderr)
        return 2

    out = args.output or str(Path(args.images[0]).with_suffix(".pdf"))
    tools.images_to_pdf(args.images, out, args.size, args.dpi, args.margin,
                        args.quality,
                        progress=lambda f, m: print(f"[{f * 100:5.1f}%] {m}"))
    size = tools.human_size(Path(out).stat().st_size)
    print(f"\nSaved: {out} ({size})")
    return 0


def _make_console_forgiving() -> None:
    """Stop an unprintable character from killing a batch job on Windows.

    A Windows console is still cp1252 by default, and printing anything outside
    it — an arrow in a progress line, an accent in a filename — raises
    UnicodeEncodeError mid-run. The job is then lost over a decoration. Asking
    for replacement characters instead means the worst case is a "?" in the
    progress output rather than an abandoned document.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_console_forgiving()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        return {"compress": run_compress,
                "pdf2img": run_pdf2img,
                "img2pdf": run_img2pdf}[argv[0]](argv[1:])
    return run_watermark(argv)


if __name__ == "__main__":
    raise SystemExit(main())
