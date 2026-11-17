"""Generate a b-PAC ``.lbx`` label template for the Brother PT-P700 without P-touch Editor.

An ``.lbx`` is a ZIP holding ``label.xml`` (layout) and ``prop.xml`` (metadata). The template has
the objects Torino fills at print time:

* ``PartName`` - fixed-width frame; long names shrink to fit instead of running into the barcode
* ``PartID``   - internal ID (or "scan to open bin" on bin labels)
* ``BinCode``  - bin code, after a static "BIN" caption
* ``Barcode``  - Code 128 of the part ID, 2 printer dots per module (12 mm tape and wider)

Layout mirrors the on-screen preview (``render.py``): three text lines, then the barcode.
Tape geometry and media IDs come from the PT-P700 driver's media table (BSPP70AD.PTD):
``tape_mm: (media_id, tape_dots, printable_dots)`` at 180 dpi.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .printers import bpac_dispatch, com_call0
from .render import BARCODE_MIN_TAPE_MM, DPI, PRINTABLE_DOTS

PT_P700_MEDIA = {3.5: (263, 24, 24), 6: (257, 42, 32), 9: (258, 64, 50), 12: (259, 84, 70), 18: (260, 128, 112),
                 24: (261, 170, 128)}
PT_P700_PRINTER_ID = 0x6730  # (model code 0x67 << 8) | series code 0x30, from the driver's language-monitor INI
OBJECT_NAMES = ("PartName", "PartID", "BinCode", "Barcode")

_NS = " ".join(
    f'xmlns:{p}="http://schemas.brother.info/ptouch/2007/lbx/{p}"'
    for p in ("pt", "style", "text", "draw", "image", "barcode", "database", "table", "cable")
).replace("lbx/pt\"", "lbx/main\"")

_END_MARGIN_PT = 5.7     # ~2 mm blank tape before/after the printed area
_LINE = 1.18             # text frame height / font size


def _pt(dots: float) -> float:
    return round(dots * 72 / DPI, 1)


def _style(name: str, x: float, y: float, w: float, h: float) -> str:
    return (f'<pt:objectStyle x="{x}pt" y="{y}pt" width="{w}pt" height="{h}pt" backColor="#FFFFFF" '
            'backPrintColorNumber="0" ropMode="COPYPEN" angle="0" anchor="TOPLEFT" flip="NONE">'
            '<pt:pen style="NULL" widthX="0.5pt" widthY="0.5pt" color="#000000" printColorNumber="1"/>'
            '<pt:brush style="NULL" color="#000000" printColorNumber="1" id="0"/>'
            f'<pt:expanded objectName={quoteattr(name)} ID="0" lock="0" templateMergeTarget="LABELLIST" '
            'templateMergeType="NONE" templateMergeID="0" linkStatus="NONE" linkID="0"/></pt:objectStyle>')


def _font(size: float, bold: bool) -> str:
    return ('<text:ptFontInfo>'
            f'<text:logFont name="Arial" width="0" italic="false" weight="{700 if bold else 400}" charSet="0" '
            'pitchAndFamily="34"/>'
            f'<text:fontExt effect="NOEFFECT" underline="0" strikeout="0" size="{size}pt" orgSize="{size}pt" '
            'textColor="#000000" textPrintColorNumber="1"/></text:ptFontInfo>')


def _text(name: str, x: float, y: float, w: float, size: float, bold: bool, sample: str, fixed: bool) -> str:
    # FIXEDFRAME + shrink: the frame keeps its width and long text is scaled down to fit.
    control = "FIXEDFRAME" if fixed else "AUTOLEN"
    return ('<text:text>' + _style(name, x, y, w, round(size * _LINE, 1)) + _font(size, bold)
            + f'<text:textControl control="{control}" clipFrame="false" aspectNormal="true" shrink="true" '
            'autoLF="false" avoidImage="false"/>'
            '<text:textAlign horizontalAlignment="LEFT" verticalAlignment="CENTER" inLineAlignment="BASELINE"/>'
            f'<text:textStyle vertical="false" nullBlock="false" charSpace="0" lineSpace="0" orgPoint="{size}pt" '
            'combinedChars="false"/>'
            f'<pt:data>{escape(sample)}</pt:data>'
            f'<text:stringItem charLen="{len(sample)}">' + _font(size, bold) + '</text:stringItem></text:text>')


def _barcode(name: str, x: float, y: float, h: float, sample: str) -> str:
    # autoLengths: the module width stays at 0.8 pt (2 dots) and the symbol grows with the data,
    # which lengthens the (auto-length) label instead of squeezing the bars.
    width = round((len(sample) + 3) * 11 * 0.8 + 20, 1)
    return ('<barcode:barcode>' + _style(name, x, y, width, h)
            + '<barcode:barcodeStyle protocol="CODE128" lengths="48" zeroFill="false" barWidth="0.8pt" '
            'barRatio="1:3" humanReadable="false" humanReadableAlignment="LEFT" checkDigit="true" '
            'autoLengths="true" margin="true" sameLengthBar="false" bearerBar="false"/>'
            f'<pt:data>{escape(sample)}</pt:data></barcode:barcode>')


def label_xml(tape_mm: float, printer_name: str = "Brother PT-P700") -> str:
    if tape_mm not in PT_P700_MEDIA:
        raise ValueError(f"unsupported tape width {tape_mm} mm (PT-P700 takes {sorted(PT_P700_MEDIA)})")
    media_id, tape_dots, printable_dots = PT_P700_MEDIA[tape_mm]
    assert printable_dots == PRINTABLE_DOTS[tape_mm]
    tape_pt, printable = _pt(tape_dots), _pt(printable_dots)
    side = round((tape_pt - printable) / 2, 1)
    x0, top = _END_MARGIN_PT, side

    if tape_mm >= BARCODE_MIN_TAPE_MM:
        # same proportions as the preview: name 28 %, id 22 %, bin 25 % of the printable height (24 mm)
        k = (0.28, 0.22, 0.25) if tape_mm >= 18 else (0.32, 0.26, 0.28)
        s_name, s_id, s_bin = (round(_pt(printable_dots * f), 1) for f in k)
        text_w = {12: 120.0, 18: 140.0, 24: 150.0}[tape_mm]
        heights = [round(s * _LINE, 1) for s in (s_name, s_id, s_bin)]
        gap = max(0.0, round((printable - sum(heights)) / 4, 1))
        y_name = top + gap
        y_id = y_name + heights[0] + gap
        y_bin = y_id + heights[1] + gap
        caption_w = round(s_bin * 2.05, 1)
        objs = (_text("PartName", x0, y_name, text_w, s_name, True, "Part name", fixed=True)
                + _text("PartID", x0, y_id, text_w, s_id, False, "P-00000", fixed=True)
                + _text("BinCaption", x0, y_bin, caption_w, s_bin, True, "BIN", fixed=False)
                + _text("BinCode", x0 + caption_w, y_bin, text_w - caption_w, s_bin, True, "A-01", fixed=True)
                + _barcode("Barcode", x0 + text_w + 6, top + 1.6, round(printable - 3.2, 1), "P-00000"))
        content_w = text_w + 6 + 110
    else:
        # narrow tape: one line "name  id  BIN code", no barcode (as in the preview)
        s = round(_pt(printable_dots * 0.8), 1)
        y = top + round((printable - s * _LINE) / 2, 1)
        w_name, w_id, w_cap, w_bin = 120.0, round(s * 4.6, 1), round(s * 2.3, 1), round(s * 4.0, 1)
        objs = (_text("PartName", x0, y, w_name, s, True, "Part name", fixed=True)
                + _text("PartID", x0 + w_name + 4, y, w_id, s, False, "P-00000", fixed=True)
                + _text("BinCaption", x0 + w_name + w_id + 8, y, w_cap, s, True, "BIN", fixed=False)
                + _text("BinCode", x0 + w_name + w_id + w_cap + 8, y, w_bin, s, True, "A-01", fixed=True))
        content_w = w_name + w_id + w_cap + w_bin + 12

    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<pt:document {_NS} version="1.5" generator="P-touch Editor 5.2.014 Windows">'
            '<pt:body currentSheet="Sheet 1" direction="LTR"><style:sheet name="Sheet 1">'
            f'<style:paper media="0" width="{tape_pt}pt" height="2834.6pt" marginLeft="{side}pt" '
            f'marginTop="{x0}pt" marginRight="{side}pt" marginBottom="{x0}pt" orientation="landscape" '
            'autoLength="true" monochromeDisplay="true" printColorDisplay="false" printColorsID="0" '
            'paperColor="#FFFFFF" paperInk="#000000" split="1" '
            f'format="{media_id}" backgroundTheme="0" printerID="{PT_P700_PRINTER_ID}" '
            f'printerName={quoteattr(printer_name)}/>'
            '<style:cutLine regularCut="0pt" freeCut=""/>'
            f'<style:backGround x="{x0}pt" y="{side}pt" width="{round(content_w, 1)}pt" height="{printable}pt" '
            'brushStyle="NULL" brushId="0" userPattern="NONE" userPatternId="0" color="#000000" '
            'printColorNumber="1" backColor="#FFFFFF" backPrintColorNumber="0"/>'
            '<pt:objects>' + objs + '</pt:objects></style:sheet></pt:body></pt:document>')


def prop_xml(tape_mm: float) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<meta:properties xmlns:meta="http://schemas.brother.info/ptouch/2007/lbx/meta" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
            f'<meta:appName>P-touch Editor</meta:appName><dc:title>Torino part label {tape_mm:g} mm</dc:title>'
            '<dc:subject></dc:subject><dc:creator>Torino</dc:creator><meta:keyword></meta:keyword>'
            '<dc:description></dc:description><meta:template></meta:template>'
            f'<dcterms:created>{now}</dcterms:created><dcterms:modified>{now}</dcterms:modified>'
            '<meta:lastPrinted></meta:lastPrinted><meta:modifiedBy>Torino</meta:modifiedBy>'
            '<meta:revision>1</meta:revision><meta:editTime>0</meta:editTime><meta:numPages>1</meta:numPages>'
            '<meta:numWords>0</meta:numWords><meta:numChars>0</meta:numChars><meta:security>0</meta:security>'
            '</meta:properties>')


def write_template(path: Path | str, tape_mm: float, printer_name: str = "Brother PT-P700") -> Path:
    """Write a Torino label template for ``tape_mm`` TZe tape to ``path`` (overwrites)."""
    tape_mm = float(tape_mm)
    tape_mm = int(tape_mm) if tape_mm.is_integer() else tape_mm
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".lbx.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("label.xml", label_xml(tape_mm, printer_name))
        z.writestr("prop.xml", prop_xml(tape_mm))
    tmp.replace(path)
    return path


def check_with_bpac(template: Path | str, out_image: Path | str | None = None, *, name: str = "Door hinge LH",
                    part_id: str = "P-00001", bin_code: str = "B-04", dispatch=None) -> dict:
    """Open ``template`` with the installed b-PAC, fill sample data and (optionally) export a BMP.

    Nothing is printed. Returns which objects b-PAC found and the paper it resolved; the paper is
    only the TZe tape once Windows has a PT-P700 queue (otherwise b-PAC falls back to the default
    printer's paper).
    """
    import pythoncom

    if dispatch is None:
        dispatch = bpac_dispatch
    pythoncom.CoInitialize()
    doc = None
    try:
        doc = dispatch("bpac.Document")
        if not doc.Open(str(template)):
            return {"ok": False, "error": f"b-PAC could not open {template} (error {doc.ErrorCode})"}
        try:
            found = {n: doc.GetObject(n) is not None for n in OBJECT_NAMES}
            for n, v in (("PartName", name), ("PartID", part_id), ("BinCode", bin_code)):
                if found[n]:
                    doc.GetObject(n).Text = v
            idx = doc.GetBarcodeIndex("Barcode") if found["Barcode"] else -1
            if idx is not None and idx >= 0:
                doc.SetBarcodeData(idx, part_id)
            out = {"ok": all(found[n] for n in ("PartName", "PartID", "BinCode")), "objects": found,
                   "barcode_index": idx, "paper": com_call0(doc, "GetMediaName"),
                   "paper_id": com_call0(doc, "GetMediaId"), "printer": doc.Printer.Name}
            if out_image is not None:
                out["image"] = str(out_image) if doc.Export(4, str(out_image), DPI) else None
            return out
        finally:
            com_call0(doc, "Close")
    finally:
        doc = None  # release COM objects before COM is shut down on this thread
        pythoncom.CoUninitialize()


def default_template_path(tape_mm: float) -> Path:
    return Path.home() / "Documents" / "Labels" / f"torino-{float(tape_mm):g}mm.lbx"
