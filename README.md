# Torino Garage — car rebuild planner + parts inventory (Windows)

A Windows desktop app for rebuilding cars (any make/model/year, any number of vehicles), with your
phone as a garage scanner, Google Sheets/Drive as the shared record, Brother PT-P700 labels, and
Codex / Claude Code connected through MCP to the same data and actions.

- **Torino.exe** — double-click to open. Starts on the active vehicle's rebuild page: jobs you can do
  next (with what to pull from which bin), jobs waiting on parts or earlier work (with the exact
  blocker and what clears it), and recent changes. Navigation: Rebuild · Vehicles · Rebuild Jobs ·
  Parts & Bins · Photos · Labels · Settings. A scan/search box, the workbook, printer and phone status
  are on every screen; demo data has a striped yellow banner.
- **Phone** — *Pair Phone* shows a QR code. The phone page scans Code 128 labels with the rear camera
  (repeat-guarded), shows qty/condition/bins on both devices, takes photos for the current part or job,
  and does scan-assisted moves (scan part → choose/scan bin → review qty → save).
- **Codex / Claude Code** — `torino-cli.exe serve` is a stdio MCP server with 38 tools (inventory +
  rebuild): *"What can I work on next?"*, *"What can I do in 30 minutes?"*, *"What's blocking the rotors?"*,
  *"Log that I installed the left door hinge"*.

**End users:** the release ZIP contains `SETUP.md` — install, Google, printer, phone and Codex in five steps.
This README is for building and developing.

**Moving between a desktop and a laptop:** see [the two-computer guide](docs/TWO_COMPUTERS.md)
for installing the same version, privately transferring records, and the current limits of
Google-backed sharing. GitHub synchronizes source code; records and device setup are separate.

`main` is the stable baseline; `develop` is for ongoing changes. Personal data, credentials,
local assistant worktrees and machine settings are excluded from the public repository.

---

## Repository layout

| Path | What |
|---|---|
| `src/torino_parts/inventory.py` | Parts, bins, stock, fitment, installs, idempotent atomic writes (`_mutate`) |
| `src/torino_parts/rebuild.py` | `Garage`: jobs, checklists, dependencies, part needs, what-next, teardown, photos, specs, activity |
| `src/torino_parts/projects.py` | Outside-shop custody, shopping list, notebook, progress/resume, money (mixed into `Garage`) |
| `src/torino_parts/datasafety.py` | Local backups/restore (records + photos) and Local ↔ Google transfer with preview |
| `src/torino_parts/store/` | SQLite (**Local**), JSON workbook (**Demo**) and Google Sheets (**Google**) stores, same API |
| `src/torino_parts/labels/` | Code 128 preview rendering, durable SQLite print queue, b-PAC 3.4 backend |
| `src/torino_parts/server.py` | MCP server (stdio) + `AI_INSTRUCTIONS.md` |
| `src/torino_parts/web/` | Local web server: desktop UI (127.0.0.1), phone pages (LAN HTTPS), CA download page; TLS + pairing |
| `src/torino_parts/desktop.py` | `Torino.exe` launcher (WebView2 window via pywebview, single instance) |
| `src/torino_parts/sample_data.py`, `demo.py` | Sample garage (DEMO) and `torino demo` |
| `packaging/` | PyInstaller spec, release build + release verification scripts, `SETUP.md` |
| `tests/` | Automated tests (local store **and** a Google Sheets API fake, MCP over stdio, web server, TLS, real b-PAC when installed) |

## Develop

```powershell
cd torino            # the folder you cloned
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]" pyinstaller
.venv\Scripts\python -m pytest -q
.venv\Scripts\torino demo                       # resets DEMO data, runs the scripted walkthrough (6 checks)
.venv\Scripts\torino app                        # the desktop app from source (same as Torino.exe)
.venv\Scripts\torino serve --source codex       # MCP stdio server from source
```

Data, settings, Google token, TLS keys, paired devices and the label queue live in
`%APPDATA%\TorinoInventory` (override with `TORINO_HOME`), never in the repo or the release.
Demo data (`...\demo\`) and Google data (`...\google\`) are kept separate.

## Build and verify the release

```powershell
.venv\Scripts\python packaging\build_release.py
# -> release\v0.3.2\Torino-0.3.2-win-x64.zip (+ .sha256); TORINO_BUILD_DIR=<folder> builds elsewhere if build\dist is in use
Expand-Archive release\v0.3.2\Torino-0.3.2-win-x64.zip $env:TEMP\torino-rel -Force
.venv\Scripts\python packaging\verify_release.py $env:TEMP\torino-rel\Torino
```

`build_release.py` refuses to package anything that looks like a credential or personal data
(client secrets, tokens, CA/server keys, devices.json, config.json, workbook.json, labels.db).
`verify_release.py` exercises the extracted EXEs with a throwaway home folder: CLI + demo, MCP
(inventory + rebuild tools, install recorded once), the desktop API seeing that install, phone pairing
over trusted HTTPS, a camera frame decoded to a part, the scan showing on the desktop, restart
persistence of activity and label queue, and the native window opening and closing.

Install for daily use: extract to `%LOCALAPPDATA%\Programs` (gives `...\Programs\Torino\Torino.exe`), then

```powershell
codex mcp add torino-parts -- "$env:LOCALAPPDATA\Programs\Torino\torino-cli.exe" serve --source codex
claude mcp add torino-parts --scope user -- "$env:LOCALAPPDATA\Programs\Torino\torino-cli.exe" serve --source claude
```

## How the data stays consistent

* **One implementation.** Desktop, phone, CLI and MCP all call the same `Garage` object.
* **Atomic.** A job installation writes the stock change, the Installations row, the Transactions row,
  the job's part need and the Activity row in one commit (one Sheets `batchUpdate`).
* **Idempotent.** Every write carries a `request_id`; a repeat returns the original result
  (`duplicate_request: true`) and never deducts stock or prints twice. The desktop and phone create the
  id when a form opens, so pressing Save again after an error is a safe retry.
* **Upgrades.** Opening an older workbook adds the new tabs (Jobs, JobSteps, JobDeps, JobParts, Photos,
  Specs, Activity) and missing columns (`Stock.removed_from`, `Installations.job_id`); existing rows,
  IDs and your own extra columns are untouched. Columns are found by header name.
* **Google quota.** Reads are cached for 8 s per process; writes always re-read under a lock.
  If Google needs a new sign-in, the app shows a red banner and Settings → Reconnect Google.

## Rebuild model

* **Job** — vehicle, system (brakes, suspension, electrical, interior…), kind (assembly, teardown,
  repair, inspection), goal, priority, your time estimate, status (planned, in progress, on hold, done,
  cancelled), notes, checklist steps, dependencies, part needs, photos, specs, activity.
* **Computed state** — *ready*, *ready_with_checks* (parts on hand but to inspect/verify fitment),
  *blocked* (earlier job not done, part missing/short, no part record, failed check), *on hold*.
  Each blocker has a "to clear" action. *What next* ranks in-progress first, then priority, then jobs
  that unblock others; *minutes* uses remaining estimate = estimate × unticked steps / steps.
* **Part needs** — needed / on hand (usable conditions only) / installed, pull-from bins, fitment for this
  vehicle (verified, unverified, unknown, not listed — parts that came off this vehicle count as verified),
  and contention when other open jobs need the same stock.
* **Teardown** — `record_removal`: stores the part (donor = this vehicle, `removed_from`), the in-place
  photo, a reinstall need (marked *inspect*) on the job where it goes back, and the bag/bin label.
* **Specs** — torque/clearance/capacity values must carry a `source`.

## Phone security

The phone page is HTTPS with a private CA created on first run (`%APPDATA%\TorinoInventory\tls`).
The CA is **name-constrained** to private network ranges and `.local`, so it cannot vouch for public
websites. Pairing codes are single-use, expire in 10 minutes, and are exchanged for a device cookie
(HttpOnly, Secure, SameSite=Strict; only its hash is stored); devices can be unpaired in Settings.
The plain-HTTP setup port serves only the public CA certificate. The desktop UI listens on 127.0.0.1
only, needs a per-session token, and rejects non-loopback Host headers.

## Printing (Brother PT-P700)

Configurable in the Labels page: preview vs live (b-PAC 3.4, 64-bit), Windows
queue, `.lbx` template with objects `PartName`, `PartID`, `BinCode`, optional `Barcode`, tape width,
auto-print after intake. `labels/lbx.py` generates the template for any PT-P700 tape width (media IDs and
printable dots from the driver's media table), so P-touch Editor is optional: *Create template for this
tape* in the Labels page, or `torino printer make-template --tape 24`. b-PAC is driven through pywin32 late
binding, where zero-argument members (`EndPrint`, `Close`, `GetMediaName`) run on attribute access —
always go through `com_call0`; the test fake reproduces this. Jobs are durable (SQLite); offline → *pending*; possibly-printed → *uncertain*
(never resent without your decision). Bin labels encode `BIN:<code>` so a scan opens the bin.

## Verification on a new computer

Run the automated tests in the environment installed on that computer. The tests use isolated
sample records and a Google API fake; passing them does not verify a live Google account or hardware.
Earlier release checks were performed on the original development computer and do not establish
that a copied installation is configured correctly on another computer.

Verify Google sign-in and the intended workbook/Photos folder, one physical PT-P700 label
(driver, b-PAC 64-bit, template), a phone scan on that computer's Wi-Fi connection
(certificate installation and pairing), and MCP registration with that computer's executable path.
