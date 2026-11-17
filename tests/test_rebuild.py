"""Rebuild workflow: jobs, dependencies, part needs, what-next, installs, teardown, photos, specs, upgrades."""

import json

import pytest
from conftest import make_store
from PIL import Image

from torino_parts.inventory import InventoryError
from torino_parts.rebuild import Garage
from torino_parts.schema import TABS


@pytest.fixture
def car(garage):
    g = garage
    for b in ("A-01", "A-02", "B-04"):
        g.create_bin(f"bin-{b}", b)
    v = g.add_vehicle("veh", 1970, "Ford", "Torino", nickname="Torino")["vehicle_id"]
    kit = g.receive_stock("kit", "A-01", 2, "new", print_label=False, new_part={
        "name": "Caliper rebuild kit", "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970, "year_to": 1971, "verified": True}]})["part"]["part_id"]
    hinge = g.receive_stock("hinge", "A-02", 1, "used", print_label=False, new_part={
        "name": "Door hinge, upper", "side_position": "left",
        "fitment": [{"make": "Mercury", "model": "Montego", "year_from": 1970, "year_to": 1971}]})["part"]["part_id"]
    j1 = g.create_job("j1", v, "Rebuild front calipers", "brakes", goal="Calipers rebuilt and painted", estimate_min=90,
                      steps=["Remove calipers", "Replace seals", "Reinstall"], parts=[{"part_id": kit, "qty": 2}])["job_id"]
    j2 = g.create_job("j2", v, "Install front rotors", "brakes", depends_on=[j1], estimate_min=60,
                      parts=[{"description": "Front rotor, 11.3 in", "qty": 2}])["job_id"]
    j3 = g.create_job("j3", v, "Hang left door", "body", estimate_min=45,
                      parts=[{"part_id": hinge, "qty": 1, "check": "inspect"}])["job_id"]
    return {"g": g, "v": v, "kit": kit, "hinge": hinge, "j1": j1, "j2": j2, "j3": j3}


def test_what_next_uses_dependencies_and_parts(car):
    g = car["g"]
    nxt = g.what_next(car["v"])
    states = {j["job_id"]: j["state"] for j in nxt["do_next"] + nxt["blocked"]}
    assert states == {car["j1"]: "ready", car["j2"]: "blocked", car["j3"]: "ready_with_checks"}
    assert nxt["do_next"][0]["job_id"] == car["j1"]  # ready + unblocks J2 beats ready_with_checks
    assert nxt["do_next"][0]["unblocks"] == [car["j2"]]
    assert nxt["do_next"][0]["pull_list"][0]["bins"] == ["A-01 (2 new)"]
    blocked = nxt["blocked"][0]
    assert [b["type"] for b in blocked["blockers"]] == ["job", "part_unknown"]
    assert "Finish " + car["j1"] in blocked["blockers"][0]["clear"]
    j3 = next(j for j in nxt["do_next"] if j["job_id"] == car["j3"])
    assert any("Inspect" in c for c in j3["checks"]) and any("Mercury" not in c and "fits" in c for c in j3["checks"])


def test_missing_vs_needs_check(car):
    g = car["g"]
    job = g.get_job(car["j3"])
    need = job["parts_needed"][0]
    assert need["status"] == "needs_check" and need["on_hand"] == 1 and need["fitment"] == "not_listed"
    assert {c["type"] for c in job["checks"]} == {"inspect", "fitment"}
    g.update_job_part("chk", need["need_id"], check_status="passed")
    assert g.get_job(car["j3"])["state"] == "ready"
    rotors = g.get_job(car["j2"])["parts_needed"][0]
    assert rotors["status"] == "missing" and rotors["part_id"] is None


def test_time_budget_uses_estimates_and_progress(car):
    g = car["g"]
    r = g.what_next(car["v"], minutes=40)
    assert r["fits_in_time"] == [] and "quick_checks" in r
    steps = g.get_job(car["j1"])["checklist"]
    g.update_job("tick", car["j1"], steps_done=[steps[0]["step_id"], steps[1]["step_id"]], status="in_progress")
    r = g.what_next(car["v"], minutes=40)
    assert [j["job_id"] for j in r["fits_in_time"]] == [car["j1"]]
    assert r["fits_in_time"][0]["remaining_min"] == 30 and r["fits_in_time"][0]["next_step"] == "Reinstall"


def test_job_installation_updates_stock_vehicle_and_job_atomically_once(car):
    g = car["g"]
    need = g.get_job(car["j1"])["parts_needed"][0]
    args = dict(request_id="inst-1", job_id=car["j1"], need_id=need["need_id"], qty=1)
    a = g.record_job_installation(**args)
    b = g.record_job_installation(**args)
    assert not a["duplicate_request"] and b["duplicate_request"]
    snap = g.store.read()
    assert [s["qty"] for s in snap["Stock"] if s["part_id"] == car["kit"]] == [1]
    inst = [i for i in snap["Installations"] if i["request_id"] == "inst-1"]
    assert len(inst) == 1 and inst[0]["job_id"] == car["j1"] and inst[0]["vehicle_id"] == car["v"]
    job = g.get_job(car["j1"])
    assert job["status"] == "in_progress" and job["parts_needed"][0]["qty_installed"] == 1
    assert job["activity"][0]["type"] == "installed" and len([x for x in snap["Activity"] if x["request_id"] == "inst-1"]) == 1
    assert job["where_you_left_off"]["last_activity"]["type"] == "installed"
    assert g.get_vehicle(car["v"])["installed_parts"][0]["part_id"] == car["kit"]


def test_teardown_removal_with_photo_label_and_reinstall_link(car, tmp_path):
    g = car["g"]
    td = g.create_job("td", car["v"], "Strip passenger door", "body", kind="teardown")["job_id"]
    photo = tmp_path / "inplace.jpg"
    Image.new("RGB", (40, 30), "gray").save(photo)
    args = dict(request_id="rm-1", vehicle_id=car["v"], bin_code="B-04", qty=2, removed_from="right door, upper",
                job_id=td, reinstall_job_id=car["j3"], photo_path=str(photo),
                new_part={"name": "Door hinge bolt", "category": "hardware"})
    a = g.record_removal(**args)
    b = g.record_removal(**args)
    assert b["duplicate_request"] and a["part"]["part_id"] == b["part"]["part_id"]
    assert a["reinstall_need_id"] == b["reinstall_need_id"] and a["photo_id"] == b["photo_id"]
    assert a["label"]["status"] == "previewed" and b["label"]["job_id"] == a["label"]["job_id"]
    snap = g.store.read()
    st = [s for s in snap["Stock"] if s["part_id"] == a["part"]["part_id"]]
    assert len(st) == 1 and st[0]["qty"] == 2 and st[0]["donor_vehicle_id"] == car["v"] and st[0]["removed_from"] == "right door, upper"
    need = next(n for n in g.get_job(car["j3"])["parts_needed"] if n["need_id"] == a["reinstall_need_id"])
    assert need["check"] == "inspect" and need["fitment"] == "verified" and need["fitment_note"] == "came off this vehicle"
    assert g.get_job(td)["status"] == "in_progress"
    assert g.list_photos(job_id=td)["photos"][0]["stage"] == "in place"


def test_dependencies_cycles_hold_and_done_warnings(car):
    g = car["g"]
    with pytest.raises(InventoryError, match="circular"):
        g.update_job("cyc", car["j1"], add_depends_on=[car["j2"]])
    with pytest.raises(InventoryError, match="hold_reason"):
        g.update_job("hold0", car["j1"], status="on_hold")
    g.update_job("hold", car["j1"], status="on_hold", hold_reason="waiting for brake fluid")
    assert g.get_job(car["j1"])["state"] == "on_hold"
    g.update_job("resume", car["j1"], status="in_progress")
    done = g.update_job("done", car["j1"], status="done")
    assert done["warnings"] and g.get_job(car["j2"])["blockers"][0]["type"] == "part_unknown"
    g.update_job("rmdep", car["j2"], remove_depends_on=[car["j1"]])
    assert g.get_job(car["j2"])["depends_on"] == []
    assert all(b["type"] != "job" for b in g.get_job(car["j2"])["blockers"])


def test_notes_photos_specs_and_lookup(car, tmp_path):
    g = car["g"]
    g.add_job_note("n1", car["j1"], "Left caliper piston seized, soaking in PB Blaster")
    assert g.get_job(car["j1"])["activity"][0]["summary"].startswith("Left caliper")
    photo = tmp_path / "p.png"
    Image.new("RGB", (10, 10), "red").save(photo)
    p1 = g.attach_photo("ph1", job_id=car["j1"], part_id=car["kit"], caption="kit contents", photo_path=str(photo))
    assert g.attach_photo("ph1", job_id=car["j1"], part_id=car["kit"], caption="kit contents", photo_path=str(photo))["photo_id"] == p1["photo_id"]
    assert g.get_part(car["kit"])["photo_links"] and g.get_job(car["j1"])["photos"][0]["photo_id"] == p1["photo_id"]
    with pytest.raises(InventoryError, match="source"):
        g.record_spec("s0", "Caliper bolt torque", "30-40", source="", unit="ft-lb")
    sp = g.record_spec("s1", "Caliper anchor bolt torque", "90-120", unit="ft-lb", source="1970 Ford Car Shop Manual Vol 1 (spec page)",
                       job_id=car["j1"])
    assert g.get_job(car["j1"])["specs"][0]["spec_id"] == sp["spec_id"]
    assert g.lookup_code(car["kit"])["kind"] == "part"
    assert g.lookup_code("BIN:A-01")["bin"]["items"][0]["part_id"] == car["kit"]
    assert g.lookup_code("a-02")["kind"] == "bin"
    assert g.lookup_code(car["j1"].lower())["kind"] == "job"
    assert g.lookup_code("p1")["part"]["part_id"] == car["kit"]
    with pytest.raises(InventoryError):
        g.lookup_code("zzzz-nothing")


def test_bin_label(car):
    out = car["g"].print_bin_label("bl1", "B-04")
    assert out["label"]["part_id"] == "BIN:B-04" and out["label"]["status"] == "previewed"
    assert ":" not in out["label"]["preview_path"].split("\\")[-1]


V1_TABS = ["Parts", "Vehicles", "Fitment", "Bins", "Stock", "Installations", "Transactions"]


def test_upgrade_v1_local_workbook_preserves_data(tmp_path):
    from torino_parts.store import LocalStore
    v1 = {t: [] for t in V1_TABS}
    v1["Parts"].append({"part_id": "P-00007", "name": "Old part", "status": "identified", "fitment_scope": "unknown",
                        "my_column": "keep"})
    v1["Bins"].append({"bin_code": "A-01"})
    v1["Stock"].append({"stock_id": "S-00003", "part_id": "P-00007", "bin_code": "A-01", "condition": "used", "qty": 2})
    (tmp_path / "wb.json").write_text(json.dumps(v1))
    g = Garage(LocalStore(tmp_path / "wb.json"))
    snap = g.store.read()
    assert set(snap) == set(TABS) and snap["Parts"][0]["my_column"] == "keep"
    v = g.add_vehicle("v", 1970, "Ford", "Torino")["vehicle_id"]
    j = g.create_job("j", v, "Use old part", parts=[{"part_id": "P-00007"}])["job_id"]
    assert g.get_job(j)["parts_needed"][0]["on_hand"] == 2
    new = g.receive_stock("r", "A-01", 1, new_part={"name": "New"}, print_label=False)
    assert new["part"]["part_id"] == "P-00008"  # IDs continue after existing ones


def test_upgrade_v1_google_sheet_preserves_ids_and_columns(tmp_path):
    from torino_parts.store.sheets import SheetsStore
    from fakes import FakeSheets
    fake = FakeSheets()
    v1_store = SheetsStore(fake, "s", tmp_path / "l")
    import torino_parts.store.sheets as sheets_mod
    full = dict(sheets_mod.TABS)
    try:
        sheets_mod.TABS = {k: full[k] for k in V1_TABS}  # build an "old" workbook
        v1_store.ensure_schema()
    finally:
        sheets_mod.TABS = full
    fake.grids["Parts"][0].append("my_column")
    fake.grids["Parts"].append(["P-00042", "Old hinge", "identified", "", "", "", "", "unknown", "", "", "", "", "", "keep me"])
    fake.grids["Stock"][0] = fake.grids["Stock"][0][:8]  # pre-"removed_from" header
    fake.grids["Stock"].append(["S-00009", "P-00042", "A-01", "used", 3, "", "", ""])
    store = SheetsStore(fake, "s", tmp_path / "l2")
    store.ensure_schema()  # upgrade
    assert set(fake.grids) == set(TABS)
    assert fake.grids["Parts"][0][-1] == "my_column" and fake.grids["Parts"][1][-1] == "keep me"
    assert fake.grids["Stock"][0][-1] == "removed_from"
    g = Garage(store)
    g.create_bin("b", "A-01")
    v = g.add_vehicle("v", 1970, "Ford", "Torino")["vehicle_id"]
    j = g.create_job("j", v, "Hang door", parts=[{"part_id": "P-00042", "qty": 2}])["job_id"]
    need = g.get_job(j)["parts_needed"][0]
    assert need["on_hand"] == 3 and need["pull_from"][0]["stock_id"] == "S-00009"
    g.record_job_installation("i", j, need_id=need["need_id"])
    assert fake.grids["Parts"][1][-1] == "keep me"
    assert [r for r in fake.grids["Stock"] if r and r[0] == "S-00009"][0][4] == 2
