"""Inventory behaviour; every test runs against the local store AND the Sheets store (fake API)."""

import pytest
from conftest import seed

from torino_parts.inventory import InventoryError


def stock_of(inv, part_id):
    return {(s["bin_code"], s["condition"]): s["qty"] for s in inv.get_part(part_id)["stock"]}


def test_search_left_door_hinges(inv):
    ids = seed(inv)
    res = inv.search_parts("Where are the left door hinges?")
    assert [r["part_id"] for r in res["results"]] == [ids["hinge_lh"]]
    r = res["results"][0]
    assert {(s["bin_code"], s["condition"], s["qty"]) for s in r["stock"]} == {("A-01", "used", 2), ("A-02", "new", 1)}
    assert r["total_by_condition"] == {"used": 2, "new": 1}
    # driver side synonym, and RH excluded the other way round
    assert [x["part_id"] for x in inv.search_parts("driver side door hinge")["results"]] == [ids["hinge_lh"]]
    assert [x["part_id"] for x in inv.search_parts("RH hinges")["results"]] == [ids["hinge_rh"]]


def test_search_fitment_filters(inv):
    ids = seed(inv)
    by_vehicle = inv.search_parts("hinge", vehicle_id=ids["torino"])["results"]
    lh = next(r for r in by_vehicle if r["part_id"] == ids["hinge_lh"])
    assert lh["fitment_match"] == "verified"
    montego = inv.search_parts("hinge", make="Mercury", model="Montego", year=1971)["results"]
    assert next(r for r in montego if r["part_id"] == ids["hinge_lh"])["fitment_match"] == "unverified"
    # unknown-fitment RH hinge is shown but flagged unknown, never as a fit
    assert next(r for r in montego if r["part_id"] == ids["hinge_rh"])["fitment_match"] == "unknown"
    assert inv.search_parts("hinge", vehicle_id=ids["monte"])["results"] == [
        r for r in inv.search_parts("hinge", vehicle_id=ids["monte"])["results"] if r["fitment_match"] == "unknown"]
    assert inv.search_parts("1971 montego hinge")["interpreted"]["year"] == 1971


def test_receive_is_idempotent_and_labels_once(inv):
    ids = seed(inv)
    args = dict(request_id="intake-1", bin_code="B-04", qty=3, condition="used", part_id=ids["hinge_lh"])
    a = inv.receive_stock(**args)
    b = inv.receive_stock(**args)
    assert not a["duplicate_request"] and b["duplicate_request"]
    assert stock_of(inv, ids["hinge_lh"])[("B-04", "used")] == 3
    snap = inv.store.read()
    assert len([t for t in snap["Transactions"] if t["request_id"] == "intake-1"]) == 1
    jobs = [j for j in inv.labels.queue.list(limit=100) if j["request_id"] == "intake-1"]
    assert len(jobs) == 1 and jobs[0]["attempts"] == 1
    assert a["label"]["job_id"] == b["label"]["job_id"]
    # same id, different content -> refused
    with pytest.raises(InventoryError, match="already used"):
        inv.receive_stock(**{**args, "qty": 4})


def test_new_part_intake_with_photo_and_fitment(inv, tmp_path):
    seed(inv)
    photo = tmp_path / "p.jpg"
    from PIL import Image
    Image.new("RGB", (20, 20), "gray").save(photo)
    out = inv.receive_stock("intake-2", "B-04", 1, "used", photo_path=str(photo),
                            new_part={"name": "Window crank handle", "stamped_number": "",
                                      "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970}]})
    p = out["part"]
    assert p["part_id"].startswith("P-") and p["photo_links"] and p["photo_links"][0].startswith("file:")
    assert p["fitment"][0]["verified"] == "unverified" and p["fitment"][0]["fits"] == "1970 Ford Torino"
    assert out["label"]["status"] == "previewed"
    # retry with the photo passed via a different temp path is still the same request
    photo2 = tmp_path / "copy.jpg"
    photo2.write_bytes(photo.read_bytes())
    again = inv.receive_stock("intake-2", "B-04", 1, "used", photo_path=str(photo2),
                              new_part={"name": "Window crank handle", "stamped_number": "",
                                        "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970}]})
    assert again["duplicate_request"] and again["part"]["part_id"] == p["part_id"]
    assert len(inv.search_parts("window crank")["results"]) == 1


def test_unidentified_and_universal_parts(inv):
    seed(inv)
    u = inv.receive_stock("u1", "A-01", 1, "used", new_part={"unidentified": True, "notes": "bracket"}, print_label=False)
    assert u["part"]["status"] == "unidentified" and u["part"]["fitment_scope"] == "unknown"
    assert u["part"]["name"] == "Unidentified part"
    f = inv.receive_stock("u2", "A-01", 10, "new", new_part={"name": "Hose clamp", "fitment_scope": "universal"}, print_label=False)
    assert inv.search_parts("clamp", make="Chevrolet")["results"][0]["fitment_match"] == "universal"
    with pytest.raises(InventoryError):
        inv.add_fitment("u3", f["part"]["part_id"], "Ford")
    upd = inv.update_part("u4", u["part"]["part_id"], name="Alternator bracket", identified=True)
    assert upd["part"]["status"] == "identified" and upd["part"]["name"] == "Alternator bracket"


def test_duplicate_oem_number_is_flagged(inv):
    seed(inv)
    inv.receive_stock("o1", "A-01", 1, "new", new_part={"name": "Water pump", "oem_number": "D0AZ-8501-B"}, print_label=False)
    with pytest.raises(InventoryError) as e:
        inv.receive_stock("o2", "A-01", 1, "new", new_part={"name": "Water pump", "oem_number": "d0az 8501 b"}, print_label=False)
    assert e.value.details["existing_part_id"]


def test_missing_bin_needs_confirmation(inv):
    ids = seed(inv)
    with pytest.raises(InventoryError) as e:
        inv.receive_stock("m1", "Z-99", 1, "used", part_id=ids["hinge_lh"])
    assert e.value.details["missing_bin"] == "Z-99"
    out = inv.receive_stock("m1", "Z-99", 1, "used", part_id=ids["hinge_lh"], create_bin_if_missing=True, print_label=False)
    assert out["bin_code"] == "Z-99"


def test_move_adjust_install(inv):
    ids = seed(inv)
    lh = ids["hinge_lh"]
    mv = inv.move_stock("mv1", lh, "A-01", "B-04", 1)
    assert mv["from"]["qty_after"] == 1 and mv["to"]["qty_after"] == 1
    assert inv.move_stock("mv1", lh, "A-01", "B-04", 1)["duplicate_request"]
    assert stock_of(inv, lh) == {("A-01", "used"): 1, ("A-02", "new"): 1, ("B-04", "used"): 1}
    with pytest.raises(InventoryError, match="only 1"):
        inv.move_stock("mv2", lh, "A-01", "B-04", 5)
    adj = inv.adjust_count("adj1", "cycle count", part_id=lh, bin_code="A-02", new_qty=3)
    assert (adj["qty_before"], adj["qty_after"]) == (1, 3)
    adj2 = inv.adjust_count("adj2", "dropped one", part_id=lh, bin_code="A-02", delta=-1)
    assert adj2["qty_after"] == 2
    with pytest.raises(InventoryError, match="negative"):
        inv.adjust_count("adj3", "x", part_id=lh, bin_code="A-02", delta=-9)
    # ambiguous install source (used in A-01 and B-04, new in A-02) -> must choose
    with pytest.raises(InventoryError) as e:
        inv.record_installation("i1", lh, ids["torino"])
    assert len(e.value.details["options"]) == 3
    ins = inv.record_installation("i1", lh, ids["torino"], bin_code="B-04")
    assert ins["qty_left_in_bin"] == 0 and ins["from_bin"] == "B-04"
    assert inv.record_installation("i1", lh, ids["torino"], bin_code="B-04")["duplicate_request"]
    veh = inv.get_vehicle(ids["torino"])
    assert [p["part_id"] for p in veh["installed_parts"]] == [lh]
    assert inv.get_part(lh)["installed_on"][0]["vehicle"].startswith(ids["torino"])
    # transaction history is complete
    types = [t["type"] for t in inv.history(part_id=lh, limit=100)["transactions"]]
    assert types.count("move_out") == 1 and types.count("install") == 1 and types.count("adjust") == 2


def test_donor_installed_and_fitment_are_distinct(inv):
    ids = seed(inv)
    lh = ids["hinge_lh"]
    inv.record_installation("i9", lh, ids["torino"], bin_code="A-01", condition="used")
    p = inv.get_part(lh)
    donor = [s["donor_vehicle"] for s in p["stock"] if s["condition"] == "used"]
    assert donor and donor[0].startswith(ids["monte"])          # came from the Monte Carlo
    assert p["installed_on"][0]["vehicle"].startswith(ids["torino"])  # went onto the Torino
    assert {f["fits"].split()[1] for f in p["fitment"]} == {"Ford", "Mercury"}  # fits Ford/Mercury
    assert inv.get_vehicle(ids["monte"])["donated_stock"][0]["part_id"] == lh


def test_reprint_does_not_change_inventory(inv):
    ids = seed(inv)
    before = inv.store.read()
    r1 = inv.print_label("rp1", ids["hinge_lh"], "A-01")
    r2 = inv.print_label("rp1", ids["hinge_lh"], "A-01")
    after = inv.store.read()
    assert before["Stock"] == after["Stock"] and len(before["Transactions"]) == len(after["Transactions"])
    assert r1["label"]["job_id"] == r2["label"]["job_id"] and r2["label"]["duplicate_request"]
    with pytest.raises(InventoryError, match="which bin"):
        inv.print_label("rp2", ids["hinge_lh"])  # stocked in two bins


def test_validation(inv):
    seed(inv)
    with pytest.raises(InventoryError):
        inv.receive_stock("", "A-01", 1, part_id="P-00001")
    with pytest.raises(InventoryError):
        inv.receive_stock("x", "A-01", 0, part_id="P-00001")
    with pytest.raises(InventoryError):
        inv.receive_stock("x", "A-01", 1, condition="shiny", part_id="P-00001")
    with pytest.raises(InventoryError):
        inv.add_vehicle("vv", 1970, "Ford", "Torino", vin="IOQ123")
    with pytest.raises(InventoryError):
        inv.receive_stock("x", "A-01", 1, new_part={"name": "x", "fitment": [{"make": "Ford", "year_from": 1972, "year_to": 1970}]})


def test_notes_with_formula_text_stay_text(inv):
    seed(inv)
    out = inv.receive_stock("f1", "A-01", 1, "used", print_label=False,
                            new_part={"name": "Bracket", "notes": "=IMPORTXML(\"x\")"})
    assert inv.get_part(out["part"]["part_id"])["notes"] == "=IMPORTXML(\"x\")"
