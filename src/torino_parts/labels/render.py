"""Label preview rendering at the PT-P700's native 180 dpi.

The preview mirrors the live template layout: short part name, internal part ID,
bin code, and a Code 128 barcode of the part ID when the tape is wide enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DPI = 180
# Printable height in dots for each TZe width on the PT-P700 (128-pin head).
PRINTABLE_DOTS = {3.5: 24, 6: 32, 9: 50, 12: 70, 18: 112, 24: 128}
BARCODE_MIN_TAPE_MM = 12
NAME_MAX_CHARS = 30


@dataclass
class LabelContent:
    name: str
    part_id: str
    bin_code: str

    def short_name(self) -> str:
        n = " ".join(self.name.split())
        return n if len(n) <= NAME_MAX_CHARS else n[: NAME_MAX_CHARS - 1] + "…"


def _font(size: int, bold: bool = False):
    candidates = (
        ["arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["arial.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"]
    )
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def code128_modules(data: str) -> str:
    """Return the Code 128 module pattern ('1' = bar) for ``data``."""
    import barcode  # python-barcode

    return barcode.get("code128", data).build()[0]


def barcode_fits(tape_mm: float) -> bool:
    return tape_mm >= BARCODE_MIN_TAPE_MM


def render_label(content: LabelContent, tape_mm: float, out_path: Path, scale: int = 3) -> Path:
    """Render a preview PNG. The image height equals the tape width at 180 dpi."""
    if tape_mm not in PRINTABLE_DOTS:
        raise ValueError(f"unsupported tape width {tape_mm} mm")
    printable = PRINTABLE_DOTS[tape_mm]
    tape_px = round(tape_mm / 25.4 * DPI)
    margin_top = (tape_px - printable) // 2
    pad = 10

    name = content.short_name()
    with_barcode = barcode_fits(tape_mm)

    if tape_mm >= 18:
        sizes = (int(printable * 0.28), int(printable * 0.22), int(printable * 0.25))
    elif tape_mm >= 12:
        sizes = (int(printable * 0.32), int(printable * 0.26), int(printable * 0.28))
    else:
        sizes = (int(printable * 0.8),) * 3
    f_name, f_id, f_bin = _font(sizes[0], True), _font(sizes[1]), _font(sizes[2], True)

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))

    def tw(text, font):
        b = probe.textbbox((0, 0), text, font=font)
        return b[2] - b[0]

    if tape_mm >= 12:
        id_line = "scan to open bin" if content.part_id.startswith("BIN:") else content.part_id
        lines = [(name, f_name), (id_line, f_id), (f"BIN {content.bin_code}", f_bin)]
        text_w = max(tw(t, f) for t, f in lines)
    else:
        single = f"{name} | {content.part_id} | {content.bin_code}"
        lines = [(single, f_name)]
        text_w = tw(single, f_name)

    modules = code128_modules(content.part_id) if with_barcode else ""
    module_w = 2
    quiet = 10 * module_w
    bc_w = len(modules) * module_w + 2 * quiet if modules else 0

    width = pad + text_w + (pad * 2 + bc_w if modules else 0) + pad
    img = Image.new("RGB", (width, tape_px), "white")
    d = ImageDraw.Draw(img)

    # Text block, vertically distributed within the printable area.
    if len(lines) == 3:
        heights = [s for s in sizes]
        gap = max(1, (printable - sum(heights)) // 4)
        y = margin_top + gap
        for (text, font), h in zip(lines, heights):
            d.text((pad, y), text, font=font, fill="black")
            y += h + gap
    else:
        text, font = lines[0]
        d.text((pad, margin_top + (printable - sizes[0]) // 2), text, font=font, fill="black")

    if modules:
        x0 = pad + text_w + pad * 2 + quiet
        top = margin_top + 4
        bottom = margin_top + printable - 4
        for i, m in enumerate(modules):
            if m == "1":
                d.rectangle([x0 + i * module_w, top, x0 + (i + 1) * module_w - 1, bottom], fill="black")

    # Show the unprintable tape edges in light grey so the preview reads as tape.
    if margin_top > 0:
        d.rectangle([0, 0, width - 1, margin_top - 1], fill=(235, 235, 235))
    if margin_top + printable < tape_px:
        d.rectangle([0, margin_top + printable, width - 1, tape_px - 1], fill=(235, 235, 235))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    img.save(out_path, dpi=(DPI * scale, DPI * scale))
    return out_path
