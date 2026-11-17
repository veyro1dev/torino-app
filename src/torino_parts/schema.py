"""Workbook layout. One tab per record type; first row holds column headers.

Columns are located by header name, so you may reorder or add your own columns
in the Google Sheet. Do not rename the listed headers.
"""

TABS: dict[str, list[str]] = {
    "Parts": [
        "part_id", "name", "status", "side_position", "manufacturer", "oem_number",
        "stamped_number", "fitment_scope", "category", "notes", "photo_links",
        "created_at", "updated_at",
    ],
    "Vehicles": [
        "vehicle_id", "nickname", "year", "make", "model", "trim", "engine", "vin",
        "notes", "created_at",
    ],
    "Fitment": [
        "fitment_id", "part_id", "make", "model", "year_from", "year_to", "trim",
        "engine", "verified", "source", "notes", "created_at",
    ],
    # kind: "bin" (a box/shelf/bag) or "work_area" (bench, car, paint booth): both are places in the shop
    "Bins": ["bin_code", "description", "location", "created_at", "kind"],
    "Stock": [
        "stock_id", "part_id", "bin_code", "condition", "qty", "donor_vehicle_id",
        "notes", "updated_at", "removed_from",
    ],
    "Installations": [
        "install_id", "part_id", "vehicle_id", "qty", "stock_id", "bin_code",
        "condition", "donor_vehicle_id", "installed_at", "notes", "request_id", "job_id",
    ],
    "Transactions": [
        "txn_id", "request_id", "request_hash", "timestamp", "type", "part_id",
        "stock_id", "bin_code", "qty_delta", "qty_after", "condition", "vehicle_id",
        "related_bin", "notes",
    ],
    # ---- rebuild workflow (schema v2; added to existing workbooks automatically) ----
    "Jobs": [
        "job_id", "vehicle_id", "system", "title", "goal", "kind", "status", "priority",
        "estimate_min", "notes", "hold_reason", "created_at", "updated_at", "started_at",
        "completed_at",
    ],
    "JobSteps": ["step_id", "job_id", "seq", "text", "done", "done_at"],
    "JobDeps": ["dep_id", "job_id", "depends_on", "note", "created_at"],
    "JobParts": [
        "need_id", "job_id", "part_id", "description", "qty_needed", "qty_installed",
        "check", "check_status", "notes", "created_at", "updated_at",
    ],
    "Photos": [
        "photo_id", "link", "vehicle_id", "job_id", "part_id", "caption", "stage", "source",
        "taken_at", "request_id",
    ],
    "Specs": [
        "spec_id", "vehicle_id", "job_id", "part_id", "item", "value", "unit", "source",
        "verified", "notes", "created_at",
    ],
    "Activity": [
        "act_id", "request_id", "request_hash", "timestamp", "type", "vehicle_id", "job_id",
        "part_id", "summary", "source",
    ],
    # ---- schema v3 ----
    # Parts that left the shop (machine shop, plater, upholsterer...). The stock row is reduced when a
    # part goes out and increased again on return, so "on hand" always means "in my shop".
    "Custody": [
        "custody_id", "part_id", "stock_id", "bin_code", "condition", "qty_out", "qty_returned", "holder",
        "reason", "vehicle_id", "job_id", "sent_at", "expected_back", "returned_at", "status", "notes",
        "created_at", "updated_at",
    ],
    # Shopping list per project (vehicle): considering -> ready -> ordered -> received (or cancelled)
    "Shopping": [
        "item_id", "vehicle_id", "status", "name", "part_id", "job_id", "need_id", "vendor", "link",
        "est_price", "qty", "qty_received", "category", "order_ref", "ordered_at", "received_at", "notes",
        "created_at", "updated_at",
    ],
    # Money: kind = expense | refund. amount is always positive; refunds subtract.
    "Expenses": [
        "expense_id", "vehicle_id", "date", "kind", "amount", "vendor", "category", "description",
        "job_id", "part_id", "item_id", "receipt_link", "notes", "created_at", "voided",
    ],
    "Budgets": ["budget_id", "vehicle_id", "category", "amount", "notes", "updated_at"],
    # Project notebook
    "Notes": [
        "note_id", "vehicle_id", "title", "body", "tags", "job_id", "part_id", "photo_id", "source",
        "created_at", "updated_at", "request_id",
    ],
}

SCHEMA_VERSION = 3

NUMERIC = {"qty", "year", "year_from", "year_to", "qty_delta", "qty_after", "priority", "estimate_min",
           "seq", "qty_needed", "qty_installed", "qty_out", "qty_returned", "est_price", "qty_received",
           "amount"}

SHOPPING_STATUSES = ["considering", "ready", "ordered", "received", "cancelled"]
EXPENSE_KINDS = ["expense", "refund"]
EXPENSE_CATEGORIES = ["parts", "machine shop", "paint & body", "tools", "supplies", "fluids", "hardware",
                      "upholstery", "shipping", "registration & title", "other"]
BIN_KINDS = ["bin", "work_area"]

# Part.status
IDENTIFIED = "identified"
UNIDENTIFIED = "unidentified"

# Part.fitment_scope
SCOPE_SPECIFIC = "specific"   # see Fitment rows
SCOPE_UNIVERSAL = "universal"  # fits anything of its kind
SCOPE_UNKNOWN = "unknown"      # fitment not known yet
SCOPES = {SCOPE_SPECIFIC, SCOPE_UNIVERSAL, SCOPE_UNKNOWN}

# Fitment.verified
VERIFIED = "verified"
UNVERIFIED = "unverified"

CONDITIONS = {"new", "used", "remanufactured", "core", "damaged"}
CONDITION_ALIASES = {
    "nos": "new", "new old stock": "new", "oem new": "new", "brand new": "new",
    "reman": "remanufactured", "rebuilt": "remanufactured", "refurbished": "remanufactured",
    "pre-owned": "used", "pulled": "used", "salvage": "used", "good used": "used",
    "broken": "damaged", "for parts": "damaged",
}

# Jobs
JOB_STATUSES = ["planned", "in_progress", "on_hold", "done", "cancelled"]
JOB_KINDS = ["assembly", "teardown", "repair", "inspection", "other"]
SYSTEMS = ["body", "brakes", "cooling", "drivetrain", "electrical", "engine", "exhaust", "fuel",
           "interior", "steering", "suspension", "trim", "wheels & tires", "other"]
# JobParts.check / check_status
CHECKS = ["none", "inspect", "verify_fitment"]
CHECK_STATUSES = ["pending", "passed", "failed"]
USABLE_CONDITIONS = {"new", "used", "remanufactured"}
