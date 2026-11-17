"""Sheets-specific behaviour: schema creation, atomic commits, header-based columns."""

import pytest
from conftest import make_store

from torino_parts.inventory import Inventory
from torino_parts.schema import TABS


def test_schema_created(tmp_path):
    s = make_store("sheets", tmp_path)
    assert set(s.fake.grids) == set(TABS)  # Sheet1 removed
    for tab, cols in TABS.items():
        assert s.fake.grids[tab][0] == cols


def test_stock_and_transaction_written_in_one_atomic_batch(tmp_path):
    s = make_store("sheets", tmp_path)
    inv = Inventory(s)
    inv.create_bin("b", "A-01")
    calls = s.fake.batch_calls
    inv.receive_stock("r1", "A-01", 2, "used", new_part={"name": "Hinge"}, print_label=False)
    assert s.fake.batch_calls == calls + 1  # part + stock + transactions together

    # a failed batch leaves nothing behind, and the retry applies exactly once
    s.fake.fail_next_batch = True
    with pytest.raises(RuntimeError):
        inv.receive_stock("r2", "A-01", 5, "used", part_id="P-00001", print_label=False)
    snap = s.read()
    assert [x["qty"] for x in snap["Stock"]] == [2]
    assert not [t for t in snap["Transactions"] if t["request_id"] == "r2"]
    inv.receive_stock("r2", "A-01", 5, "used", part_id="P-00001", print_label=False)
    inv.receive_stock("r2", "A-01", 5, "used", part_id="P-00001", print_label=False)
    assert [x["qty"] for x in s.read()["Stock"]] == [7]


def test_user_reordered_and_extra_columns(tmp_path):
    s = make_store("sheets", tmp_path)
    g = s.fake.grids["Bins"]
    g[0] = ["my_notes", "location", "bin_code", "description", "created_at"]  # user rearranged + added a column
    inv = Inventory(s)
    inv.create_bin("b1", "A-01", "Door hardware", "Shelf 1")
    row = s.fake.grids["Bins"][1]
    assert row[2] == "A-01" and row[1] == "Shelf 1" and row[0] == ""
    s.fake.grids["Bins"][1][0] = "keep me"
    assert inv.list_bin("A-01")["location"] == "Shelf 1"
    # updates only touch known columns
    inv.receive_stock("r", "A-01", 1, new_part={"name": "x"}, print_label=False)
    inv.receive_stock("r2", "A-01", 1, part_id="P-00001", print_label=False)
    assert s.fake.grids["Bins"][1][0] == "keep me"


def test_ensure_schema_adds_missing_columns(tmp_path):
    s = make_store("sheets", tmp_path)
    s.fake.grids["Parts"][0] = s.fake.grids["Parts"][0][:5]
    s.ensure_schema()
    assert s.fake.grids["Parts"][0] == TABS["Parts"]
