"""End-to-end: launch `torino serve` as a stdio MCP server and call tools like an AI client would."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

EXPECTED_TOOLS = {
    "search_parts", "get_part", "list_bin_contents", "list_bins", "list_vehicles", "get_vehicle",
    "transaction_history", "inventory_info", "create_bin", "add_vehicle", "receive_stock", "move_stock",
    "adjust_count", "record_installation", "add_fitment", "set_fitment_verified", "update_part",
    "print_label", "label_status", "process_label_queue", "resolve_label_job",
    # rebuild workflow
    "vehicle_overview", "what_next", "list_jobs", "get_job", "activity_log", "lookup_code", "list_photos",
    "list_specs", "set_active_vehicle", "create_job", "update_job", "update_job_part", "record_job_installation",
    "record_removal", "attach_photo", "record_spec", "print_bin_label",
    # v0.3 project features
    "resume", "progress", "notebook", "add_note", "list_away", "send_out", "return_from_shop", "list_shopping",
    "add_shopping_item", "update_shopping_item", "receive_shopping_item", "money_summary", "record_expense",
    "set_budget", "data_backups", "backup_local_data",
    # v0.3.2
    "preview_label", "void_expense",
}


def test_mcp_project_workflows_and_retries(tmp_path):
    """Notebook, shopping -> order (paid once) -> partial delivery with label, expenses, outside shop - over stdio,
    with every write retried using the same request_id."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _client(tp):
        env = {**os.environ, "TORINO_HOME": str(tp / "home")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "torino_parts.cli", "serve"], env=env)
        async with Client(params, read_timeout_seconds=60) as c:
            async def call(_tool, **args):
                r = await c.call_tool(_tool, args)
                if r.is_error:
                    return {"__error__": r.content[0].text}
                return _payload(r)
            yield call

    async def run():
        async with _client(tmp_path) as call:
            v = (await call("add_vehicle", request_id="v", year=1970, make="Ford", model="Torino"))["vehicle_id"]
            await call("set_active_vehicle", vehicle_id=v)
            await call("create_bin", request_id="b", bin_code="T-01")
            await call("create_bin", request_id="bench", bin_code="BENCH", kind="work_area", description="Bench")
            job = (await call("create_job", request_id="j", title="Front brakes", system="brakes",
                              parts=[{"description": "Front rotor", "qty": 2}]))["job_id"]
            n1 = await call("add_note", request_id="note-1", body="Rotors are scored", job_id=job, tags="brakes")
            n2 = await call("add_note", request_id="note-1", body="Rotors are scored", job_id=job, tags="brakes")
            it = (await call("add_shopping_item", request_id="shop-1", name="Front rotor", qty=2, est_price=45.5,
                             vendor="RockAuto", job_id=job))["item"]
            o1 = await call("update_shopping_item", request_id="order-1", item_id=it["item_id"], status="ordered",
                            paid_amount=88.2)
            o2 = await call("update_shopping_item", request_id="order-1", item_id=it["item_id"], status="ordered",
                            paid_amount=88.2)
            dup = await call("record_expense", request_id="exp-x", amount=88.2, vendor="RockAuto", item_id=it["item_id"])
            r1 = await call("receive_shopping_item", request_id="rcv-1", item_id=it["item_id"], bin_code="T-01", qty=1)
            r1b = await call("receive_shopping_item", request_id="rcv-1", item_id=it["item_id"], bin_code="T-01", qty=1)
            m = await call("money_summary")
            out = await call("send_out", request_id="out-1", part_id=r1["part"]["part_id"], holder="Smith Machine",
                             reason="turn", from_bin="T-01")
            away = await call("list_away")
            nb = await call("notebook", query="scored")
            res = await call("resume")
            labels = await call("label_status")
            prev = await call("preview_label", part_id=r1["part"]["part_id"], bin_code="T-01")
            labels_after_preview = await call("label_status")
            void = await call("void_expense", request_id="void-1", expense_id=o1["expense_id"], reason="entered twice")
            m2 = await call("money_summary")
            return locals()

    r = asyncio.run(run())
    assert r["n2"]["duplicate_request"] and r["n2"]["note_id"] == r["n1"]["note_id"]
    assert r["o1"]["expense_id"] and r["o2"]["duplicate_request"] and r["o2"]["expense_id"] == r["o1"]["expense_id"]
    assert "already recorded" in r["dup"]["__error__"]
    assert r["r1"]["status"] == "partially_received" and r["r1b"]["duplicate_request"]
    assert r["m"]["spent"] == 88.2 and len(r["m"]["expenses"]) == 1 and r["m"]["outstanding_orders_total"] == 0
    assert r["away"]["count"] == 1 and r["away"]["away"][0]["holder"] == "Smith Machine"
    assert r["nb"]["count"] == 1 and r["nb"]["notes"][0]["job"]["job_id"] == r["job"]
    intake_labels = [j for j in r["labels"]["recent_jobs"] if j["kind"] == "intake"]
    assert len(intake_labels) == 1  # retried receive did not queue a second label
    from pathlib import Path
    from PIL import Image
    pv = r["prev"]
    assert pv["printed"] is False and pv["lines"]["bin"] == "T-01" and Path(pv["preview_path"]).is_file()
    assert Image.open(pv["preview_path"]).height == round(24 / 25.4 * 180) * 3  # 24 mm tape at 180 dpi, x3
    assert len(r["labels_after_preview"]["recent_jobs"]) == len(r["labels"]["recent_jobs"])  # nothing queued
    assert r["void"]["status"] == "voided" and r["m2"]["spent"] == 0
    assert r["m2"]["outstanding_orders_total"] == 91.0  # the order is unpaid again (still ordered)
    assert [e["voided"] for e in r["m2"]["expenses"]][0].endswith("entered twice")


def _payload(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def _session(tmp_path: Path):
    env = {**os.environ, "TORINO_HOME": str(tmp_path / "home")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "torino_parts.cli", "serve"], env=env)
    async with Client(params, read_timeout_seconds=60) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools.tools}
        assert EXPECTED_TOOLS <= names, EXPECTED_TOOLS - names
        assert "request_id" in c.instructions if c.instructions else True

        async def call(name, **args):
            r = await c.call_tool(name, args)
            if r.is_error:
                return {"__error__": r.content[0].text}
            return _payload(r)

        await call("create_bin", request_id="b1", bin_code="B-04", description="Intake shelf")
        v = await call("add_vehicle", request_id="v1", year=1970, make="Ford", model="Torino", nickname="Torino")
        photo = tmp_path / "photo.png"
        from PIL import Image
        Image.new("RGB", (32, 32), "gray").save(photo)
        intake = dict(request_id="intake-b04-1", bin_code="B-04", qty=1, condition="used",
                      new_part={"name": "Door hinge, upper", "side_position": "left",
                                "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970, "year_to": 1971}]},
                      photo_path=str(photo))
        first = await call("receive_stock", **intake)
        second = await call("receive_stock", **intake)
        found = await call("search_parts", query="Where are the left door hinges?")
        status = await call("label_status")
        bad = await call("receive_stock", request_id="x", bin_code="Z-99", qty=1, part_id=first["part"]["part_id"])
        reprint = await call("print_label", request_id="rp1", part_id=first["part"]["part_id"], bin_code="B-04")
        inst = await call("record_installation", request_id="i1", part_id=first["part"]["part_id"],
                          vehicle_id=v["vehicle_id"], bin_code="B-04")
        # rebuild tools
        await call("receive_stock", request_id="kit", bin_code="B-04", qty=2, condition="new",
                   new_part={"name": "Caliper rebuild kit"}, print_label=False)
        j1 = await call("create_job", request_id="j1", title="Rebuild calipers", system="brakes", estimate_min=60,
                        steps=["Remove", "Rebuild"], parts=[{"part_id": "P-00002", "qty": 2}])
        j2 = await call("create_job", request_id="j2", title="Mount rotors", system="brakes",
                        depends_on=[j1["job_id"]], parts=[{"description": "Front rotor", "qty": 2}])
        nxt = await call("what_next", minutes=60)
        blocked = await call("get_job", job_id=j2["job_id"])
        need = (await call("get_job", job_id=j1["job_id"]))["parts_needed"][0]["need_id"]
        ji = await call("record_job_installation", request_id="ji", job_id=j1["job_id"], need_id=need, qty=1)
        ji2 = await call("record_job_installation", request_id="ji", job_id=j1["job_id"], need_id=need, qty=1)
        info = await call("inventory_info")
        rebuild = (j1, j2, nxt, blocked, ji, ji2, info)
        return first, second, found, status, bad, reprint, inst, rebuild


def test_mcp_server_end_to_end(tmp_path):
    first, second, found, status, bad, reprint, inst, rebuild = asyncio.run(_session(tmp_path))
    j1, j2, nxt, blocked, ji, ji2, info = rebuild
    assert info["demo_mode"] is True
    assert [j["job_id"] for j in nxt["fits_in_time"]] == [j1["job_id"]]
    assert [b["type"] for b in blocked["blockers"]] == ["job", "part_unknown"]
    assert ji["job"]["parts_needed"][0]["qty_installed"] == 1 and ji2["duplicate_request"] is True
    assert ji2["job"]["parts_needed"][0]["qty_installed"] == 1
    assert ji["job"]["activity"][0]["source"] == "mcp"
    assert first["label"]["status"] == "previewed" and Path(first["label"]["preview_path"]).is_file()
    assert first["part"]["photo_links"]
    assert second["duplicate_request"] and second["label"]["job_id"] == first["label"]["job_id"]
    assert found["results"][0]["stock"][0] == {"stock_id": "S-00001", "bin_code": "B-04", "condition": "used", "qty": 1}
    assert status["jobs_by_status"] == {"previewed": 1}
    assert "missing_bin" in bad["__error__"]
    assert reprint["inventory_changed"] is False and reprint["label"]["status"] == "previewed"
    assert inst["status"] == "installed" and inst["qty_left_in_bin"] == 0
