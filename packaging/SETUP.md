# Torino — setup guide (Windows x64)

## 1. Install and open
1. Right-click the ZIP → **Properties** → tick **Unblock** → OK (or in PowerShell:
   `Unblock-File "$env:USERPROFILE\Downloads\Torino-0.3.2-win-x64.zip"`).
2. Extract it so you get `%LOCALAPPDATA%\Programs\Torino\Torino.exe`:
   ```powershell
   Expand-Archive "$env:USERPROFILE\Downloads\Torino-0.3.2-win-x64.zip" "$env:LOCALAPPDATA\Programs" -Force
   ```
3. Double-click **`%LOCALAPPDATA%\Programs\Torino\Torino.exe`** (right-click → *Pin to Start* or *Send to → Desktop*
   for a shortcut). The app is not code-signed: if Windows shows *"Windows protected your PC"*, click
   **More info → Run anyway**.
4. The first time, Windows Firewall asks whether Torino may use networks: allow **Private networks** only
   (needed for the phone). If you missed it, run once in an *Administrator* PowerShell:
   `netsh advfirewall firewall add rule name="Torino phone" dir=in action=allow protocol=TCP localport=8766,8767 profile=private`

Torino opens on **DEMO data** (yellow striped banner). Your settings, sign-in and data live in
`%APPDATA%\TorinoInventory` — never in the program folder, so updating = extract the new ZIP over the old one.
Updating keeps everything: older data and Google workbooks are upgraded in place (new tables/columns are added,
nothing is removed or renumbered).

## 2. Choose where your real records live
**Settings → Data source**:
- **Local — this PC** (recommended to start): a database on this PC. Works offline, no Google account, free.
  Torino makes one automatic backup per day; **Settings → Backups → Back up now** makes one any time (every record
  plus every photo and receipt). Put the backup folder on OneDrive or a USB stick with **Change folder…**.
  **Restore…** shows what is in a backup before replacing anything, and first saves a backup of what is there now.
- **Google — Sheet + Drive** (optional, step 2b). **Settings → Move data between Local and Google** copies everything
  (same IDs, photos copied) into an empty target after showing a preview; the source is never changed.
- **Demo**: sample records only, never mixed with yours.
The chip at the top always shows which one is in use (DEMO / LOCAL / GOOGLE); the phone and Codex use the same one.

## 2b. Google Drive (optional)
1. <https://console.cloud.google.com/> → create a project → **APIs & Services → Library** → enable
   **Google Sheets API** and **Google Drive API**.
2. **Google Auth Platform → Branding**: app name + your email. **Audience**: External, add your Google account as a
   test user, then **Publish app** (otherwise sign-in expires every 7 days; Torino only uses the non-sensitive
   `drive.file` scope).
3. **Clients → Create client → Desktop app** → **Download JSON**.
4. In Torino: **Settings → Google Drive** → choose that JSON → **Connect Google** → sign in in the browser.
   Torino creates a private Drive folder *Torino Parts Inventory* with the workbook and a *Photos* folder
   (or upgrades an existing Torino workbook, keeping all IDs and columns).
5. **Settings → Data source → Use Google**, or first **Local → Google…** to copy your Local records over.
If Google later needs you to sign in again, a red banner appears; use **Settings → Reconnect Google** — or switch to
Local to keep working offline. Torino never needs a paid Google service, billing or an AI API key.

## 3. Brother PT-P700 (USB)
1. From <https://support.brother.com> → PT-P700 → Downloads (Windows): install the **Printer Driver**. Install the
   **b-PAC 3.4 Client Component, 64-bit** from Brother's b-PAC developer page. (P-touch Editor is *not* needed.)
2. Printer on + USB: hold the **Editor Lite** button until its green lamp goes **off**. (With the lamp on, the
   printer is only a USB drive and Windows creates no printer for it.)
3. Check the queue name: `Get-Printer | Where-Object Name -like '*PT-P700*'` (usually `Brother PT-P700`).
4. Torino → **Labels** → pick the **tape width you have loaded** → **Create template for this tape**. Torino writes
   `Documents\Labels\torino-<width>mm.lbx` (objects `PartName`, `PartID`, `BinCode`, and on 12 mm or wider a
   Code 128 `Barcode`), checks it with b-PAC and selects it. Or: `torino-cli.exe printer make-template --tape 24`.
   You can still use your own P-touch Editor template with those object names.
5. Torino → **Labels** → Mode *Live*, queue → **Save** → **Print test label**
   (prints exactly one). Turn **Auto-print after intake** on when happy.
   Uncertain jobs (printer may have printed) are shown in red; choose *Print again*, *It printed* or *Cancel*.

## 4. Phone scanner (same home Wi-Fi)
1. Torino → **Pair Phone**. Scan QR **1** with the phone camera (first time only) and install the certificate:
   - iPhone: Allow → Settings → *Profile Downloaded* → Install → Settings → General → About →
     *Certificate Trust Settings* → turn on *Torino Garage local CA*.
   - Android: Settings → Security → *Encryption & credentials* → *Install a certificate* → *CA certificate*.
2. Scan QR **2** to pair (single-use, 10 minutes). Bookmark the page or *Add to Home Screen*.
3. Point the rear camera at a part label: the record appears on the phone and the laptop. Use *Type code* for
   damaged labels, *Photo* to attach pictures, *Move* to scan a part then its new bin.
Unpair phones in **Settings → Phone**.

## 5. Codex and Claude Code
Run once in PowerShell (paths assume step 1's install folder):
```powershell
codex mcp add torino-parts -- "$env:LOCALAPPDATA\Programs\Torino\torino-cli.exe" serve --source codex
claude mcp add torino-parts --scope user -- "$env:LOCALAPPDATA\Programs\Torino\torino-cli.exe" serve --source claude
```
Then ask Codex: *"What can I work on next on the Torino?"* or *"Log that I installed the left door hinge."*
The guidance Codex follows is in `AI_INSTRUCTIONS.md` (sent automatically by the server).

## 6. Where things are
- **Rebuild** — where you left off (next step + what to pull from which bin), what to do next, a time filter
  (*30 min*: each matching job once), and what blocks the rest (missing, on order, at an outside shop, earlier work).
- **Progress** — done / active / blocked work and parts away. **Notebook** — searchable notes linked to jobs,
  parts and photos.
- **Parts & Bins** — a part's page shows where it is in the shop (bin or work area) and anything *away* (who has
  it, why, since when, expected back); **Send out…** / **Record return…** handle partial returns.
- **Shopping** — Considering → Ready to buy → Ordered → Received. **Receive…** puts a delivery (or part of one) into a
  bin with the normal label. Buying happens outside Torino.
- **Money** — budget, spending, refunds and receipts; actual spending, outstanding orders and possible purchases are
  shown as three separate totals. A payment is recorded once: receiving an order never counts it again. Graphs show
  actual spending over time against the budget and spending by category (orders and plans are never in the line).
  **Void…** fixes a mistaken entry (kept on record, no longer counted); **Save as spreadsheet (CSV)** writes
  `Documents\Torino\<vehicle>-money-<date>.csv`.
- **Labels → Preview a label** (or ask Codex for `preview_label`) shows exactly what will print, without printing.
- **Settings → Setup status** — Google, printer and phone, each with its one next step.

## Command line
`torino-cli.exe --help` — e.g. `torino-cli.exe demo`, `torino-cli.exe search "left door hinge"`,
`torino-cli.exe printer test --live`, `torino-cli.exe setup-google --client-secret <json>`.
