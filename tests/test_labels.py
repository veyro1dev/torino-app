"""Label queue, preview rendering and the b-PAC backend (driven by a fake COM object)."""

import copy

import pytest
from conftest import PRINTER_CFG
from fakes import FakeBpacWorld
from PIL import Image

from torino_parts.labels import LabelQueue, LabelService
from torino_parts.labels.printers import BpacBackend
from torino_parts.labels.render import PRINTABLE_DOTS, LabelContent, render_label


@pytest.fixture
def world(tmp_path):
    w = FakeBpacWorld()
    tpl = tmp_path / "template.lbx"
    tpl.write_bytes(b"lbx")
    w.template = str(tpl)
    return w


def live_service(tmp_path, world, auto_print=True, **over):
    cfg = copy.deepcopy(PRINTER_CFG)
    cfg.update(mode="live", auto_print=auto_print, template=world.template, timeout_s=2, **over)
    q = LabelQueue(tmp_path / "labels.db")
    return LabelService(q, cfg, tmp_path / "previews", backend=BpacBackend(cfg, dispatch=world.dispatch))


def test_live_print_success_and_no_double_print(tmp_path, world):
    svc = live_service(tmp_path, world)
    j = svc.request("intake", "r1", "P-00001", "Door hinge LH", "B-04")
    assert j["status"] == "printed"
    copies, texts, barcodes = world.printed[0]
    assert texts == {"PartName": "Door hinge LH", "PartID": "P-00001", "BinCode": "B-04"}
    assert barcodes == {0: "P-00001"}
    j2 = svc.request("intake", "r1", "P-00001", "Door hinge LH", "B-04")
    assert j2["duplicate_request"] and len(world.printed) == 1


def test_early_bound_bpac_also_prints_once(tmp_path, world):
    world.late_bound = False  # e.g. gencache/makepy wrappers: zero-arg members are real methods
    svc = live_service(tmp_path, world)
    assert svc.request("intake", "r1", "P-00001", "Door hinge LH", "B-04")["status"] == "printed"
    assert len(world.printed) == 1


def test_late_bound_endprint_is_printed_not_uncertain(tmp_path, world):
    # regression: under pywin32 late binding `doc.EndPrint()` ran EndPrint and then called its bool
    assert world.late_bound
    svc = live_service(tmp_path, world)
    j = svc.request("intake", "r1", "P-00001", "Door hinge LH", "B-04")
    assert j["status"] == "printed" and len(world.printed) == 1
    world.media = "12mm"  # the tape check must actually run under late binding too
    assert svc.request("intake", "r2", "P-00001", "Door hinge LH", "B-04")["status"] == "failed"


def test_bin_label_prints_scan_hint_like_preview(tmp_path, world):
    svc = live_service(tmp_path, world)
    svc.request("bin", "rb", "BIN:A-01", "Bin A-01", "A-01")
    _, texts, barcodes = world.printed[0]
    assert texts["PartID"] == "scan to open bin" and barcodes == {0: "BIN:A-01"}


def test_template_without_barcode_still_prints(tmp_path, world):
    world.barcode_objects = []  # e.g. a 9 mm template without room for a barcode
    svc = live_service(tmp_path, world, tape_mm=9)
    world.media = "9mm"
    assert svc.request("intake", "r1", "P-1", "Hinge", "A-01")["status"] == "printed"


def test_offline_keeps_job_pending_and_prints_later(tmp_path, world):
    world.online = False
    svc = live_service(tmp_path, world)
    j = svc.request("intake", "r1", "P-1", "Hinge", "A-01")
    assert j["status"] == "pending" and "offline" in j["last_error"]
    svc.queue.close()
    # restart: new service on the same DB; job persisted
    world.online = True
    svc2 = live_service(tmp_path, world)
    svc2.startup()
    assert svc2.queue.get(j["job_id"])["status"] == "printed" and len(world.printed) == 1


def test_auto_print_off_queues_until_processed(tmp_path, world):
    svc = live_service(tmp_path, world, auto_print=False)
    j = svc.request("intake", "r1", "P-1", "Hinge", "A-01")
    assert j["status"] == "pending" and j["preview_path"] and not world.printed
    svc.process_pending()
    assert svc.queue.get(j["job_id"])["status"] == "printed"


def test_endprint_failure_is_uncertain_and_not_retried_automatically(tmp_path, world):
    world.endprint_ok = False
    svc = live_service(tmp_path, world)
    j = svc.request("intake", "r1", "P-1", "Hinge", "A-01")
    assert j["status"] == "uncertain" and "may" in j["message"].lower()
    world.endprint_ok = True
    svc.process_pending()  # does not touch uncertain jobs
    assert svc.queue.get(j["job_id"])["status"] == "uncertain"
    assert svc.resolve(j["job_id"], "reprint")["status"] == "printed"


def test_endprint_exception_and_timeout_are_uncertain(tmp_path, world):
    world.endprint_raises = True
    svc = live_service(tmp_path, world)
    assert svc.request("intake", "r1", "P-1", "Hinge", "A-01")["status"] == "uncertain"
    world.endprint_raises = False
    world.hang_in_endprint = 3  # longer than timeout_s=2
    j = svc.request("intake", "r2", "P-1", "Hinge", "A-01")
    assert j["status"] == "uncertain" and "did not finish" in j["last_error"]


def test_wrong_tape_or_missing_template_fails_without_printing(tmp_path, world):
    world.media = "12mm"
    svc = live_service(tmp_path, world)
    j = svc.request("intake", "r1", "P-1", "Hinge", "A-01")
    assert j["status"] == "failed" and "12 mm" in j["last_error"] and not world.printed
    world.media = "24mm"
    svc.cfg["template"] = str(tmp_path / "missing.lbx")
    assert svc.request("intake", "r2", "P-1", "Hinge", "A-01")["status"] == "failed"
    svc.cfg["template"] = world.template
    assert svc.resolve(j["job_id"], "reprint")["status"] == "printed"


def test_crash_while_sending_becomes_uncertain(tmp_path):
    q = LabelQueue(tmp_path / "labels.db")
    job, _ = q.enqueue("intake", "r1", "P-1", "Hinge", "A-01")
    assert q.claim(job["job_id"], "Brother PT-P700", 24)
    with q._tx() as c:  # pretend the owner process died mid-send
        c.execute("UPDATE jobs SET owner_pid=999999 WHERE job_id=?", (job["job_id"],))
    assert q.recover() == [job["job_id"]]
    assert q.get(job["job_id"])["status"] == "uncertain"


def test_recover_ignores_jobs_owned_by_live_process(tmp_path):
    import subprocess
    import sys
    q = LabelQueue(tmp_path / "labels.db")
    job, _ = q.enqueue("intake", "r1", "P-1", "Hinge", "A-01")
    q.claim(job["job_id"], "x", 24)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with q._tx() as c:
            c.execute("UPDATE jobs SET owner_pid=? WHERE job_id=?", (other.pid, job["job_id"]))
        assert q.recover() == []
        assert q.get(job["job_id"])["status"] == "sending"
    finally:
        other.kill()


def test_label_conflict_on_reused_request_id(tmp_path):
    q = LabelQueue(tmp_path / "labels.db")
    q.enqueue("reprint", "r1", "P-1", "Hinge", "A-01")
    with pytest.raises(ValueError, match="already used"):
        q.enqueue("reprint", "r1", "P-2", "Other", "A-01")


@pytest.mark.parametrize("tape", sorted(PRINTABLE_DOTS))
def test_preview_all_tape_widths(tmp_path, tape):
    out = render_label(LabelContent("Door hinge, upper LH FR", "P-00001", "B-04"), tape, tmp_path / f"{tape}.png", scale=1)
    img = Image.open(out)
    assert img.height == round(tape / 25.4 * 180)
    assert img.width > img.height


def test_barcode_only_when_it_fits(tmp_path):
    def bar_columns(tape):
        """Columns that are dark for >= 80% of the printable height: only barcode bars do that."""
        img = Image.open(render_label(LabelContent("Door hinge", "P-00001", "B-04"), tape,
                                      tmp_path / f"b{tape}.png", scale=1)).convert("L")
        px = img.load()
        need = int(PRINTABLE_DOTS[tape] * 0.8)
        return sum(1 for x in range(img.width) if sum(px[x, y] < 128 for y in range(img.height)) >= need)
    assert bar_columns(24) > 50 and bar_columns(12) > 50
    assert bar_columns(9) == 0 and bar_columns(6) == 0
