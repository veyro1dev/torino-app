"""Generated PT-P700 .lbx templates: structure, and (when installed) acceptance by the real b-PAC."""

import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

from torino_parts.labels.lbx import PT_P700_MEDIA, check_with_bpac, write_template
from torino_parts.labels.render import BARCODE_MIN_TAPE_MM, PRINTABLE_DOTS

PT = "{http://schemas.brother.info/ptouch/2007/lbx/main}"
STYLE = "{http://schemas.brother.info/ptouch/2007/lbx/style}"


def _parse(path):
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["label.xml", "prop.xml"]
        ET.fromstring(z.read("prop.xml"))
        return ET.fromstring(z.read("label.xml"))


@pytest.mark.parametrize("tape", sorted(PT_P700_MEDIA))
def test_template_structure_every_tape(tmp_path, tape):
    root = _parse(write_template(tmp_path / f"t{tape}.lbx", tape))
    paper = root.find(f".//{STYLE}paper")
    media_id, tape_dots, printable_dots = PT_P700_MEDIA[tape]
    assert paper.get("format") == str(media_id) and paper.get("autoLength") == "true"
    assert printable_dots == PRINTABLE_DOTS[tape]
    assert float(paper.get("width").removesuffix("pt")) == pytest.approx(tape_dots * 72 / 180, abs=0.1)
    names = {e.get("objectName") for e in root.iter(f"{PT}expanded")}
    assert {"PartName", "PartID", "BinCode"} <= names
    assert ("Barcode" in names) == (tape >= BARCODE_MIN_TAPE_MM)
    # every object sits inside the printable band of the tape
    side = float(paper.get("marginLeft").removesuffix("pt"))
    band = (side - 0.05, side + printable_dots * 72 / 180 + 0.05)
    for st in root.iter(f"{PT}objectStyle"):
        y, h = (float(st.get(k).removesuffix("pt")) for k in ("y", "height"))
        assert band[0] <= y and y + h <= band[1] + 0.6, (st.find(f"{PT}expanded").get("objectName"), y, h, band)


def test_rejects_unknown_tape(tmp_path):
    with pytest.raises(ValueError):
        write_template(tmp_path / "x.lbx", 36)


def _bpac_installed():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "bpac.Document"))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _bpac_installed(), reason="Brother b-PAC client not installed")
@pytest.mark.parametrize("tape", [24, 12, 9])
def test_real_bpac_opens_fills_and_renders(tmp_path, tape):
    from PIL import Image

    tpl = write_template(tmp_path / f"t{tape}.lbx", tape)
    out = check_with_bpac(tpl, tmp_path / f"t{tape}.bmp", name="Front brake caliper bracket, left side")
    assert out["ok"], out
    assert out["objects"]["Barcode"] == (tape >= BARCODE_MIN_TAPE_MM)
    img = Image.open(out["image"]).convert("L")
    assert img.getextrema()[0] == 0  # something black was drawn
    if tape >= BARCODE_MIN_TAPE_MM:
        import zxingcpp
        hits = [r.text for r in zxingcpp.read_barcodes(img.convert("RGB"))]
        assert "P-00001" in hits, hits
