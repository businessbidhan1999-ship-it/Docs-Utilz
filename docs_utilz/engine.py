"""Watermark detection and removal using PDF redaction or raster processing."""

from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

import cv2
import numpy as np

try:                       # PyMuPDF 1.24+ renamed itself; `fitz` still works
    import pymupdf as fitz  # but prints a deprecation warning on import
except ImportError:        # pragma: no cover — older PyMuPDF
    import fitz


@dataclass
class Match:
    """One detected watermark instance, in pixel coords of the rendered page."""
    x: int
    y: int
    w: int
    h: int
    score: float
    angle: float = 0.0
    scale: float = 1.0
    template: np.ndarray | None = None  # the transformed template that matched

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.w, self.y + self.h


@dataclass
class Settings:
    mode: str = "raster"              # "raster" | "smart"
    dpi: int = 200                    # render resolution for detection/output
    threshold: float = 0.62           # template-match confidence 0..1
    scale_min: float = 0.55
    scale_max: float = 1.85
    scale_steps: int = 13
    angles: tuple[float, ...] = (0.0,)  # extra rotations to try, in degrees
    padding: int = 6                  # grow each hit by N px before erasing
    # See FILL_MODES for the available erasing strategies.
    fill: str = "clean"
    protect_dark: bool = True         # don't erase pixels darker than...
    dark_cutoff: int = 110            # ...this gray level (real text)
    smart_targets: tuple[str, ...] = ("images", "graphics")  # + "text"
    verify_iou: float = 0.42          # shape agreement required to accept a hit
    # A mark lying over body text correlates far worse than one on blank paper —
    # the text it covers is noise as far as the match is concerned — so hits
    # this weak are still considered, and judged on shape instead. See detect().
    weak_margin: float = 0.22
    weak_verify_iou: float = 0.60
    max_hits_per_page: int = 60
    pages: tuple[int, ...] | None = None  # None = all pages (0-based)


# Shared fill labels and descriptions for the GUI and CLI.
FILL_MODES: tuple[tuple[str, str, str], ...] = (
    ("clean",
     "Erase completely (recommended)",
     "Rebuilds the paper under the mark, so nothing faint is left behind and "
     "the patch matches the page around it. Text underneath is recovered."),
    ("unblend",
     "Lift the mark, keep everything under it",
     "Cancels the mark's tint but leaves every other pixel alone. Safest when "
     "the mark sits on top of content, but a very strong mark can leave a "
     "faint shadow."),
    ("inpaint",
     "Paint over from the surroundings",
     "Lets OpenCV invent the missing pixels from the edges of the hole. Good "
     "over busy backgrounds, tends to smear over plain paper."),
    ("background",
     "Flat paper colour",
     "Fills the mark's strokes with one measured paper colour. Fast and "
     "predictable; can show as a patch on an unevenly lit scan."),
    ("box",
     "Blank the whole rectangle",
     "Wipes every pixel of the detected rectangle, watermark or not. The last "
     "resort when nothing else catches the mark."),
)

FILL_LABELS = {key: label for key, label, _ in FILL_MODES}
FILL_HELP = {key: text for key, _, text in FILL_MODES}


Progress = Callable[[float, str], None]


def _noop(_p: float, _m: str) -> None:
    pass


def pixmap_to_rgb(pix: "fitz.Pixmap") -> np.ndarray:
    """PyMuPDF Pixmap -> HxWx3 uint8 RGB array (stride-safe)."""
    buf = np.frombuffer(pix.samples, dtype=np.uint8)
    buf = buf.reshape(pix.height, pix.stride)[:, : pix.width * pix.n]
    img = buf.reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    if pix.n == 4:
        return cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
    return np.ascontiguousarray(img[:, :, :3])


def render_page(page: "fitz.Page", dpi: int) -> np.ndarray:
    return pixmap_to_rgb(page.get_pixmap(dpi=dpi, alpha=False))


def is_pdf(path: str) -> bool:
    return str(path).lower().endswith(".pdf")


def read_image(path: str) -> np.ndarray:
    """Read a picture from disk as HxWx3 RGB, compositing away any alpha.

    `np.fromfile` rather than `cv2.imread` because OpenCV's own reader cannot
    open a path containing non-ASCII characters on Windows, which office
    documents very often do.
    """
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    if img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        bgr = img[:, :, :3].astype(np.float32)
        img = (bgr * alpha + 255.0 * (1 - alpha)).astype(np.uint8)
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2RGB)


def write_image(path: str, rgb: np.ndarray, quality: int = 95) -> None:
    """Write RGB out, choosing the encoder from the file's extension."""
    ext = os.path.splitext(path)[1].lower() or ".png"
    params = ([int(cv2.IMWRITE_JPEG_QUALITY), quality]
              if ext in (".jpg", ".jpeg") else [])
    ok, buf = cv2.imencode(ext, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), params)
    if not ok:
        raise RuntimeError(f"Could not encode {path}")
    with open(path, "wb") as fh:
        fh.write(buf.tobytes())


def load_template(path: str) -> np.ndarray:
    """Load the watermark snippet as grayscale, auto-trimming flat borders."""
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"Could not read image: {path}")

    # If the snippet has alpha, composite it onto white so transparent
    # regions don't read as black.
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        rgb = img[:, :, :3].astype(np.float32)
        img = (rgb * alpha + 255.0 * (1 - alpha)).astype(np.uint8)

    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return _trim_flat_border(gray)


def _trim_flat_border(gray: np.ndarray, tol: int = 6) -> np.ndarray:
    """Crop away uniform margins left over from a sloppy screenshot crop."""
    bg = int(np.median(np.concatenate([
        gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]
    ])))
    mask = np.abs(gray.astype(np.int16) - bg) > tol
    if not mask.any():
        return gray
    ys, xs = np.where(mask)
    y0, y1 = max(int(ys.min()) - 1, 0), min(int(ys.max()) + 2, gray.shape[0])
    x0, x1 = max(int(xs.min()) - 1, 0), min(int(xs.max()) + 2, gray.shape[1])
    cropped = gray[y0:y1, x0:x1]
    return cropped if cropped.size >= 64 else gray


def _rotate_keep(img: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 1e-6:
        return img
    h, w = img.shape[:2]
    bg = float(np.median(img))
    rad = math.radians(angle)
    cos, sin = abs(math.cos(rad)), abs(math.sin(rad))
    nw, nh = int(w * cos + h * sin), int(w * sin + h * cos)
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=bg)


def _iou(a: Match, b: Match) -> float:
    ax0, ay0, ax1, ay1 = a.box
    bx0, by0, bx1, by1 = b.box
    ix = max(0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    if inter == 0:
        return 0.0
    union = a.w * a.h + b.w * b.h - inter
    return inter / union


def _nms(matches: list[Match], iou_thresh: float = 0.3,
         limit: int = 60) -> list[Match]:
    kept: list[Match] = []
    for m in sorted(matches, key=lambda x: -x.score):
        if all(_iou(m, k) < iou_thresh for k in kept):
            kept.append(m)
            if len(kept) >= limit:
                break
    return kept


def _match_at(page_gray: np.ndarray, tmpl_base: np.ndarray,
              scale: float) -> tuple[np.ndarray, np.ndarray] | None:
    ph, pw = page_gray.shape
    tw = max(int(round(tmpl_base.shape[1] * scale)), 8)
    th = max(int(round(tmpl_base.shape[0] * scale)), 8)
    if th >= ph or tw >= pw:
        return None
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
    tmpl = cv2.resize(tmpl_base, (tw, th), interpolation=interp)
    return cv2.matchTemplate(page_gray, tmpl, cv2.TM_CCOEFF_NORMED), tmpl


def detect(page_rgb: np.ndarray, template: np.ndarray,
           s: Settings) -> list[Match]:
    """Coarse-to-fine, multi-scale (optionally multi-angle) template match.

    A watermark repeats at one consistent size, so we first sweep coarsely to
    find *which* scale/angle the watermark lives at, then re-search finely
    around that winner and collect every instance above the threshold. This is
    both faster and far more accurate than one flat sweep, and it tolerates the
    snippet being captured at a different DPI than the one used for processing.
    """
    page_gray = cv2.cvtColor(page_rgb, cv2.COLOR_RGB2GRAY)
    lo, hi = min(s.scale_min, 1.0), max(s.scale_max, 1.0)

    # -- stage 1: coarse sweep, log-spaced, always including 1.0 --
    coarse = sorted({1.0, *np.exp(np.linspace(
        np.log(lo), np.log(hi), max(s.scale_steps, 5))).tolist()})

    best = (-1.0, 1.0, 0.0)  # score, scale, angle
    rotations: dict[float, np.ndarray] = {
        a: _rotate_keep(template, a) for a in s.angles}

    for angle, rot in rotations.items():
        for sc in coarse:
            got = _match_at(page_gray, rot, sc)
            if got is None:
                continue
            res, _ = got
            peak = float(res.max())
            if peak > best[0]:
                best = (peak, sc, angle)

    _, best_scale, best_angle = best
    rot = rotations[best_angle]

    # -- stage 2: fine sweep around the winner, gathering every instance --
    #
    # The floor is deliberately below the asked-for threshold. The same mark
    # scores very differently depending on what it lands on: on blank paper it
    # correlates near 1.0, but over a paragraph of body text the text underneath
    # counts as noise and the score falls by a third — often under any threshold
    # loose enough to be safe. Those instances are the ones people find left
    # behind afterwards. So weak candidates are kept here and made to earn their
    # place on shape agreement instead, which does not care what the mark landed
    # on. Correlation decides *where* to look; shape decides whether it is real.
    floor = max(0.30, s.threshold - s.weak_margin)
    fine = sorted({best_scale * f for f in
                   np.linspace(0.94, 1.06, 7).tolist()} | {best_scale})
    hits: list[Match] = []
    for sc in fine:
        got = _match_at(page_gray, rot, sc)
        if got is None:
            continue
        res, tmpl = got
        # Each instance produces a broad hill of high scores, so thresholding
        # alone yields thousands of near-identical boxes per mark. Only the
        # summit of each hill is a candidate, which keeps the list short enough
        # for the (much more expensive) shape check on every one of them.
        k = max(3, (min(tmpl.shape[:2]) // 2) | 1)
        summit = cv2.dilate(res, np.ones((k, k), np.float32))
        ys, xs = np.where((res >= floor) & (res >= summit - 1e-6))
        if len(xs) > 600:
            order = np.argsort(res[ys, xs])[-600:]
            ys, xs = ys[order], xs[order]
        for y, x in zip(ys, xs):
            hits.append(Match(int(x), int(y), tmpl.shape[1], tmpl.shape[0],
                              float(res[y, x]), best_angle, float(sc), tmpl))

    hits = _nms(hits, limit=s.max_hits_per_page * 4)
    verified = []
    for m in hits:
        agreement = _shape_agreement(page_gray, m)
        needed = (s.verify_iou if m.score >= s.threshold
                  else max(s.verify_iou, s.weak_verify_iou))
        if agreement >= needed:
            verified.append(m)
    verified.sort(key=lambda m: -m.score)
    return verified[:s.max_hits_per_page]


def _binary_ink(gray: np.ndarray) -> np.ndarray:
    """Pixels that differ from the local paper colour — i.e. actual marks."""
    bg = float(np.median(gray))
    return np.abs(gray.astype(np.int16) - bg) > 20


def _shape_agreement(page_gray: np.ndarray, m: Match) -> float:
    """IoU between the template's strokes and the strokes actually on the page.

    Correlation alone happily fires on unrelated body text, because a page of
    dark glyphs on white correlates with almost any other page of dark glyphs
    on white. Comparing the *shapes* rejects those: a real watermark instance
    has ink in the same places as the template, a false one does not.
    """
    if m.template is None:
        return 1.0
    roi = page_gray[m.y:m.y + m.h, m.x:m.x + m.w]
    if roi.shape != m.template.shape:
        return 0.0
    a = _binary_ink(m.template)
    b = _binary_ink(roi)
    if not a.any() or not b.any():
        return 0.0
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union)


def _ink_mask(tmpl: np.ndarray, strength: float = 0.18) -> np.ndarray:
    """Boolean mask of the watermark's own strokes inside the template.

    `strength` sets how far from the snippet's paper colour a pixel must be to
    count as a stroke. The default is deliberately strict — it is what shape
    verification compares against. Erasing uses a looser value so that the
    anti-aliased fringe around each stroke is lifted too; leaving that fringe
    behind is what makes a "removed" watermark still faintly readable.
    """
    bg = int(np.median(np.concatenate([
        tmpl[0, :], tmpl[-1, :], tmpl[:, 0], tmpl[:, -1]
    ])))
    diff = np.abs(tmpl.astype(np.int16) - bg)
    thr = max(5, int(diff.max() * strength))
    return diff > thr


def _paper_colour(page_rgb: np.ndarray, box: tuple[int, int, int, int],
                  mask: np.ndarray, margin: int = 12) -> np.ndarray:
    """Estimate the paper colour immediately around one watermark.

    Sampled locally rather than page-wide because a phone scan is never evenly
    lit: the paper by the footer can be several levels darker than the paper at
    the top, and filling with a page-wide average leaves a visible patch. The
    high percentile picks the paper out even when body text is in the window.
    """
    h, w = page_rgb.shape[:2]
    y0, y1, x0, x1 = box
    ey0, ey1 = max(y0 - margin, 0), min(y1 + margin, h)
    ex0, ex1 = max(x0 - margin, 0), min(x1 + margin, w)
    big = page_rgb[ey0:ey1, ex0:ex1]
    taken = np.zeros(big.shape[:2], bool)
    taken[y0 - ey0:y1 - ey0, x0 - ex0:x1 - ex0] = mask
    free = big[~taken]
    if len(free) < 16:
        return np.full(3, 255.0, np.float32)
    return np.percentile(free.astype(np.float32), 80, axis=0)


_KERNEL3 = np.ones((3, 3), np.uint8)


def _region_masks(m: Match, box: tuple[int, int, int, int], grow: int,
                  whole_box: bool) -> tuple[np.ndarray, np.ndarray]:
    """The watermark's strokes (`core`) and strokes-plus-fringe (`wide`).

    `core` is measured from the template strictly, so it samples the mark's own
    colour without the pale anti-aliased rim dragging the average up. `wide`
    is the mask that actually gets erased, and is deliberately looser: leaving
    the rim behind is exactly what makes a "removed" watermark still faintly
    readable.
    """
    y0, y1, x0, x1 = box
    shape = (y1 - y0, x1 - x0)
    core = np.zeros(shape, bool)
    wide = np.zeros(shape, bool)
    if whole_box or m.template is None:
        core[:] = True
        wide[:] = True
        return core, wide

    iy0, ix0 = m.y - y0, m.x - x0
    core[iy0:iy0 + m.h, ix0:ix0 + m.w] = _ink_mask(m.template)
    fringe = cv2.dilate(_ink_mask(m.template, 0.09).astype(np.uint8),
                        _KERNEL3, iterations=grow).astype(bool)
    wide[iy0:iy0 + m.h, ix0:ix0 + m.w] = fringe
    return core, wide


def erase(page_rgb: np.ndarray, matches: Iterable[Match],
          s: Settings) -> tuple[np.ndarray, np.ndarray]:
    """Erase every match from the page. Returns (clean_page, mask_used)."""
    out = page_rgb.copy()
    gray = cv2.cvtColor(page_rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    full_mask = np.zeros((h, w), np.uint8)

    for m in matches:
        # The complete-erase mode reconstructs the paper rather than touching
        # single strokes, so it can afford — and needs — a wider working margin:
        # partly to have clean paper to read the lighting from, partly because
        # the bit of the mark that a slightly tight crop left out lies just
        # beyond the template's own footprint and has to be inside the region to
        # be caught at all.
        pad = (s.padding + int(np.clip(0.05 * max(m.w, m.h), 8, 24))
               if s.fill == "clean" else s.padding)
        x0 = max(m.x - pad, 0)
        y0 = max(m.y - pad, 0)
        x1 = min(m.x + m.w + pad, w)
        y1 = min(m.y + m.h + pad, h)
        if x1 <= x0 or y1 <= y0:
            continue

        roi = page_rgb[y0:y1, x0:x1]
        groi = gray[y0:y1, x0:x1]

        if s.fill == "clean":
            used = _clean_region(out[y0:y1, x0:x1], roi, groi, m,
                                 (y0, y1, x0, x1), s)
            full_mask[y0:y1, x0:x1] |= used.astype(np.uint8)
            continue

        core, wide = _region_masks(m, (y0, y1, x0, x1), 2,
                                   whole_box=(s.fill == "box"))

        if s.protect_dark and s.fill in ("inpaint", "background"):
            # Keep genuine body text that happens to sit under the watermark —
            # but only what is clearly darker than the watermark itself, or a
            # mark as dark as this one would be protected from its own removal.
            level = float(np.median(groi[core])) if core.any() else 255.0
            cutoff = min(float(s.dark_cutoff), level - 25.0)
            wide &= groi >= cutoff

        if not wide.any():
            continue

        if s.fill == "unblend":
            _unblend_region(out[y0:y1, x0:x1], roi, core, wide,
                            _paper_colour(page_rgb, (y0, y1, x0, x1), wide))
        elif s.fill != "inpaint":
            paper = _paper_colour(page_rgb, (y0, y1, x0, x1), wide)
            out[y0:y1, x0:x1][wide] = paper.astype(np.uint8)

        full_mask[y0:y1, x0:x1] |= wide.astype(np.uint8)

    if s.fill == "inpaint" and full_mask.any():
        out = cv2.inpaint(out, full_mask, 3, cv2.INPAINT_TELEA)

    return out, full_mask


def _unblend_region(dst: np.ndarray, roi: np.ndarray, core: np.ndarray,
                    wide: np.ndarray, paper: np.ndarray) -> None:
    """Lift a translucent watermark out of one region, in place.

    A watermark is usually a wash laid over the page, so rather than painting
    over it we rescale each colour channel so that the watermark's own colour
    lands exactly on the surrounding paper colour, then clamp there. The fringe
    is lighter than the strokes, so it clamps to paper as well and disappears.
    Ink underneath is far darker, survives the same scaling, and simply becomes
    legible again — no inpainting smear.

    The scaling is per channel because watermarks are rarely neutral grey:
    CamScanner's is blue. One shared factor would blow out the blue and green
    while leaving red behind, staining the page cyan where the mark used to be.

    How far to open the scale up is read off the page itself. A translucent
    mark composites as `seen = (1-a)·original + a·colour`, so the recovery is
    `original = paper + (seen - mark)/(1-a)` — an affine map that pins the
    mark's own tone to paper. That leaves only `1-a` to find, and the darkest
    thing under the mark supplies it: document ink is very nearly black, so
    however far the darkest covered pixel sits below the mark's tone is how
    much of the original still shows through. Recovering the strength this way
    is what keeps letters under the watermark at roughly the weight of the rest
    of their line instead of washed out.

    Where nothing dark is covered — the ordinary footer mark on blank paper —
    there is nothing to measure and the darkest pixel is the mark itself, so
    that estimate is discarded and the scale simply runs from black.
    """
    src = core if core.any() else wide
    roi = roi.astype(np.float32)
    mark = np.percentile(roi[src], 35, axis=0)
    dark = np.percentile(roi[wide], 2, axis=0)
    dark = np.where(mark - dark >= 0.25 * paper, dark, 0.0)

    slope = np.clip(paper / np.maximum(mark - dark, 12.0), 1.0, 4.0)
    lifted = np.clip(paper + (roi - mark) * slope, 0.0, paper)
    dst[wide] = lifted[wide].astype(np.uint8)


def _paper_surface(roi: np.ndarray, groi: np.ndarray,
                   hide: np.ndarray) -> np.ndarray:
    """A smooth picture of the paper alone, over the whole region.

    A single paper colour is not enough to make a patch invisible: a phone scan
    is lit unevenly, so paper that is 246 at the top of the mark can be 231 at
    the bottom, and one flat value shows up as a rectangle. So everything that
    is not paper is cut out, the holes are filled in from their edges, and the
    result is blurred hard — which throws away the filled-in detail (it was
    invented anyway) and keeps only the slow gradient of the lighting.

    What counts as "not paper" cannot be taken from the template alone. The
    crop someone snips is never exactly the mark: clip a few pixels off one
    side and the rest of the mark stays outside every mask, sits in the region
    while the paper is being measured, drags the estimate darker, and the mark
    is then not merely left behind but surrounded by a dark halo. So the region
    is asked directly instead: the brightest pixel in each small neighbourhood
    is paper, and anything meaningfully below its own neighbourhood's maximum is
    something lying on the paper, whatever put it there.
    """
    local_paper = cv2.dilate(groi, np.ones((17, 17), np.uint8))
    mask = hide | (groi.astype(np.int16) < local_paper.astype(np.int16) - 8)
    mask = cv2.dilate(mask.astype(np.uint8), _KERNEL3, iterations=2)

    img = np.clip(roi, 0.0, 255.0).astype(np.uint8)
    if mask.any() and not mask.all():
        img = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)
    k = int(min(img.shape[:2]) // 3) | 1
    k = max(3, min(k, 61))
    return cv2.GaussianBlur(img.astype(np.float32), (k, k), 0)


def _transparency_slope(roi: np.ndarray, core: np.ndarray, wide: np.ndarray,
                        content: np.ndarray, mark: np.ndarray,
                        paper: np.ndarray) -> float:
    """How hard to pull the mark's tone apart to get the page back under it.

    A translucent mark composites as `seen = (1-a)·under + a·colour`, so undoing
    it is `under = paper + (seen - mark)/(1-a)` and the only unknown left is the
    transparency `1-a`. It is measured, not guessed, and measured as one number
    for all three channels — because that is what it physically is. Letting each
    channel find its own figure is what tints recovered letters: the estimates
    disagree by a few percent, and a few percent of disagreement between red and
    blue is a visible cast on every glyph the mark crossed.

    The measurement compares the same two things inside the mark and outside it:
    how far the mark's own tone sits above the darkest thing under it, against
    how far bare paper sits above the same page's ordinary text. The ratio of
    those two distances is exactly `1-a`.

    Where there is no text beside the mark to compare with, nothing can be
    measured, and 0.6 stands in — the usual weight of a scanner-app stamp.
    """
    strokes = cv2.erode(core.astype(np.uint8), _KERNEL3,
                        iterations=1).astype(bool)
    under = strokes & content
    # Deliberately well clear of the mark: text that the mark's fringe touches
    # is neither properly covered nor properly bare, and would bias both sides.
    away = content & ~cv2.dilate(wide.astype(np.uint8), _KERNEL3,
                                 iterations=3).astype(bool)
    trans = 0.6
    if under.sum() >= 32 and away.sum() >= 32:
        covered_ink = np.percentile(roi[under], 10, axis=0)
        bare_ink = np.percentile(roi[away], 10, axis=0)
        ratio = (mark - covered_ink) / np.maximum(paper - bare_ink, 1.0)
        trans = float(np.median(ratio))
    return float(np.clip(1.0 / float(np.clip(trans, 0.25, 0.98)), 1.0, 4.0))


def _clean_region(dst: np.ndarray, roi_u8: np.ndarray, groi: np.ndarray,
                  m: Match, box: tuple[int, int, int, int],
                  s: Settings) -> np.ndarray:
    """Erase one watermark instance completely, in place. Returns the mask used.

    The other modes work stroke by stroke: they decide, for each pixel the
    template says is part of the mark, what that pixel should become. That is
    why they leave a stain. The template never lines up perfectly — the scan is
    a fraction of a degree rotated, the scale is off by a percent, JPEG has
    smeared every edge sideways — so a rim of mark-coloured pixels always falls
    outside the mask and survives, and from across the room those rims still
    spell CamScanner.

    This mode inverts the question. It works out what the *paper* looks like
    across the whole region, then declares that anything inside the region which
    is neither that paper nor genuine dark content is residue, whether or not
    the template claimed it. So the leftover rim goes with the strokes, and the
    region ends up as uniform as the paper beside it.

    Real content is still protected: `protect_dark` keeps everything clearly
    darker than the mark, and those pixels get the unblending treatment so the
    mark's tint comes off the text underneath instead of the text being flooded.
    Turning `protect_dark` off wipes the rectangle to bare paper — which is what
    to reach for when a solid dark logo is the thing that must go.
    """
    roi = roi_u8.astype(np.float32)
    core, wide = _region_masks(m, box, max(2, int(round(min(m.w, m.h) * 0.02))),
                               whole_box=False)

    # The mark's own grey level, used as the line between "this is the mark"
    # and "this is content the mark happens to cover".
    tone = float(np.median(groi[core])) if core.any() else float(np.median(groi))
    if s.protect_dark:
        ink = groi < min(float(s.dark_cutoff), tone - 25.0)
        # A glyph is a dark core in a two-pixel grey halo, and the halo is not
        # dark enough to pass the test above. Erasing it while keeping the core
        # sharpens every letter the mark crossed into a jagged stencil, so what
        # touches real content counts as real content.
        content = cv2.dilate(ink.astype(np.uint8), _KERNEL3,
                             iterations=2).astype(bool)
    else:
        ink = np.zeros(groi.shape, bool)
        content = ink

    surface = _paper_surface(roi, groi, wide | content)
    surface_gray = surface @ np.array([0.299, 0.587, 0.114], np.float32)

    # The mark's own colour, read where it lies on bare paper.
    clear = core & ~content
    sample = clear if clear.sum() >= max(64, core.sum() // 8) else core
    mark = np.percentile(roi[sample], 35, axis=0)

    # Residue is everything in the region that is not paper and not content —
    # measured against the paper as it actually is here, not a global average.
    # 3 grey levels is below what anyone can see and above JPEG's own noise.
    dirty = np.abs(groi.astype(np.float32) - surface_gray) > 3.0
    residue = (dirty & ~content) | (wide & ~content)

    # Across a line of body text almost every pixel is within two of a glyph, so
    # the halo rule above would hand the whole line back — mark included — and
    # the mark would survive wherever it crosses text. Colour settles it: a
    # pixel wearing the mark's own colour is the mark, however close to a letter
    # it sits. Only pixels dark enough to be the letter itself stay protected.
    mark_like = (np.abs(roi - mark).max(axis=2) <= 14.0) & dirty & ~ink
    residue |= mark_like

    # Grown by a pixel to catch the smeared edge JPEG leaves around everything,
    # but never into content: that pixel belongs to a letter, not to the mark.
    residue |= (cv2.dilate(residue.astype(np.uint8), _KERNEL3,
                           iterations=1).astype(bool) & ~content)
    if not residue.any():
        return residue

    # Paper is never perfectly flat — it carries scanner grain. Laying down a
    # mathematically smooth patch reads as a sticker even when the colour is
    # spot on, so the grain of the surrounding paper is measured and put back.
    free = roi[~(residue | content)]
    grain = (min(float(np.median(free.std(axis=0))), 3.0)
             if len(free) >= 64 else 0.0)
    fill = surface
    if grain > 0.3:
        # Seeded, so the same page cleaned twice comes out byte-identical.
        noise = np.random.default_rng(1234).normal(0.0, grain, fill.shape)
        fill = fill + noise.astype(np.float32)
    dst[residue] = np.clip(fill, 0.0, 255.0)[residue].astype(np.uint8)

    # Content that the mark lay over — taken from where the mark actually turned
    # out to be, template footprint and found residue together, so a letter
    # under a bit of the mark that the crop missed still gets its tint lifted
    # instead of keeping a coloured smudge.
    # Closed, generously, before it is grown: where the mark crosses a letter,
    # the crossing itself is content and so is its two-pixel halo, which leaves
    # a gap the width of the letter between the erased pixels either side. Only
    # a closing wide enough to bridge that gap counts the crossing as covered —
    # otherwise it keeps the mark's colour for good, as a coloured speck sitting
    # on the letter, and nothing downstream will ever take it off.
    span = int(np.clip(min(m.w, m.h) // 3, 9, 25)) | 1
    zone = cv2.morphologyEx((wide | residue).astype(np.uint8), cv2.MORPH_CLOSE,
                            np.ones((span, span), np.uint8))
    covered = content & cv2.dilate(zone, _KERNEL3, iterations=2).astype(bool)
    if covered.any() and core.any():
        # Text under the wash: pin the mark's tone onto the paper here, which
        # drags the letters back to the weight they have on the rest of the line.
        #
        # The mark's own colour is read where it lies on bare paper. Sampling it
        # across the whole footprint would average in the text it covers, come
        # out darker than the mark really is, and under-correct — which is what
        # leaves recovered letters with a wash of the watermark's colour still
        # on them.
        paper = np.percentile(surface.reshape(-1, 3), 60, axis=0)
        slope = _transparency_slope(roi, core, wide, content, mark, paper)
        lifted = np.clip(surface + (roi - mark) * slope, 0.0, 255.0)
        dst[covered] = lifted[covered].astype(np.uint8)
        residue |= covered

    return residue


def _contained(inner: "fitz.Rect", outer: "fitz.Rect") -> float:
    """Fraction of `inner`'s area that lies inside `outer`."""
    clip = fitz.Rect(inner) & outer
    area = abs(inner.get_area())
    if area <= 0:
        return 0.0
    return abs(clip.get_area()) / area if not clip.is_empty else 0.0


def _rect_iou(a: "fitz.Rect", b: "fitz.Rect") -> float:
    clip = fitz.Rect(a) & b
    if clip.is_empty:
        return 0.0
    inter = abs(clip.get_area())
    union = abs(a.get_area()) + abs(b.get_area()) - inter
    return inter / union if union > 0 else 0.0


def _apply_smart(page: "fitz.Page", matches: Iterable[Match],
                 dpi: int, targets: tuple[str, ...]) -> int:
    k = 72.0 / dpi  # pixel -> PDF point
    rects = [fitz.Rect(m.x * k, m.y * k, (m.x + m.w) * k, (m.y + m.h) * k)
             for m in matches]
    if not rects:
        return 0

    # Pass 1 — drop the watermark's images and vector line art, leaving all
    # text untouched.
    if "images" in targets or "graphics" in targets:
        for r in rects:
            page.add_redact_annot(r)
        page.apply_redactions(
            images=(fitz.PDF_REDACT_IMAGE_REMOVE if "images" in targets
                    else fitz.PDF_REDACT_IMAGE_NONE),
            graphics=(fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED
                      if "graphics" in targets
                      else fitz.PDF_REDACT_LINE_ART_NONE),
            text=fitz.PDF_REDACT_TEXT_NONE,
        )

    # Pass 2 — for text watermarks, redact only the spans that genuinely *are*
    # the watermark: either their box closely coincides with a detection (IoU),
    # or they sit entirely inside one (a watermark split across several spans).
    # A body-text line that merely crosses the region overlaps only a thin slice
    # of it, so its IoU stays low and it survives untouched.
    if "text" in targets:
        marked = 0
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sr = fitz.Rect(span["bbox"])
                    if any(_rect_iou(sr, r) >= 0.45 or _contained(sr, r) >= 0.9
                           for r in rects):
                        page.add_redact_annot(sr)
                        marked += 1
        if marked:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
    return len(rects)


def page_count(path: str) -> int:
    """How many pages the input has — a picture counts as one."""
    if not is_pdf(path):
        return 1
    with fitz.open(path) as doc:
        return doc.page_count


def preview(pdf_path: str, template_path: str, s: Settings,
            page_index: int = 0) -> tuple[np.ndarray, np.ndarray, list[Match]]:
    """Return (annotated_preview, cleaned_preview, matches) for one page.

    Takes a PDF or a single picture; a picture is simply a one-page document.
    """
    tmpl = load_template(template_path)
    if is_pdf(pdf_path):
        with fitz.open(pdf_path) as doc:
            page_index = max(0, min(page_index, doc.page_count - 1))
            page_rgb = render_page(doc[page_index], s.dpi)
    else:
        page_rgb = read_image(pdf_path)

    matches = detect(page_rgb, tmpl, s)
    annotated = page_rgb.copy()
    for m in matches:
        cv2.rectangle(annotated, (m.x, m.y), (m.x + m.w, m.y + m.h),
                      (255, 0, 0), 2)
    cleaned, _ = erase(page_rgb, matches, s)
    return annotated, cleaned, matches


def process_image(image_path: str, template_path: str, out_path: str,
                  s: Settings, progress: Progress = _noop) -> dict:
    """Clean a single picture and write it out.

    The output follows the extension asked for, so a JPEG can be cleaned into a
    PNG, or into a one-page PDF, without a second trip through the converter.
    """
    tmpl = load_template(template_path)
    progress(0.2, f"Scanning {os.path.basename(image_path)}…")
    page_rgb = read_image(image_path)
    matches = detect(page_rgb, tmpl, s)
    cleaned, _ = erase(page_rgb, matches, s)
    progress(0.8, f"Removed {len(matches)} instance(s)")

    if is_pdf(out_path):
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(cleaned, cv2.COLOR_RGB2BGR),
                               [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            raise RuntimeError("Failed to encode the cleaned picture.")
        doc = fitz.open()
        h, w = cleaned.shape[:2]
        page = doc.new_page(width=w * 72.0 / s.dpi, height=h * 72.0 / s.dpi)
        page.insert_image(page.rect, stream=io.BytesIO(buf.tobytes()))
        doc.save(out_path, garbage=4, deflate=True)
        doc.close()
    else:
        write_image(out_path, cleaned)

    progress(1.0, f"Saved {out_path}")
    return {"pages": 1, "processed": 1, "hits": len(matches),
            "per_page": {1: len(matches)}}


def process(pdf_path: str, template_path: str, out_path: str, s: Settings,
            progress: Progress = _noop) -> dict:
    """Run the full job and write the cleaned file. Returns a small report."""
    if not is_pdf(pdf_path):
        return process_image(pdf_path, template_path, out_path, s, progress)

    tmpl = load_template(template_path)
    src = fitz.open(pdf_path)
    total = src.page_count
    targets = list(s.pages) if s.pages else list(range(total))
    report = {"pages": total, "processed": 0, "hits": 0, "per_page": {}}

    if s.mode == "smart":
        for i, pno in enumerate(targets):
            page = src[pno]
            page_rgb = render_page(page, s.dpi)
            matches = detect(page_rgb, tmpl, s)
            _apply_smart(page, matches, s.dpi, s.smart_targets)
            report["hits"] += len(matches)
            report["per_page"][pno + 1] = len(matches)
            report["processed"] += 1
            progress((i + 1) / len(targets),
                     f"Page {pno + 1}/{total}: removed {len(matches)}")
        src.save(out_path, garbage=4, deflate=True, clean=True)
        src.close()
        return report

    out = fitz.open()
    for i in range(total):
        page = src[i]
        rect = page.rect
        if i in targets:
            page_rgb = render_page(page, s.dpi)
            matches = detect(page_rgb, tmpl, s)
            cleaned, _ = erase(page_rgb, matches, s)
            report["hits"] += len(matches)
            report["per_page"][i + 1] = len(matches)
            report["processed"] += 1
            msg = f"Page {i + 1}/{total}: removed {len(matches)}"
        else:
            cleaned = render_page(page, s.dpi)
            msg = f"Page {i + 1}/{total}: skipped"

        ok, buf = cv2.imencode(
            ".jpg", cv2.cvtColor(cleaned, cv2.COLOR_RGB2BGR),
            [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            raise RuntimeError(f"Failed to encode page {i + 1}")

        newpage = out.new_page(width=rect.width, height=rect.height)
        newpage.insert_image(newpage.rect, stream=io.BytesIO(buf.tobytes()))
        progress((i + 1) / total, msg)

    out.save(out_path, garbage=4, deflate=True)
    out.close()
    src.close()
    return report
