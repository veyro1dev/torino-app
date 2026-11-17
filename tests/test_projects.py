"""Custody, shopping list, money, notebook, progress/resume - on every store (demo JSON, SQLite, Google fake)."""

import base64

import pytest

from torino_parts.inventory import InventoryError


@pytest.fixture
def car(garage):
    g = garage
    for b in ("A-01", "C-01", "T-01"):
        g.create_bin(f"bin-{b}", b)
    g.create_bin("bin-bench", "BENCH", "Work bench", kind="work_area")
    v = g.add_vehicle("v1", 1970, "Ford", "Torino")["vehicle_id"]
    cal = g.receive_stock("c1", "A-01", 2, "used", new_part={"name": "Brake caliper", "side_position": "front"},
                          print_label=False)["part"]["part_id"]
    j1 = g.create_job("j1", v, "Install calipers", "brakes", estimate_min=30, steps=["Mount", "Bleed"],
                      parts=[{"part_id": cal, "qty": 2}])["job_id"]
    j2 = g.create_job("j2", v, "Rotors", "brakes", estimate_min=120, parts=[{"description": "Front rotor", "qty": 2}])["job_id"]
    return {"g": g, "v": v, "cal": cal, "j1": j1, "j2": j2}


def test_custody_partial_return_and_job_blocking(car):
    g, cal, j1 = car["g"], car["cal"], car["j1"]
    assert g.get_job(j1)["state"] == "ready_with_checks"  # on hand; fitment not recorded yet
    out = g.send_out("out-1", cal, "Smith Machine Shop", 2, "rebuild", expected_back="2026-12-01", job_id=j1)
    cid = out["custody"]["custody_id"]
    assert out["part"]["total_qty"] == 0 and out["part"]["away_qty"] == 2
    assert out["part"]["away"][0]["holder"] == "Smith Machine Shop"
    again = g.send_out("out-1", cal, "Smith Machine Shop", 2, "rebuild", expected_back="2026-12-01", job_id=j1)
    assert again["duplicate_request"] and again["custody"]["custody_id"] == cid and g.get_part(cal)["away_qty"] == 2

    job = g.get_job(j1)
    need = job["parts_needed"][0]
    assert job["state"] == "blocked" and need["status"] == "away"
    assert [b["type"] for b in job["blockers"]] == ["away"]  # away, not "buy more"
    assert "Smith Machine Shop" in job["blockers"][0]["text"] and cid in job["blockers"][0]["clear"]

    r1 = g.return_from_shop("ret-1", cid, 1, to_bin="C-01", condition="remanufactured")
    assert r1["status"] == "partially_returned" and r1["still_away"] == 1 and r1["custody"]["status"] == "partial"
    job = g.get_job(j1)
    assert job["parts_needed"][0]["on_hand"] == 1 and [b["type"] for b in job["blockers"]] == ["away"]
    assert "1 x" in job["blockers"][0]["text"]
    with pytest.raises(InventoryError):
        g.return_from_shop("ret-x", cid, 5)
    r2 = g.return_from_shop("ret-2", cid)
    assert r2["status"] == "returned" and r2["custody"]["status"] == "returned"
    assert g.get_job(j1)["state"] == "ready_with_checks" and g.list_away()["count"] == 0
    p = g.get_part(cal)
    assert {(s["bin_code"], s["condition"], s["qty"]) for s in p["stock"]} == {("C-01", "remanufactured", 1),
                                                                               ("A-01", "used", 1)}
    assert p["away_qty"] == 0
    # work areas are places in the shop
    g.move_stock("mv-bench", cal, "A-01", "BENCH", 1)
    entry = next(s for s in g.get_part(cal)["stock"] if s["bin_code"] == "BENCH")
    assert entry["place"] == "work_area" and entry["place_name"] == "Work bench"


def test_shopping_receive_partial_delivery_links_need_and_never_double_counts_payment(car):
    g, v, j2 = car["g"], car["v"], car["j2"]
    need = g.get_job(j2)["parts_needed"][0]["need_id"]
    it = g.add_shopping_item("sh-1", v, qty=2, vendor="RockAuto", link="https://example.com/rotor",
                             est_price="45.50", job_id=j2, need_id=need)["item"]
    assert it["name"] == "Front rotor" and it["est_total"] == 91.0 and it["status"] == "considering"
    g.add_shopping_item("sh-2", v, "Brake fluid DOT 3", 1, "ready", est_price=12)
    m = g.money_summary(v)
    assert m["possible_purchases_total"] == 103.0 and m["outstanding_orders_total"] == 0 and m["spent"] == 0

    assert "on the shopping list: SH-00001" in g.get_job(j2)["blockers"][0]["text"]
    g.update_shopping_item("sh-3", it["item_id"], status="ordered", order_ref="RA-123")
    b = g.get_job(j2)["blockers"][0]
    assert "on order: SH-00001 (2 to come from RockAuto)" in b["text"] and "receive SH-00001" in b["clear"]
    m = g.money_summary(v)
    assert m["outstanding_orders_total"] == 91.0 and m["possible_purchases_total"] == 12.0  # never added together
    paid = g.update_shopping_item("sh-4", it["item_id"], paid_amount="88.20", paid_date="2026-11-10")
    assert paid["expense_id"] and paid["item"]["paid"] == 88.2
    assert g.update_shopping_item("sh-4", it["item_id"], paid_amount="88.20", paid_date="2026-11-10")["duplicate_request"]
    with pytest.raises(InventoryError, match="already recorded"):
        g.update_shopping_item("sh-5", it["item_id"], paid_amount="88.20")
    with pytest.raises(InventoryError, match="already recorded"):
        g.record_expense("exp-dup", v, "88.20", vendor="RockAuto", item_id=it["item_id"])
    m = g.money_summary(v)
    assert m["spent"] == 88.2 and m["outstanding_orders_total"] == 0  # paid order is spending, not outstanding

    r1 = g.receive_shopping_item("rcv-1", it["item_id"], "T-01", qty=1)
    assert r1["status"] == "partially_received" and r1["qty_still_to_come"] == 1
    assert r1["item"]["status"] == "ordered" and r1["item"]["partially_received"]
    assert r1["label"]["status"] in ("previewed", "pending")  # same intake + label workflow
    part_id = r1["part"]["part_id"]
    job = g.get_job(j2)
    assert job["parts_needed"][0]["part_id"] == part_id and job["parts_needed"][0]["status"] == "short"
    assert g.receive_shopping_item("rcv-1", it["item_id"], "T-01", qty=1)["duplicate_request"]
    assert g.get_part(part_id)["total_qty"] == 1
    r2 = g.receive_shopping_item("rcv-2", it["item_id"], "T-01")
    assert r2["status"] == "received" and r2["item"]["status"] == "received" and g.get_part(part_id)["total_qty"] == 2
    with pytest.raises(InventoryError):
        g.receive_shopping_item("rcv-3", it["item_id"], "T-01")
    assert g.money_summary(v)["spent"] == 88.2  # receiving never adds a payment


def test_money_budget_refunds_receipts_and_duplicate_guard(car):
    g, v, j1 = car["g"], car["v"], car["j1"]
    g.set_budget("bud-1", v, "5000")
    g.set_budget("bud-2", v, "300", category="tools")
    pdf = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 receipt").decode()
    e1 = g.record_expense("e-1", v, "149.99", vendor="Harbor Freight", category="tools", description="Flare tool",
                          date="2026-11-01", receipt_base64=pdf)
    assert e1["expense"]["receipt_link"].endswith(".pdf") and e1["totals"]["spent"] == 149.99
    assert g.record_expense("e-1", v, "149.99", vendor="Harbor Freight", category="tools", description="Flare tool",
                            date="2026-11-01", receipt_base64=pdf)["duplicate_request"]  # retried MCP call
    with pytest.raises(InventoryError, match="already recorded"):
        g.record_expense("e-2", v, "149.99", vendor="harbor freight", category="tools", date="2026-11-01")
    g.record_expense("e-3", v, "149.99", vendor="Harbor Freight", category="tools", date="2026-11-01",
                     allow_duplicate=True, description="second one for the other side")
    g.record_expense("e-4", v, "60", vendor="Smith Machine Shop", category="machine shop", job_id=j1, date="2026-11-03")
    g.record_expense("e-5", v, "40", kind="refund", vendor="Harbor Freight", category="tools", date="2026-11-04")
    m = g.money_summary(v)
    assert m["spent"] == round(149.99 * 2 + 60 - 40, 2) and m["refunds_total"] == 40
    assert m["remaining"] == round(5000 - m["spent"], 2)
    tools = next(c for c in m["category_budgets"] if c["category"] == "tools")
    assert tools["spent"] == round(149.99 * 2 - 40, 2) and tools["remaining"] == round(300 - tools["spent"], 2)
    assert next(j for j in m["by_job"] if j["job_id"] == j1)["spent"] == 60
    assert m["by_vendor"][0]["vendor"] == "Harbor Freight"
    with pytest.raises(InventoryError):
        g.record_expense("e-6", v, "-5")


def test_notebook_links_search_and_job_notes(car):
    g, v, j1, cal = car["g"], car["v"], car["j1"], car["cal"]
    ph = g.attach_photo("ph-1", job_id=j1, caption="caliper bore", photo_base64=base64.b64encode(b"\xff\xd8\xff" + b"0" * 50).decode())
    n = g.add_note("n-1", "Bore is pitted on the left caliper, needs a sleeve", title="Caliper bore", tags="brakes, #measure",
                   job_id=j1, part_id=cal, photo_id=ph["photo_id"])["note"]
    assert n["job"]["title"] == "Install calipers" and n["part"]["part_id"] == cal and n["photo"]["photo_id"] == ph["photo_id"]
    assert n["vehicle_id"] == v and n["tags"] == "brakes, measure"
    g.update_job("u-1", j1, note="Ordered sleeve kit")
    nb = g.notebook(v)
    assert [x["body"] for x in nb["notes"]][:2] == ["Ordered sleeve kit", n["body"]]
    assert g.notebook(v, query="pitted sleeve")["count"] == 1
    assert g.notebook(v, query="install calipers")["count"] == 2  # matches the linked job title
    assert g.notebook(v, tag="measure")["count"] == 1
    assert g.add_note("n-1", "Bore is pitted on the left caliper, needs a sleeve", title="Caliper bore",
                      tags="brakes, #measure", job_id=j1, part_id=cal, photo_id=ph["photo_id"])["duplicate_request"]
    assert g.notebook(v)["count"] == 2


def test_time_filter_lists_each_job_once_and_resume(car):
    g, v, j1, j2 = car["g"], car["v"], car["j1"], car["j2"]
    r = g.what_next(v, minutes=60)
    ids = [j["job_id"] for j in r["fits_in_time"]] + [j["job_id"] for j in r["do_next"]] + [j["job_id"] for j in r["blocked"]]
    assert len(ids) == len(set(ids)) and [j["job_id"] for j in r["fits_in_time"]] == [j1]
    assert j1 not in [j["job_id"] for j in r["do_next"]]
    step = g.get_job(j1)["checklist"][0]["step_id"]
    g.update_job("s-1", j1, steps_done=[step], note="Mounted both, torqued to spec")
    res = g.resume(v)
    assert res["job"]["job_id"] == j1 and res["next_step"]["text"] == "Bleed"
    assert res["pull_list"][0]["part_id"] == car["cal"] and res["last_note"]["body"] == "Mounted both, torqued to spec"
    g.update_job("d-1", j1, status="done")
    pr = g.progress(v)
    assert pr["totals"]["done"] == 1 and pr["completed"][0]["job_id"] == j1 and pr["totals"]["steps_done"] == 1
    assert any(b["job_id"] == j2 for b in pr["blocked"])
    assert pr["resume"]["vehicle_id"] == v


def test_money_timeline_void_and_csv(car):
    g, v = car["g"], car["v"]
    g.set_budget("b", v, 1000)
    g.record_expense("t1", v, "100", vendor="A", date="2026-10-01")
    g.record_expense("t2", v, "50", vendor="B", date="2026-10-05")
    g.record_expense("t3", v, "20", vendor="B", kind="refund", date="2026-10-05")
    g.record_expense("t4", v, "999", vendor="C", date="2026-10-07")
    it = g.add_shopping_item("t5", v, "Seal kit", 1, "ordered", est_price=30)["item"]
    m = g.money_summary(v)
    assert [(p["date"], p["cumulative"]) for p in m["timeline"]] == [("2026-10-01", 100), ("2026-10-05", 130), ("2026-10-07", 1129)]
    assert m["outstanding_orders_total"] == 30 and m["timeline"][-1]["cumulative"] == m["spent"]  # orders not in the line
    e4 = next(e for e in m["expenses"] if e["amount"] == 999)["expense_id"]
    out = g.void_expense("void-1", e4, "wrong amount")
    assert out["totals"]["spent"] == 130 and g.void_expense("void-1", e4, "wrong amount")["duplicate_request"]
    with pytest.raises(InventoryError):
        g.void_expense("void-2", e4, "again")
    m = g.money_summary(v)
    assert [p["cumulative"] for p in m["timeline"]] == [100, 130] and m["remaining"] == 870
    # a payment for an item, voided, makes the order unpaid (outstanding) again and allows paying it properly
    pay = g.update_shopping_item("t6", it["item_id"], paid_amount=31)["expense_id"]
    assert g.money_summary(v)["outstanding_orders_total"] == 0
    g.void_expense("void-3", pay, "charged to the wrong card")
    assert g.money_summary(v)["outstanding_orders_total"] == 30
    assert g.update_shopping_item("t7", it["item_id"], paid_amount=30)["expense_id"]
    csv_text = g.money_csv(v)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("expense_id,date,kind,amount,net") and len(lines) == 1 + 6
    assert any("wrong amount" in ln and ",0.00," in ln for ln in lines)  # voided row kept, counts 0


def test_general_notes_show_in_project_notebook(car):
    g, v = car["g"], car["v"]
    g.add_note("gen-1", "Ordered a shop manual set")  # no vehicle, job or part
    assert g.notebook(v, query="manual")["count"] == 1
