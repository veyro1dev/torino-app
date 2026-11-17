"""Local stdio MCP server exposing the inventory tools."""

from __future__ import annotations

import functools
import json
import logging
import sys
from importlib import resources
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .app import build_inventory, describe, get_active_vehicle, resolve_vehicle, set_active_vehicle
from .config import load_config, source_key
from .inventory import SOURCE, InventoryError
from .rebuild import Garage
from .store.base import StoreUnavailable

log = logging.getLogger("torino_parts")

INSTRUCTIONS = resources.files("torino_parts").joinpath("AI_INSTRUCTIONS.md").read_text(encoding="utf-8")

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PRINT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

Condition = Literal["new", "used", "remanufactured", "core", "damaged"]
JobStatus = Literal["planned", "in_progress", "on_hold", "done", "cancelled"]
JobKind = Literal["assembly", "teardown", "repair", "inspection", "other"]
Check = Literal["none", "inspect", "verify_fitment"]


class FitmentEntry(BaseModel):
    make: str = Field(description="Vehicle make, e.g. Ford")
    model: str = Field("", description="Model, e.g. Torino; blank = all models of the make")
    year_from: int | None = Field(None, description="First model year (blank = open)")
    year_to: int | None = Field(None, description="Last model year (blank = same as year_from / open)")
    trim: str = ""
    engine: str = ""
    verified: bool = Field(False, description="True only if confirmed (catalog, tag, user); otherwise unverified")
    source: str = Field("", description="Where the fitment came from, e.g. 'user', 'parts tag'")
    notes: str = ""


class NewPart(BaseModel):
    name: str = Field("", description="Short part name, e.g. 'Door hinge, upper'. Optional when unidentified=true")
    side_position: str = Field("", description="e.g. 'left', 'right front', 'upper', 'driver'")
    manufacturer: str = ""
    oem_number: str = Field("", description="Only if supplied by the user or legibly printed")
    stamped_number: str = Field("", description="Only if legible in the photo or supplied by the user")
    category: str = Field("", description="e.g. body, electrical, engine, suspension")
    notes: str = ""
    fitment_scope: Literal["specific", "universal", "unknown"] | None = Field(
        None, description="specific (see fitment list), universal, or unknown. Default: specific if fitment given, else unknown")
    fitment: list[FitmentEntry] = Field(default_factory=list, description="Makes/models/years this part fits")
    unidentified: bool = Field(False, description="Store as an unidentified part with its own ID")
    allow_duplicate: bool = Field(False, description="Allow a new part even if the OEM number already exists")


class PartNeed(BaseModel):
    part_id: str = Field("", description="Inventory part ID if the part exists (search_parts first)")
    description: str = Field("", description="What is needed when there is no part record yet, e.g. 'Front rotor 11.3 in'")
    qty: int = Field(1, ge=1)
    check: Check = Field("none", description="'inspect' for used/removed parts, 'verify_fitment' when fit is uncertain")
    notes: str = ""


def _tool_errors(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except InventoryError as e:
            raise ToolError(json.dumps(e.to_dict(), default=str)) from e
        except StoreUnavailable as e:
            raise ToolError(json.dumps({"error": str(e), "needs_user_action": True})) from e
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", fn.__name__)
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return wrapper


def create_server(inv: Garage | None = None, source: str = "mcp") -> MCPServer:
    state: dict[str, Any] = {"inv": inv}

    def get_inv() -> Garage:
        SOURCE.set(source)
        if state["inv"] is not None and inv is None:
            # Same source as the desktop app and phone: if the user switched Demo/Local/Google there,
            # follow it instead of writing to the previous source.
            if getattr(state["inv"], "source_key", None) != source_key(load_config()):
                try:
                    state["inv"].labels.queue.close()
                except Exception:  # noqa: BLE001
                    pass
                state["inv"] = None
        if state["inv"] is None:
            state["inv"] = build_inventory()
            try:
                state["inv"].labels.startup()
            except Exception:  # noqa: BLE001
                log.exception("label queue startup failed")
        return state["inv"]

    from . import __version__

    mcp = MCPServer("torino-parts", instructions=INSTRUCTIONS, version=__version__)

    def tool(annotations):
        def deco(fn):
            return mcp.tool(annotations=annotations)(_tool_errors(fn))
        return deco

    # ------------------------------------------------------------------ read
    @tool(READ)
    def search_parts(query: str = "", vehicle_id: str | None = None, make: str | None = None,
                     model: str | None = None, year: int | None = None, bin_code: str | None = None,
                     include_out_of_stock: bool = False, limit: int = 20) -> dict:
        """Find parts by plain words ("left door hinges"), part/OEM/stamped number, and/or fitment.
        Returns each match with stock entries (qty, condition, bin), fitment and installations.
        Use before creating any new part record."""
        return get_inv().search_parts(query, make=make, model=model, year=year, vehicle_id=vehicle_id,
                                      bin_code=bin_code, include_out_of_stock=include_out_of_stock, limit=limit)

    @tool(READ)
    def get_part(part_id: str) -> dict:
        """Full record for one part: stock in every bin, fitment, photos, installations."""
        return get_inv().get_part(part_id)

    @tool(READ)
    def list_bin_contents(bin_code: str) -> dict:
        """Everything currently in one bin with quantity and condition."""
        return get_inv().list_bin(bin_code)

    @tool(READ)
    def list_bins() -> dict:
        """All bins with item counts."""
        return get_inv().list_bins()

    @tool(READ)
    def list_vehicles() -> dict:
        """All individual vehicles (IDs, nicknames, year/make/model/trim/engine/VIN)."""
        return get_inv().list_vehicles()

    @tool(READ)
    def get_vehicle(vehicle_id: str) -> dict:
        """One vehicle with parts installed on it, parts donated from it, and parts that fit it."""
        return get_inv().get_vehicle(vehicle_id)

    @tool(READ)
    def transaction_history(part_id: str | None = None, bin_code: str | None = None,
                            vehicle_id: str | None = None, request_id: str | None = None, limit: int = 30) -> dict:
        """Recent stock transactions (newest first), optionally filtered."""
        return get_inv().history(part_id, bin_code, vehicle_id, request_id, limit)

    @tool(READ)
    def inventory_info() -> dict:
        """Which data source this server is using - Demo (sample records), Local (database on this PC) or
        Google (Sheet + Drive) - plus the active vehicle and printer mode. Say so when it is Demo."""
        inv = get_inv()
        return describe(inv, load_config())

    # ----------------------------------------------------------------- write
    @tool(WRITE)
    def create_bin(request_id: str, bin_code: str, description: str = "", location: str = "",
                   kind: Literal["bin", "work_area"] = "bin") -> dict:
        """Create a storage bin (e.g. B-04) or a work area in the shop (kind=work_area, e.g. BENCH 'Work bench')."""
        return get_inv().create_bin(request_id, bin_code, description, location, kind)

    @tool(WRITE)
    def add_vehicle(request_id: str, year: int, make: str, model: str, nickname: str = "", trim: str = "",
                    engine: str = "", vin: str = "", notes: str = "") -> dict:
        """Add an individual vehicle (project car, customer car, or donor/parts car)."""
        return get_inv().add_vehicle(request_id, year, make, model, nickname, trim, engine, vin, notes)

    @tool(WRITE)
    def receive_stock(request_id: str, bin_code: str, qty: int = 1, condition: Condition = "used",
                      part_id: str | None = None, new_part: NewPart | None = None,
                      donor_vehicle_id: str | None = None, notes: str = "", photo_path: str | None = None,
                      photo_drive_ref: str | None = None, photo_base64: str | None = None,
                      print_label: bool = True, create_bin_if_missing: bool = False) -> dict:
        """INTAKE: save stock for an existing part (part_id) or a new part record (new_part), then
        request its label in the same call. The label is queued only after the stock is saved.
        Retrying with the same request_id never adds stock or labels twice."""
        return get_inv().receive_stock(
            request_id, bin_code, qty, condition, part_id=part_id,
            new_part=new_part.model_dump() if new_part else None, donor_vehicle_id=donor_vehicle_id,
            notes=notes, photo_path=photo_path, photo_base64=photo_base64, photo_drive_ref=photo_drive_ref,
            print_label=print_label, create_bin_if_missing=create_bin_if_missing)

    @tool(WRITE)
    def move_stock(request_id: str, part_id: str, from_bin: str, to_bin: str, qty: int = 1,
                   condition: Condition | None = None, stock_id: str | None = None, print_label: bool = False) -> dict:
        """Move quantity of a part between bins (keeps condition and donor). Optionally label the new bin."""
        return get_inv().move_stock(request_id, part_id, from_bin, to_bin, qty, condition, stock_id, print_label)

    @tool(WRITE)
    def adjust_count(request_id: str, reason: str, stock_id: str | None = None, part_id: str | None = None,
                     bin_code: str | None = None, condition: Condition | None = None,
                     new_qty: int | None = None, delta: int | None = None) -> dict:
        """Correct a count: set new_qty (what you counted) or apply delta. Reason is recorded."""
        return get_inv().adjust_count(request_id, reason, stock_id, part_id, bin_code, condition, new_qty, delta)

    @tool(WRITE)
    def record_installation(request_id: str, part_id: str, vehicle_id: str, qty: int = 1,
                            bin_code: str | None = None, condition: Condition | None = None,
                            stock_id: str | None = None, take_from_stock: bool = True, notes: str = "",
                            installed_on: str | None = None) -> dict:
        """Record that a part was installed on an individual vehicle (removes it from its bin)."""
        return get_inv().record_installation(request_id, part_id, vehicle_id, qty, bin_code, condition,
                                             stock_id, take_from_stock, notes, installed_on)

    @tool(WRITE)
    def add_fitment(request_id: str, part_id: str, make: str, model: str = "", year_from: int | None = None,
                    year_to: int | None = None, trim: str = "", engine: str = "", verified: bool = False,
                    source: str = "", notes: str = "") -> dict:
        """Add a make/model/year range a part fits (unverified unless verified=true)."""
        return get_inv().add_fitment(request_id, part_id, make, model, year_from, year_to, trim, engine,
                                     verified, source, notes)

    @tool(WRITE)
    def set_fitment_verified(request_id: str, fitment_id: str, verified: bool) -> dict:
        """Mark a fitment entry verified or unverified."""
        return get_inv().set_fitment_verified(request_id, fitment_id, verified)

    @tool(WRITE)
    def update_part(request_id: str, part_id: str, name: str | None = None, side_position: str | None = None,
                    manufacturer: str | None = None, oem_number: str | None = None,
                    stamped_number: str | None = None, category: str | None = None, notes: str | None = None,
                    fitment_scope: Literal["specific", "universal", "unknown"] | None = None,
                    identified: bool | None = None, photo_path: str | None = None,
                    photo_drive_ref: str | None = None, photo_base64: str | None = None) -> dict:
        """Edit a part record (e.g. identify a previously unidentified part, add a photo)."""
        return get_inv().update_part(request_id, part_id, identified=identified, photo_path=photo_path,
                                     photo_base64=photo_base64, photo_drive_ref=photo_drive_ref, name=name,
                                     side_position=side_position, manufacturer=manufacturer, oem_number=oem_number,
                                     stamped_number=stamped_number, category=category, notes=notes,
                                     fitment_scope=fitment_scope)

    # --------------------------------------------------------------- rebuild
    def vid(v):
        return resolve_vehicle(get_inv(), v)

    @tool(READ)
    def vehicle_overview(vehicle_id: str | None = None) -> dict:
        """Current state of a vehicle's rebuild: jobs by state, what to do next, blockers, recent activity.
        Defaults to the active vehicle shown in the desktop app."""
        return get_inv().vehicle_overview(vid(vehicle_id))

    @tool(READ)
    def what_next(vehicle_id: str | None = None, minutes: int | None = None) -> dict:
        """'What can I work on next?' Ready jobs (dependencies done, parts on hand) with pull lists, jobs that
        only need an inspection/fitment check, blocked jobs with the exact blocker and what clears it.
        Pass minutes for 'what can I do in N minutes with what I have' (uses the user's job estimates):
        then fits_in_time holds the jobs that fit and do_next the other ready jobs (each job listed once)."""
        return get_inv().what_next(vid(vehicle_id), minutes)

    @tool(READ)
    def list_jobs(vehicle_id: str | None = None, status: JobStatus | None = None, system: str | None = None,
                  include_closed: bool = False) -> dict:
        """Jobs for a vehicle (open jobs by default) with computed state, blockers and part counts."""
        return get_inv().list_jobs(vid(vehicle_id), status, system, include_closed)

    @tool(READ)
    def get_job(job_id: str) -> dict:
        """Everything about one job: goal, checklist, dependencies, parts needed (needed / on hand / installed,
        which bins to pull from, missing vs needs-inspection), blockers, notes, photos, specs, activity and
        where the user left off."""
        return get_inv().get_job(job_id)

    @tool(READ)
    def activity_log(vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                     limit: int = 30) -> dict:
        """Recent activity (newest first): installs, removals, notes, status changes, photos, specs."""
        return get_inv().activity_log(vehicle_id, job_id, part_id, limit)

    @tool(READ)
    def lookup_code(code: str) -> dict:
        """Resolve a scanned/typed code: part ID (P-00001), bin (A-01 or BIN:A-01), job (J-00001) or vehicle."""
        return get_inv().lookup_code(code)

    @tool(READ)
    def list_photos(vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                    limit: int = 30) -> dict:
        """Photos attached to a vehicle, job and/or part (links; newest first)."""
        return get_inv().list_photos(vehicle_id, job_id, part_id, limit)

    @tool(READ)
    def list_specs(vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                   query: str = "") -> dict:
        """Saved repair specifications (torque, clearances, capacities) with their sources."""
        return get_inv().list_specs(vehicle_id, job_id, part_id, query)

    @tool(WRITE)
    def set_active_vehicle(vehicle_id: str) -> dict:
        """Make this the vehicle the desktop app opens on and the default for rebuild questions."""
        inv = get_inv()
        v = inv.get_vehicle(vehicle_id)
        set_active_vehicle_cfg(v["vehicle_id"])
        return {"status": "active", "vehicle": v["label"]}

    @tool(WRITE)
    def create_job(request_id: str, title: str, vehicle_id: str | None = None, system: str = "other", goal: str = "",
                   kind: JobKind = "assembly", priority: int = 3, estimate_min: int | None = None,
                   steps: list[str] | None = None, depends_on: list[str] | None = None,
                   parts: list[PartNeed] | None = None, notes: str = "") -> dict:
        """Plan a rebuild job (system e.g. brakes, suspension, electrical, interior). priority 1=highest.
        estimate_min is the user's own time estimate - never invent one."""
        return get_inv().create_job(request_id, vid(vehicle_id), title, system, goal, kind, priority, estimate_min,
                                    steps, depends_on, [x.model_dump() for x in parts or []], notes)

    @tool(WRITE)
    def update_job(request_id: str, job_id: str, title: str | None = None, system: str | None = None,
                   goal: str | None = None, kind: JobKind | None = None, status: JobStatus | None = None,
                   priority: int | None = None, estimate_min: int | None = None, notes: str | None = None,
                   hold_reason: str | None = None, add_steps: list[str] | None = None,
                   steps_done: list[str] | None = None, steps_undone: list[str] | None = None,
                   add_depends_on: list[str] | None = None, remove_depends_on: list[str] | None = None,
                   add_parts: list[PartNeed] | None = None, note: str | None = None) -> dict:
        """Change a job: status (set 'done' only when the user says the work is finished), tick checklist
        steps (step IDs from get_job), add steps/dependencies/part needs, or log a note to the activity log."""
        return get_inv().update_job(request_id, job_id, title, system, goal, kind, status, priority, estimate_min,
                                    notes, hold_reason, add_steps, steps_done, steps_undone, add_depends_on,
                                    remove_depends_on, [x.model_dump() for x in add_parts] if add_parts else None, note)

    @tool(WRITE)
    def update_job_part(request_id: str, need_id: str, check_status: Literal["pending", "passed", "failed"] | None = None,
                        part_id: str | None = None, qty_needed: int | None = None, check: Check | None = None,
                        notes: str | None = None) -> dict:
        """Record an inspection or fitment result the USER reports (passed/failed), link an identified
        inventory part to a need, or change the quantity needed."""
        return get_inv().update_job_part(request_id, need_id, check_status, part_id, qty_needed, check, notes)

    @tool(WRITE)
    def record_job_installation(request_id: str, job_id: str, need_id: str | None = None, part_id: str | None = None,
                                qty: int = 1, bin_code: str | None = None, stock_id: str | None = None,
                                notes: str = "", take_from_stock: bool = True) -> dict:
        """The user reports they INSTALLED a part for a job. Deducts stock from its bin, adds the vehicle's
        installation history, updates the job's part need and logs it - together, once per request_id.
        If several stock entries could be used the tool lists them: ask which bin."""
        return get_inv().record_job_installation(request_id, job_id, need_id, part_id, qty, bin_code, stock_id,
                                                 None, notes, take_from_stock)

    @tool(WRITE)
    def record_removal(request_id: str, bin_code: str, vehicle_id: str | None = None, qty: int = 1,
                       condition: Condition = "used", part_id: str | None = None, new_part: NewPart | None = None,
                       removed_from: str = "", job_id: str | None = None, reinstall_job_id: str | None = None,
                       check: Check = "inspect", photo_path: str | None = None, photo_drive_ref: str | None = None,
                       photo_base64: str | None = None, print_label: bool = True, notes: str = "",
                       create_bin_if_missing: bool = False) -> dict:
        """TEARDOWN: the user removed a part from the vehicle. Stores it in a bin/bag (donor = this vehicle,
        removed_from = where it was), saves the in-place photo, links it to the job where it goes back
        (as a need to inspect) and requests the bag/bin label - in one call."""
        return get_inv().record_removal(request_id, vid(vehicle_id), bin_code, qty, condition, part_id,
                                        new_part.model_dump() if new_part else None, removed_from, job_id,
                                        reinstall_job_id, check, photo_path, photo_base64, photo_drive_ref,
                                        print_label, notes, create_bin_if_missing)

    @tool(WRITE)
    def attach_photo(request_id: str, vehicle_id: str | None = None, job_id: str | None = None,
                     part_id: str | None = None, caption: str = "", stage: str = "", photo_path: str | None = None,
                     photo_drive_ref: str | None = None, photo_base64: str | None = None) -> dict:
        """Attach a photo to a vehicle, job and/or part (stage e.g. 'before', 'in place', 'after')."""
        return get_inv().attach_photo(request_id, vehicle_id, job_id, part_id, caption, stage, photo_path,
                                      photo_base64, photo_drive_ref)

    @tool(WRITE)
    def record_spec(request_id: str, item: str, value: str, source: str, unit: str = "",
                    vehicle_id: str | None = None, job_id: str | None = None, part_id: str | None = None,
                    verified: bool = False, notes: str = "") -> dict:
        """Save a repair specification (torque, clearance, capacity, fitment fact) WITH its source, e.g.
        '1970 Ford Car Shop Manual Vol. 1, Front suspension specs'. verified=true only if the user checked it."""
        return get_inv().record_spec(request_id, item, value, source, unit, vehicle_id, job_id, part_id, verified, notes)

    @tool(PRINT)
    def print_bin_label(request_id: str, bin_code: str, copies: int = 1) -> dict:
        """Print a label for a bin or bag (barcode BIN:<code>, so a phone scan opens the bin)."""
        return get_inv().print_bin_label(request_id, bin_code, copies)

    # ---------------------------------------------------------------- labels
    @tool(PRINT)
    def print_label(request_id: str, part_id: str, bin_code: str | None = None, copies: int = 1) -> dict:
        """Print or reprint a label (part name, part ID, bin, barcode). Never changes inventory.
        Same request_id = same job (no double print)."""
        return get_inv().print_label(request_id, part_id, bin_code, copies)

    @tool(READ)
    def label_status(job_id: str | None = None, status: str | None = None, limit: int = 15) -> dict:
        """Printer status (online, tape, template) plus label jobs. Shows uncertain jobs needing a decision."""
        labels = get_inv().labels
        if job_id:
            return {"job": labels.view(labels.queue.get(job_id))}
        out = labels.status()
        out["recent_jobs"] = labels.jobs(status, limit)
        return out

    @tool(READ)
    def preview_label(part_id: str, bin_code: str | None = None) -> dict:
        """Show what a part's label will look like on the loaded tape (PNG path + the text lines). Never prints,
        never queues and never changes inventory - safe to call any time, no approval needed."""
        return get_inv().preview_label(part_id, bin_code)

    @tool(PRINT)
    def process_label_queue() -> dict:
        """Send all pending labels now (e.g. after the printer comes back online)."""
        return {"processed": get_inv().labels.process_pending()}

    @tool(PRINT)
    def resolve_label_job(job_id: str, action: Literal["reprint", "mark_printed", "cancel"]) -> dict:
        """Decide what to do with an uncertain/failed/pending label job."""
        return {"job": get_inv().labels.resolve(job_id, action)}

    # ------------------------------------------------------- progress & notebook
    @tool(READ)
    def resume(vehicle_id: str | None = None) -> dict:
        """'Where did I leave off?' The job last worked on, its next checklist step, the pull list (part, qty,
        bins), its blockers/checks, the last note, and the next ready jobs after it."""
        return get_inv().resume(vid(vehicle_id))

    @tool(READ)
    def progress(vehicle_id: str | None = None) -> dict:
        """Project progress: completed jobs, active work, blockers, parts away, totals and where you left off."""
        return get_inv().progress(vid(vehicle_id))

    @tool(READ)
    def notebook(vehicle_id: str | None = None, query: str = "", job_id: str | None = None,
                 part_id: str | None = None, tag: str | None = None, limit: int = 30) -> dict:
        """Search the project notebook (newest first) by words, job, part or tag. Notes link to jobs, parts, photos."""
        return get_inv().notebook(vid(vehicle_id), query, job_id, part_id, tag, limit=limit)

    @tool(WRITE)
    def add_note(request_id: str, body: str, vehicle_id: str | None = None, title: str = "", tags: str = "",
                 job_id: str | None = None, part_id: str | None = None, photo_id: str | None = None) -> dict:
        """Add a notebook entry the user reported (what they did, found, measured or decided). Link it to the job,
        part and/or photo it is about. Quote the returned note_id."""
        return get_inv().add_note(request_id, body, vid(vehicle_id) if not job_id else vehicle_id, title, tags,
                                  job_id, part_id, photo_id)

    # ----------------------------------------------------------- outside shops
    @tool(READ)
    def list_away(vehicle_id: str | None = None, part_id: str | None = None, include_returned: bool = False) -> dict:
        """Parts that are out of the shop (machine shop, plater...): who has them, why, since when, expected back,
        overdue, and which open jobs they block."""
        return get_inv().list_away(vehicle_id, part_id, include_returned)

    @tool(WRITE)
    def send_out(request_id: str, part_id: str, holder: str, qty: int = 1, reason: str = "",
                 from_bin: str | None = None, stock_id: str | None = None, condition: Condition | None = None,
                 expected_back: str = "", job_id: str | None = None, notes: str = "") -> dict:
        """The user took a part to an outside shop/person. Removes it from its bin and records who has it, why,
        when and the expected return date (YYYY-MM-DD). Location is tracked separately from condition/fitment."""
        return get_inv().send_out(request_id, part_id, holder, qty, reason, from_bin, stock_id, condition,
                                  expected_back, job_id, notes)

    @tool(WRITE)
    def return_from_shop(request_id: str, custody_id: str, qty: int | None = None, to_bin: str | None = None,
                         condition: Condition | None = None, notes: str = "", create_bin_if_missing: bool = False) -> dict:
        """Some or all of an away part came back (custody_id from list_away). Omit qty for 'all of it'. Puts it in
        to_bin (default: the bin it left from); set condition if it changed (e.g. remanufactured)."""
        return get_inv().return_from_shop(request_id, custody_id, qty, to_bin, condition, notes, create_bin_if_missing)

    # ---------------------------------------------------------------- shopping
    @tool(READ)
    def list_shopping(vehicle_id: str | None = None,
                      status: Literal["considering", "ready", "ordered", "received", "cancelled"] | None = None) -> dict:
        """The project's shopping list grouped by status with estimated totals."""
        return get_inv().list_shopping(vid(vehicle_id), status)

    @tool(WRITE)
    def add_shopping_item(request_id: str, name: str = "", vehicle_id: str | None = None, qty: int = 1,
                          status: Literal["considering", "ready", "ordered"] = "considering", vendor: str = "",
                          link: str = "", est_price: float | None = None, part_id: str | None = None,
                          job_id: str | None = None, need_id: str | None = None, category: str = "parts",
                          notes: str = "") -> dict:
        """Add something to buy for this project (est_price = estimated price EACH). Link the job / part need it is
        for. Torino never buys anything; checkout happens outside the app."""
        return get_inv().add_shopping_item(request_id, vid(vehicle_id), name, qty, status, vendor, link, est_price,
                                           part_id, job_id, need_id, category, notes)

    @tool(WRITE)
    def update_shopping_item(request_id: str, item_id: str,
                             status: Literal["considering", "ready", "ordered", "received", "cancelled"] | None = None,
                             name: str | None = None, qty: int | None = None, vendor: str | None = None,
                             link: str | None = None, est_price: float | None = None, order_ref: str | None = None,
                             notes: str | None = None, category: str | None = None, job_id: str | None = None,
                             part_id: str | None = None, paid_amount: float | None = None, paid_date: str | None = None,
                             allow_additional_payment: bool = False) -> dict:
        """Move an item along (ready -> ordered -> cancelled) or edit it. When the user says what they paid, pass
        paid_amount (total) once - it becomes one expense linked to the item; a second payment for the same item is
        refused unless allow_additional_payment=true. Deliveries go through receive_shopping_item."""
        return get_inv().update_shopping_item(request_id, item_id, status, name, qty, vendor, link, est_price, order_ref,
                                              notes, category, job_id, part_id, paid_amount, paid_date,
                                              allow_additional_payment)

    @tool(PRINT)
    def receive_shopping_item(request_id: str, item_id: str, bin_code: str, qty: int | None = None,
                              condition: Condition = "new", part_id: str | None = None, new_part: NewPart | None = None,
                              print_label: bool = True, create_bin_if_missing: bool = False, notes: str = "") -> dict:
        """A delivery arrived. Omit qty for 'everything still to come'; pass the delivered qty for a partial delivery.
        Saves stock in the bin (normal intake: transaction + label), links the part to the item and its job need,
        and marks the item received or partly received. Never records the payment again."""
        return get_inv().receive_shopping_item(request_id, item_id, bin_code, qty, condition, part_id,
                                               new_part.model_dump() if new_part else None, print_label,
                                               create_bin_if_missing, notes)

    # ------------------------------------------------------------------- money
    @tool(READ)
    def money_summary(vehicle_id: str | None = None, include_expenses: bool = True) -> dict:
        """Budget vs actual spending (expenses minus refunds), outstanding orders and possible purchases as three
        SEPARATE totals (never add them), plus spending by category, job and vendor."""
        return get_inv().money_summary(vid(vehicle_id), include_expenses=include_expenses)

    @tool(WRITE)
    def record_expense(request_id: str, amount: float, vehicle_id: str | None = None, vendor: str = "",
                       category: str = "parts", description: str = "", date: str | None = None,
                       kind: Literal["expense", "refund"] = "expense", job_id: str | None = None,
                       part_id: str | None = None, item_id: str | None = None, receipt_path: str | None = None,
                       receipt_base64: str | None = None, notes: str = "", allow_duplicate: bool = False) -> dict:
        """Record money the user REPORTS they spent (or got back: kind=refund, positive amount). date YYYY-MM-DD.
        Link the job/part/shopping item. A payment identical to an existing one is refused (not counted twice); only
        pass allow_duplicate=true if the user confirms it is a separate payment. Receipt: image or PDF."""
        return get_inv().record_expense(request_id, vid(vehicle_id) if not (item_id or job_id) else vehicle_id, amount,
                                        vendor, category, description, date, kind, job_id, part_id, item_id,
                                        receipt_path, receipt_base64, notes, allow_duplicate)

    @tool(WRITE)
    def void_expense(request_id: str, expense_id: str, reason: str) -> dict:
        """Mark an expense/refund the user says was entered by mistake (it stays on record with the reason but
        no longer counts). To correct an amount: void it, then record the right one."""
        return get_inv().void_expense(request_id, expense_id, reason)

    @tool(WRITE)
    def set_budget(request_id: str, amount: float, vehicle_id: str | None = None, category: str = "",
                   notes: str = "") -> dict:
        """Set the project budget (category empty) or a budget for one spending category."""
        return get_inv().set_budget(request_id, vid(vehicle_id), amount, category, notes)

    # ------------------------------------------------------------- local data
    @tool(READ)
    def data_backups() -> dict:
        """Which data source is active and the local backups available (restore is done by the user in the app)."""
        from .datasafety import list_backups
        cfg = load_config()
        return {"source": describe(get_inv(), cfg), "backups": [{k: b.get(k) for k in ("name", "created_at", "reason",
                                                                                       "records", "photo_files", "error")}
                                                                for b in list_backups()[:15]]}

    @tool(WRITE)
    def backup_local_data() -> dict:
        """Make a backup of the Local records and photos now (Local or Demo mode). Safe to call any time."""
        from .datasafety import backup_current
        get_inv()
        return backup_current("manual")

    return mcp


def set_active_vehicle_cfg(vehicle_id: str) -> None:
    set_active_vehicle(vehicle_id)


def main(source: str = "mcp") -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    create_server(source=source).run("stdio")


if __name__ == "__main__":
    main()
