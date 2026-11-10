#!/usr/bin/env python3
"""
photo_collage.py - build a multi-page PDF photo collage from all images found
(recursively) in the current working folder.

Output: <current folder name>_collage_YYYY-MM-DD-HHMMSS.pdf in the current folder.

Requirements:
    pip install Pillow                 # required
    pip install pillow-heif            # optional, for .heic / .heif
    pip install rawpy                  # optional, for camera RAW files

Layout: images are placed either in rows (--rows, the default) where all
images in a row share the same height, or in columns (--columns) where all
images in a column share the same width. Each row/column is stretched to fill
the printable width/height of the paper. N in --rows N / --columns N is a
target: each page may use N-K..N+K rows/columns (K = --tolerance, default 1).
How many images go into each row/column, and how many rows/columns go on
each page, is chosen automatically to waste as little page area as possible
(which also avoids an almost empty last page).
--max-crop PCT (default 0 = off) allows trimming up to PCT % off the photos
so that the rows/columns fill each page completely.
The very last row/column is left unstretched if stretching would make it
noticeably (more than 15 %) thicker than the ones before it.
--spacing adds space between neighbouring images within a row (horizontal)
or, in columns mode, within a column (vertical). It is also the minimum space
between neighbouring rows (columns).
On each page the first row sits just below the top margin, the last row just
above the bottom margin, and the rows in between are evenly spaced, at least
--spacing apart (columns: first at the left margin, last at the right
margin).

The PDF is written by a small built-in writer: page background and image
borders are vector shapes, and each photo is embedded as a JPEG downscaled so
that it never exceeds --image-dpi at its printed size.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Error: Pillow is required. Install it with:  pip install Pillow")

# Optional HEIF/HEIC support
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except ImportError:
    HEIF_SUPPORT = False

# Optional camera RAW support
try:
    import rawpy

    RAW_SUPPORT = True
except ImportError:
    RAW_SUPPORT = False

# Allow very large photos (panoramas etc.) without decompression-bomb errors
Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

MM_TO_PT = 72.0 / 25.4
MM_PER_INCH = 25.4

STANDARD_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
HEIF_EXTS = {".heic", ".heif"}
RAW_EXTS = {
    ".raw", ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf",
    ".sr2", ".orf", ".rw2", ".raf", ".pef", ".srw", ".x3f", ".3fr", ".erf",
    ".kdc", ".mrw", ".rwl", ".iiq", ".mef", ".mos",
}
ALL_EXTS = STANDARD_EXTS | HEIF_EXTS | RAW_EXTS

# ISO 216 A-series, portrait (width x height) in mm
ISO_A_SIZES = {
    "A0": (841, 1189),
    "A1": (594, 841),
    "A2": (420, 594),
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "A6": (105, 148),
    "A7": (74, 105),
    "A8": (52, 74),
    "A9": (37, 52),
    "A10": (26, 37),
}

JPEG_QUALITY = 90

# --------------------------------------------------------------------------
# Argument parsing / validation
# --------------------------------------------------------------------------


def hex_color(value: str) -> tuple[int, int, int]:
    """Parse '#RRGGBB', 'RRGGBB', '#RGB' or 'RGB' into an (r, g, b) tuple."""
    v = value.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", v):
        v = "".join(c * 2 for c in v)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", v):
        raise argparse.ArgumentTypeError(
            f"invalid hex color '{value}' (expected e.g. #FFFFFF or FFFFFF)"
        )
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 (got {n})")
    return n


def mm_range(low: float, high: float, allow_low: bool = True):
    """Factory for a float argument type limited to [low, high] (or (low, high])."""

    def parse(value: str) -> float:
        try:
            n = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{value}' is not a number")
        if not math.isfinite(n):
            raise argparse.ArgumentTypeError(f"'{value}' is not a finite number")
        too_low = n < low if allow_low else n <= low
        if too_low or n > high:
            bracket = "[" if allow_low else "("
            raise argparse.ArgumentTypeError(
                f"must be in range {bracket}{low:g}, {high:g}] mm (got {value})"
            )
        return n

    return parse


def non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more (got {n})")
    return n


def percent_range(low: float, high: float):
    """Factory for a float argument type limited to [low, high] percent."""

    def parse(value: str) -> float:
        try:
            n = float(value.rstrip("%"))
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{value}' is not a number")
        if not math.isfinite(n) or n < low or n > high:
            raise argparse.ArgumentTypeError(
                f"must be in range [{low:g}, {high:g}] percent (got {value})"
            )
        return n

    return parse


def paper_size_name(value: str) -> str:
    name = value.strip().upper()
    if name not in ISO_A_SIZES:
        raise argparse.ArgumentTypeError(
            f"invalid paper size '{value}' (valid: {', '.join(ISO_A_SIZES)})"
        )
    return name


def positive_float(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number")
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0 (got {value})")
    return n


def non_negative_float(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number")
    if not math.isfinite(n) or n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more (got {value})")
    return n


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Recursively find all images in the current folder and arrange them "
            "in a multi-page PDF photo collage."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    placement = p.add_mutually_exclusive_group()
    placement.add_argument(
        "--rows", type=positive_int, nargs="?", const=3, default=argparse.SUPPRESS,
        metavar="N",
        help="arrange images in rows, about N rows per page (N defaults to 3, "
             "see --tolerance). All "
             "images in a row get the same height and each row is stretched to "
             "the full paper width. This is the default placement method")
    placement.add_argument(
        "--columns", type=positive_int, nargs="?", const=3,
        default=argparse.SUPPRESS, metavar="N",
        help="arrange images in columns, about N columns per page (N defaults "
             "to 3, see --tolerance). "
             "All images in a column get the same width and each column is "
             "stretched to the full paper height")
    p.add_argument("--tolerance", type=non_negative_int, default=1, metavar="K",
                   help="each page may use between N-K and N+K rows/columns, "
                        "whichever fills the pages best (0 = exactly N)")
    p.add_argument("--max-crop", type=percent_range(0, 50), default=0.0,
                   metavar="PCT",
                   help="allow cropping up to PCT percent (0-50) of each photo "
                        "so the rows/columns fill the pages completely. "
                        "0 = never crop")
    p.add_argument("--evenness", type=non_negative_float, default=EVENNESS_WEIGHT,
                   metavar="W",
                   help="how strongly rows/columns are kept at similar "
                        "thickness versus filling the pages (0 or more). "
                        "Higher = more even rows/columns but more empty space; "
                        "0 = only maximise filled area")
    p.add_argument("--paper-size", type=paper_size_name, default="A4",
                   help=f"ISO 216 A-series paper size ({', '.join(ISO_A_SIZES)})")
    p.add_argument("--paper-size-x", type=mm_range(0, 1000, allow_low=False),
                   default=None, metavar="MM",
                   help="custom paper width in mm (0-1000), overrides --paper-size")
    p.add_argument("--paper-size-y", type=mm_range(0, 1000, allow_low=False),
                   default=None, metavar="MM",
                   help="custom paper height in mm (0-1000), overrides --paper-size")
    p.add_argument("--paper-orientation", choices=["portrait", "landscape"],
                   type=str.lower, default=None,
                   help="paper orientation (default: portrait). With a custom "
                        "size it is only applied if given explicitly")
    p.add_argument("--paper-margin", type=mm_range(0, 1000), default=10.0,
                   metavar="MM", help="paper margin in mm (0-1000)")
    p.add_argument("--image-border", type=mm_range(0, 1000), default=0.0,
                   metavar="MM", help="border around each image in mm (0-1000)")
    p.add_argument("--spacing", type=mm_range(0, 1000), default=1.0,
                   metavar="MM",
                   help="space in mm (0-1000) between neighbouring images in "
                        "the same row (horizontal) or, with --columns, in the "
                        "same column (vertical). Also the minimum space "
                        "between neighbouring rows (columns)")
    p.add_argument("--image-dpi", type=positive_float, default=300.0,
                   metavar="DPI", help="maximum resolution of images in the PDF")
    p.add_argument("--background-color", type=hex_color, default="#FFFFFF",
                   metavar="HEX", help="paper background color as hex code")
    p.add_argument("--border-color", type=hex_color, default="#000000",
                   metavar="HEX", help="image border color as hex code")
    # Note: argparse also runs `type` on string defaults, so the color
    # defaults above end up as (r, g, b) tuples as well.
    args = p.parse_args(argv)

    # ---- Resolve the final paper size (mm) --------------------------------
    std_w, std_h = ISO_A_SIZES[args.paper_size]
    custom = args.paper_size_x is not None or args.paper_size_y is not None
    width = args.paper_size_x if args.paper_size_x is not None else std_w
    height = args.paper_size_y if args.paper_size_y is not None else std_h

    # With custom dimensions, honour them as given unless the user
    # explicitly asked for an orientation.
    orientation = args.paper_orientation
    if orientation is None and not custom:
        orientation = "portrait"
    if orientation == "portrait" and width > height:
        width, height = height, width
    elif orientation == "landscape" and height > width:
        width, height = height, width

    args.page_w_mm = float(width)
    args.page_h_mm = float(height)
    args.size_label = (
        f"custom {width:g} x {height:g} mm" if custom
        else f"{args.paper_size} {orientation} ({width:g} x {height:g} mm)"
    )

    # ---- Placement method -------------------------------------------------
    if hasattr(args, "columns"):
        args.layout, args.lines = "columns", args.columns
    else:
        args.layout, args.lines = "rows", getattr(args, "rows", 3)

    # ---- Check that the layout actually fits ------------------------------
    m, b, n, s = args.paper_margin, args.image_border, args.lines, args.spacing
    content_w = args.page_w_mm - 2 * m
    content_h = args.page_h_mm - 2 * m
    if content_w <= 0 or content_h <= 0:
        p.error(
            f"--paper-margin {m:g} mm leaves no printable area on a "
            f"{args.page_w_mm:g} x {args.page_h_mm:g} mm page"
        )
    # main = direction a row/column runs, cross = direction they are stacked
    if args.layout == "rows":
        main_len, cross_len = content_w, content_h
    else:
        main_len, cross_len = content_h, content_w
    if main_len - 2 * b <= 0:
        p.error(f"--image-border {b:g} mm is too thick for the "
                f"{content_w:g} x {content_h:g} mm printable area")
    if cross_len - n * 2 * b - (n - 1) * s <= 0:
        p.error(
            f"{n} {args.layout} with {b:g} mm image border and {s:g} mm "
            f"spacing do not fit in the {content_w:g} x {content_h:g} mm "
            f"printable area"
        )
    return args


# --------------------------------------------------------------------------
# Image discovery
# --------------------------------------------------------------------------


def natural_key(path: Path):
    """Sort key so that 'img2' comes before 'img10'."""
    return [
        int(tok) if tok.isdigit() else tok.lower()
        for tok in re.split(r"(\d+)", str(path))
    ]


def find_images(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            if Path(name).suffix.lower() in ALL_EXTS:
                found.append(Path(dirpath, name).relative_to(root))
    found.sort(key=natural_key)
    return found


# --------------------------------------------------------------------------
# Image loading
# --------------------------------------------------------------------------


class SkipImage(Exception):
    """Raised when an image cannot be used; message is shown to the user."""


def flatten_to_rgb(img: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    """Convert any mode to RGB, compositing transparency onto `bg`."""
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "RGBa"):
        img = img.convert("RGBA")
        base = Image.new("RGB", img.size, bg)
        base.paste(img, mask=img.getchannel("A"))
        return base
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16/32-bit greyscale: scale down to 8 bit
        img = img.point(lambda v: v / 256).convert("L")
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


class SourceImage:
    """Lazily opened image with known (orientation-corrected) dimensions."""

    def __init__(self, path: Path):
        self.path = path
        self.is_raw = path.suffix.lower() in RAW_EXTS
        ext = path.suffix.lower()
        if ext in HEIF_EXTS and not HEIF_SUPPORT:
            raise SkipImage("HEIF/HEIC support missing (pip install pillow-heif)")
        if self.is_raw:
            if not RAW_SUPPORT:
                raise SkipImage("RAW support missing (pip install rawpy)")
            try:
                with rawpy.imread(str(path)) as raw:
                    s = raw.sizes
                    w, h = s.width, s.height
                    if s.flip in (5, 6):  # rotated 90 degrees
                        w, h = h, w
            except Exception as e:  # noqa: BLE001
                raise SkipImage(f"cannot read RAW file ({e})")
            self.size = (w, h)
        else:
            try:
                with Image.open(path) as img:
                    w, h = img.size
                    orientation = img.getexif().get(0x0112, 1)
            except Exception as e:  # noqa: BLE001
                raise SkipImage(f"cannot open image ({e})")
            self.orientation = orientation
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            self.size = (w, h)
        if self.size[0] <= 0 or self.size[1] <= 0:
            raise SkipImage("image has zero size")

    def load(self, max_px: tuple[int, int], bg) -> Image.Image:
        """Return an RGB image, correctly rotated, no larger than max_px."""
        if self.is_raw:
            img = self._load_raw(max_px)
        else:
            img = self._load_standard(max_px)
        img = flatten_to_rgb(img, bg)
        if img.width > max_px[0] or img.height > max_px[1]:
            img.thumbnail(max_px, Image.LANCZOS)
        return img

    def _load_standard(self, max_px) -> Image.Image:
        img = Image.open(self.path)
        # Let the JPEG decoder skip unneeded resolution (big speed-up)
        if img.format == "JPEG":
            dw, dh = max_px
            if self.orientation in (5, 6, 7, 8):
                dw, dh = dh, dw
            try:
                img.draft("RGB", (dw, dh))
            except Exception:  # noqa: BLE001
                pass
        img.load()
        return ImageOps.exif_transpose(img)

    def _load_raw(self, max_px) -> Image.Image:
        with rawpy.imread(str(self.path)) as raw:
            # Half-size demosaicing is much faster; use it when it is enough.
            half_ok = (self.size[0] // 2 >= max_px[0]) or (
                self.size[1] // 2 >= max_px[1]
            )
            rgb = raw.postprocess(use_camera_wb=True, half_size=half_ok,
                                  output_bps=8)
        return Image.fromarray(rgb)


# --------------------------------------------------------------------------
# Minimal streaming PDF writer
# --------------------------------------------------------------------------


class PdfWriter:
    """Writes a PDF incrementally: vector rectangles + embedded JPEG images."""

    def __init__(self, path: Path):
        self.f = open(path, "wb")
        self.offsets: dict[int, int] = {}
        self.next_id = 3  # 1 = catalog, 2 = page tree (written at the end)
        self.page_ids: list[int] = []
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def _new_id(self) -> int:
        oid = self.next_id
        self.next_id += 1
        return oid

    def _write_obj(self, oid: int, body: bytes, stream: bytes | None = None):
        self.offsets[oid] = self.f.tell()
        self.f.write(f"{oid} 0 obj\n".encode())
        self.f.write(body)
        if stream is not None:
            self.f.write(b"\nstream\n")
            self.f.write(stream)
            self.f.write(b"\nendstream")
        self.f.write(b"\nendobj\n")

    def add_page(self, width_pt: float, height_pt: float,
                 content: bytes, images: dict[str, Image.Image]):
        xobj_refs = []
        for name, img in images.items():
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
            data = buf.getvalue()
            oid = self._new_id()
            self._write_obj(
                oid,
                (f"<< /Type /XObject /Subtype /Image /Width {img.width} "
                 f"/Height {img.height} /ColorSpace /DeviceRGB "
                 f"/BitsPerComponent 8 /Filter /DCTDecode "
                 f"/Length {len(data)} >>").encode(),
                data,
            )
            xobj_refs.append(f"/{name} {oid} 0 R")

        content_id = self._new_id()
        self._write_obj(content_id,
                        f"<< /Length {len(content)} >>".encode(), content)

        page_id = self._new_id()
        self._write_obj(
            page_id,
            (f"<< /Type /Page /Parent 2 0 R "
             f"/MediaBox [0 0 {width_pt:.3f} {height_pt:.3f}] "
             f"/Resources << /XObject << {' '.join(xobj_refs)} >> >> "
             f"/Contents {content_id} 0 R >>").encode(),
        )
        self.page_ids.append(page_id)

    def close(self):
        kids = " ".join(f"{pid} 0 R" for pid in self.page_ids)
        self._write_obj(
            2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.page_ids)} >>".encode()
        )
        self._write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        info_id = self._new_id()
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        self._write_obj(
            info_id,
            f"<< /Producer (photo_collage.py) /CreationDate (D:{now}) >>".encode(),
        )

        xref_pos = self.f.tell()
        size = self.next_id
        self.f.write(f"xref\n0 {size}\n".encode())
        self.f.write(b"0000000000 65535 f \n")
        for oid in range(1, size):
            self.f.write(f"{self.offsets[oid]:010d} 00000 n \n".encode())
        self.f.write(
            (f"trailer\n<< /Size {size} /Root 1 0 R /Info {info_id} 0 R >>\n"
             f"startxref\n{xref_pos}\n%%EOF\n").encode()
        )
        self.f.close()

    def abort(self):
        self.f.close()


def rgb_op(color, op: str) -> str:
    r, g, b = (c / 255 for c in color)
    return f"{r:.4f} {g:.4f} {b:.4f} {op}"


# --------------------------------------------------------------------------
# Collage building
# --------------------------------------------------------------------------


@dataclass
class Placement:
    """Where one image goes: inner image box in mm, measured from the
    top-left corner of the page (the border is drawn outside this box).
    `crop` is the visible part of the photo as fractions of its width and
    height: (left, top, right, bottom); (0, 0, 1, 1) means not cropped."""
    src: SourceImage
    x: float
    y: float
    w: float
    h: float
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)


# Upper limit on images per row/column considered by the line breaker
MAX_PER_LINE = 200

# The very last row/column is only stretched to fill the page if that makes
# it at most this much thicker than the rows/columns before it; otherwise it
# keeps their thickness and starts at the left (rows) / top (columns).
LAST_LINE_MAX_GROWTH = 1.15

# ---- Cost model of the line breaker (unit: one page) ----------------------
# Every page costs 1 and every row/column gives back the share of the page it
# covers, so the total cost is "pages used minus area covered", i.e. the
# wasted space. Minimising it fills pages and avoids an almost empty last
# page. The other terms keep the result looking good:
EVENNESS_WEIGHT = 0.15          # default for --evenness (similar thickness)
COUNT_DEVIATION_PENALTY = 0.02  # prefer exactly N rows/columns when equal
OVERFLOW_PENALTY = 10.0         # per mm a page would overflow (last resort)
BEAM_WIDTH = 20                 # max. partial solutions kept per state group


@dataclass
class _BreakState:
    """One partial solution of the line breaker (a linked list of lines)."""
    cost: float
    used: float      # stretched thickness used so far on the current page
    lines: int       # number of lines on the current page (0 = none yet)
    start: int       # first image index of the line that ended here
    full: bool       # True = stretched line, False = unstretched last line
    fixed: float     # thickness of an unstretched last line, else 0
    area: float      # photo area (mm^2) of the stretched lines on the page
    parent: "_BreakState | None"


def cap_to_total(values: list[float], total: float) -> list[float]:
    """Return values with the largest ones lowered to a common cap so that
    their sum does not exceed `total` (smaller values stay untouched)."""
    if sum(values) <= total:
        return list(values)
    ordered = sorted(values)
    used = 0.0
    cap = ordered[-1]
    for k, v in enumerate(ordered):
        cap = (total - used) / (len(ordered) - k)
        if cap <= v:
            break
        used += v
    return [min(v, cap) for v in values]


def compute_layout(args, sources: list[SourceImage]) -> list[list[Placement]]:
    """Split the images into rows (or columns) and the rows into pages.

    Terminology (works for both modes):
      line      = one row (rows mode) or one column (columns mode)
      main axis = the direction a line runs (x for rows, y for columns)
      cross     = the direction lines are stacked
      thickness = the shared image height (rows) / width (columns) of a line

    Every line is stretched so it exactly fills the main axis (neighbouring
    images within a line are separated by --spacing). A page holds between
    N - tolerance and N + tolerance lines. Which images go on which line and
    which lines go on which page is chosen by dynamic programming that
    minimises the wasted page area (see the cost model constants above),
    preserving the image order.

    With --max-crop, all lines of a page are scaled by a common factor to
    fill the cross axis exactly: thicker lines are cropped along the main
    axis (sides of the photos), thinner ones along the cross axis.

    Along the cross axis the first line touches the start margin (top /
    left), the last line touches the end margin (bottom / right), and
    neighbouring lines are at least --spacing apart; any leftover space is
    shared equally between the lines.
    """
    rows_mode = args.layout == "rows"
    m, b, n = args.paper_margin, args.image_border, args.lines
    s = args.spacing  # between images in a line, and minimum between lines
    content_w = args.page_w_mm - 2 * m
    content_h = args.page_h_mm - 2 * m
    main_len, cross_len = (content_w, content_h) if rows_mode else (content_h, content_w)

    # Allowed scale range of a line's thickness through cropping
    crop = args.max_crop / 100.0
    lo, hi = 1.0 - crop, 1.0 / (1.0 - crop)

    def free_for(lines: int) -> float:
        """Cross-axis room for line thickness on a page with `lines` lines
        (after borders and the minimum spacing between the lines)."""
        return cross_len - lines * 2 * b - (lines - 1) * s

    # Allowed number of lines per page (borders must still leave room)
    l_min = max(1, n - args.tolerance)
    l_max = n + args.tolerance
    while l_max > n and free_for(l_max) <= 0:
        l_max -= 1

    # Length along the main axis per unit of thickness
    ratios = [(w / h if rows_mode else h / w) for w, h in (src.size for src in sources)]
    prefix = [0.0]
    for r in ratios:
        prefix.append(prefix[-1] + r)

    # Ideal thickness: N lines (including their borders) fill the cross axis
    target = free_for(n) / n

    def stretched_thickness(i: int, j: int) -> float | None:
        """Thickness of a line holding images i..j-1 stretched to main_len."""
        k = j - i
        free = main_len - k * 2 * b - (k - 1) * s
        return free / (prefix[j] - prefix[i]) if free > 0 else None

    page_area = main_len * cross_len

    def photo_len(i: int, j: int) -> float:
        """Main-axis length of a stretched line that is actual photo."""
        k = j - i
        return main_len - k * 2 * b - (k - 1) * s

    def line_cost(t: float) -> float:
        dev = math.log(t / target)
        # Thick lines eat the space of the other lines: penalise more.
        return args.evenness * dev * dev * (2.0 if dev > 0 else 1.0)

    def page_scale(lines: int, used: float, fixed: float, partial: bool) -> float:
        """Common thickness factor for the stretched lines of a page: fills
        the page as far as cropping allows (a partly filled last page is
        only shrunk if needed, never enlarged)."""
        if used <= 0:
            return 1.0
        f = (free_for(lines) - fixed) / used
        return max(lo, min(1.0 if partial else hi, f))

    def page_close(lines: int, used: float, area: float, fixed: float,
                   partial: bool) -> float:
        """Cost of finishing a page. The lines' own photo area was already
        credited when they were added; this adds the page itself, the extra
        (or lost) photo area from cropping, overflow and count deviation."""
        free = free_for(lines)
        f = page_scale(lines, used, fixed, partial)
        total = f * used + fixed
        # Effective scale actually realised (the safety net caps overflow)
        g = (min(total, free) - fixed) / used if used > 0 else 1.0
        cost = 1.0 - (g - 1.0) * area / page_area
        cost += OVERFLOW_PENALTY * max(0.0, total - free)
        if not partial:
            cost += COUNT_DEVIATION_PENALTY * abs(lines - n)
        return cost

    # ---- Page-aware optimal line breaking ---------------------------------
    # Dynamic programming over the images in order. A state remembers how
    # many lines the current page has and how much room they use. For each
    # (image index, lines on page) only non-dominated states are kept
    # (less room used and lower cost is better), limited to BEAM_WIDTH.
    count = len(sources)
    slope = (hi - 1.0) * main_len / page_area  # max. crop gain per mm used
    root = _BreakState(0.0, 0.0, 0, 0, True, 0.0, 0.0, None)
    fronts: list[list[_BreakState]] = [[root]] + [[] for _ in range(count)]
    best_final: _BreakState | None = None

    for j in range(1, count + 1):
        is_last = j == count
        cand: dict[tuple[int, int], _BreakState] = {}
        for k in range(1, min(j, MAX_PER_LINE) + 1):
            i = j - k
            t = stretched_thickness(i, j)
            if t is None:
                break  # adding more images only leaves less room
            for st in fronts[i]:
                # Continue the current page, or close it and start a new one
                starts = []
                if 0 < st.lines < l_max:
                    starts.append((st.cost, st.used, st.area, st.lines + 1))
                if st.lines == 0:
                    starts.append((st.cost, 0.0, 0.0, 1))
                elif st.lines >= l_min:
                    starts.append((st.cost + page_close(st.lines, st.used, st.area,
                                                        0.0, False), 0.0, 0.0, 1))
                line_area = t * photo_len(i, j)
                for base, used0, area0, nl in starts:
                    if not is_last:
                        used = used0 + t
                        if nl > 1 and used * lo > free_for(nl):
                            continue  # can never fit on this page
                        c = base + line_cost(t) - line_area / page_area
                        key = (nl, round(used))
                        if key not in cand or c < cand[key].cost:
                            cand[key] = _BreakState(c, used, nl, i, True, 0.0,
                                                    area0 + line_area, st)
                        continue

                    # Last line of all: complete the solution right away
                    partial = nl < l_min
                    options = []
                    if t <= LAST_LINE_MAX_GROWTH * target:
                        options.append((True, used0 + t, area0 + line_area, 0.0,
                                        base + line_cost(t) - line_area / page_area))
                    if t > target:
                        # Unstretched: as thick as the other lines on the page
                        ref = used0 / (nl - 1) if nl > 1 else target
                        tt = min(ref, t)
                        room = free_for(nl) - used0 * lo
                        # It must keep (almost) that thickness, not be
                        # squeezed into whatever room is left on the page.
                        if room >= tt * lo:
                            tt = min(tt, room)
                            covered = tt * tt * (prefix[j] - prefix[i])
                            options.append((False, used0, area0, tt,
                                            base - covered / page_area))
                    for full, used, area, fixed, c in options:
                        c += page_close(nl, used, area, fixed, partial)
                        if best_final is None or c < best_final.cost:
                            best_final = _BreakState(c, used, nl, i, full, fixed,
                                                     area, st)
            if t < target / 4:
                break  # lines only get thinner from here on

        if is_last:
            break
        # Keep only non-dominated states per number of lines on the page.
        # The cost is adjusted for what cropping could still gain with the
        # extra room, so that pruning never discards a better solution.
        by_lines: dict[int, list[_BreakState]] = {}
        for st in cand.values():
            by_lines.setdefault(st.lines, []).append(st)
        for group in by_lines.values():
            group.sort(key=lambda st: (st.used, st.cost))
            kept = []
            best_adj = math.inf
            for st in group:
                adj = st.cost - slope * st.used
                if adj < best_adj:
                    kept.append(st)
                    best_adj = adj
            if len(kept) > BEAM_WIDTH:
                kept = sorted(kept, key=lambda st: st.cost)[:BEAM_WIDTH]
            fronts[j].extend(kept)

    # ---- Reconstruct pages of lines ---------------------------------------
    chain = []
    st = best_final
    end = count
    while st is not None and st.parent is not None:
        chain.append((st.start, end, st.full, st.fixed, st.lines == 1))
        end = st.start
        st = st.parent
    chain.reverse()
    page_lines: list[list[tuple[int, int, bool, float]]] = []
    for i, j, full, fixed, new_page in chain:
        if new_page or not page_lines:
            page_lines.append([])
        page_lines[-1].append((i, j, full, fixed))

    # ---- Position everything ------------------------------------------------
    pages: list[list[Placement]] = []
    prev_extra = 0.0  # extra gap (beyond --spacing) on the previous page
    for pno, plines in enumerate(page_lines):
        nl = len(plines)
        partial = pno == len(page_lines) - 1 and nl < l_min
        free = free_for(nl)
        natural = [stretched_thickness(i, j) for i, j, _, _ in plines]
        fixed = sum(fx for _, _, full, fx in plines if not full)
        used = sum(t for t, (_, _, full, _) in zip(natural, plines) if full)
        f = page_scale(nl, used, fixed, partial)
        thick = [f * t if full else fx for t, (_, _, full, fx) in zip(natural, plines)]

        # Safety net only: normally every page fits. If one cannot, the
        # thickest lines are shrunk further and centred.
        thick = cap_to_total(thick, free)

        # Lines are --spacing apart; leftover cross-axis space is shared
        # equally between them on top of that, so the first line touches the
        # top/left margin and the last line the bottom/right margin. A partly
        # filled last page instead reuses the previous page's gap and starts
        # at the top/left.
        leftover = max(0.0, free - sum(thick))
        if nl < 2:
            extra = 0.0
        elif not partial:
            extra = leftover / (nl - 1)
        else:
            extra = min(prev_extra, leftover / (nl - 1))
        gap = s + extra

        v = 0.0
        placements: list[Placement] = []
        for (i, j, full, _), t_nat, t in zip(plines, natural, thick):
            k = j - i
            if full:
                g = t / t_nat  # thickness scale of this line
                crop_main = 1.0 - 1.0 / g if g > 1.0 else 0.0
                crop_cross = 1.0 - max(g, lo) if g < 1.0 else 0.0
            else:
                crop_main = crop_cross = 0.0
            # Visible image length along the main axis per unit of ratio
            per_ratio = t / (1.0 - crop_cross) * (1.0 - crop_main)
            line_len = per_ratio * (prefix[j] - prefix[i]) + k * 2 * b + (k - 1) * s
            # Stretched lines fill the main axis (offset 0 unless the safety
            # net shrank them); an unstretched last line starts at the edge.
            u = (main_len - line_len) / 2 if full else 0.0
            cx, cy = (crop_main, crop_cross) if rows_mode else (crop_cross, crop_main)
            box = (cx / 2, cy / 2, 1.0 - cx / 2, 1.0 - cy / 2)
            for idx in range(i, j):
                u_len = per_ratio * ratios[idx]
                iu, iv = u + b, v + b  # inner image start
                if rows_mode:
                    pl = Placement(sources[idx], m + iu, m + iv, u_len, t, box)
                else:
                    pl = Placement(sources[idx], m + iv, m + iu, t, u_len, box)
                placements.append(pl)
                u += u_len + 2 * b + s
            v += t + 2 * b + gap
        pages.append(placements)
        prev_extra = extra
    return pages


def build_collage(args, sources: list[SourceImage], out_path: Path) -> int:
    page_w = args.page_w_mm * MM_TO_PT
    page_h = args.page_h_mm * MM_TO_PT
    border = args.image_border * MM_TO_PT

    pages = compute_layout(args, sources)
    done = 0

    pdf = PdfWriter(out_path)
    try:
        for placements in pages:
            ops = [
                rgb_op(args.background_color, "rg"),
                f"0 0 {page_w:.3f} {page_h:.3f} re f",
            ]
            images: dict[str, Image.Image] = {}

            for i, pl in enumerate(placements):
                src = pl.src
                done += 1
                print(f"  [{done}/{len(sources)}] {src.path}")

                # Convert mm (top-left origin) to PDF points (bottom-left)
                draw_w = pl.w * MM_TO_PT
                draw_h = pl.h * MM_TO_PT
                x = pl.x * MM_TO_PT
                y = page_h - pl.y * MM_TO_PT - draw_h

                # Maximum pixel size allowed by --image-dpi (for the visible
                # part; the whole photo is loaded at matching resolution)
                max_px = (
                    max(1, math.ceil(draw_w / 72 * args.image_dpi)),
                    max(1, math.ceil(draw_h / 72 * args.image_dpi)),
                )
                cl, ct, cr, cb = pl.crop
                full_px = (math.ceil(max_px[0] / (cr - cl)),
                           math.ceil(max_px[1] / (cb - ct)))
                try:
                    img = src.load(full_px, args.background_color)
                    if pl.crop != (0.0, 0.0, 1.0, 1.0):
                        cropped = img.crop((round(cl * img.width), round(ct * img.height),
                                            round(cr * img.width), round(cb * img.height)))
                        img.close()
                        img = cropped
                    if img.width > max_px[0] or img.height > max_px[1]:
                        img.thumbnail(max_px, Image.LANCZOS)
                except Exception as e:  # noqa: BLE001
                    print(f"    warning: could not render {src.path}: {e}",
                          file=sys.stderr)
                    continue

                if border > 0:
                    ops.append(rgb_op(args.border_color, "rg"))
                    ops.append(
                        f"{x - border:.3f} {y - border:.3f} "
                        f"{draw_w + 2 * border:.3f} {draw_h + 2 * border:.3f} re f"
                    )
                name = f"Im{i}"
                images[name] = img
                ops.append(
                    f"q {draw_w:.3f} 0 0 {draw_h:.3f} {x:.3f} {y:.3f} cm "
                    f"/{name} Do Q"
                )

            pdf.add_page(page_w, page_h, "\n".join(ops).encode(), images)
            for img in images.values():
                img.close()
        pdf.close()
    except BaseException:
        pdf.abort()
        out_path.unlink(missing_ok=True)
        raise
    return len(pages)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    args = parse_args(argv)
    cwd = Path.cwd()

    print(f"Searching for images in {cwd} ...")
    paths = find_images(cwd)
    if not paths:
        print("No images found (looked for: "
              f"{', '.join(sorted(e.lstrip('.') for e in ALL_EXTS))}).")
        return 1

    sources: list[SourceImage] = []
    for p in paths:
        try:
            sources.append(SourceImage(p))
        except SkipImage as e:
            print(f"  skipping {p}: {e}", file=sys.stderr)
    if not sources:
        print("None of the found images could be read.")
        return 1

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    folder_name = cwd.name or "root"
    out_path = cwd / f"{folder_name}_collage_{timestamp}.pdf"

    print(
        f"Found {len(sources)} image(s). Layout: {args.lines} "
        f"(+/-{args.tolerance}) {args.layout} per page, max crop "
        f"{args.max_crop:g} %, evenness {args.evenness:g}, {args.size_label}, margin {args.paper_margin:g} mm, "
        f"border {args.image_border:g} mm, spacing {args.spacing:g} mm, "
        f"max {args.image_dpi:g} DPI"
    )
    try:
        n_pages = build_collage(args, sources, out_path)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Done: {out_path.name}  ({n_pages} page(s), {size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
