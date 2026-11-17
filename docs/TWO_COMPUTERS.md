# Use Torino on a desktop and a laptop

Torino currently targets Windows x64. Install the same code version on both computers,
then choose how to move or share your private garage records. GitHub holds the program
source, tests, documentation and bundled UI assets. Your records stay private.

## Install from GitHub

On each Windows computer, install Git and 64-bit Python 3.11 or newer, then run in PowerShell:

```powershell
git clone https://github.com/veyro1dev/torino-app.git
cd torino-app
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\torino app
```

Create a fresh `.venv` on each computer. Copying a virtual environment can leave launchers
and package paths pointing to the old machine. Use the same Python version on both machines
when diagnosing differences. The desktop window also needs Microsoft Edge WebView2 Runtime;
see [the setup guide](../packaging/SETUP.md) for the printer and phone prerequisites.

For a standalone app, install PyInstaller in that environment and build the complete release:

```powershell
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python packaging\build_release.py
```

Transfer and extract the entire release ZIP. `Torino.exe` depends on the files alongside it;
copying only the executable loses required libraries and UI assets.

## Move your records privately first

The default data folder is `%APPDATA%\TorinoInventory`, or the folder specified by `TORINO_HOME`.
Copying or cloning the source code does not bring this folder with it. A fresh installation
starts in Demo mode, which can look like missing records or missing functionality.

For Local records:

1. On the computer with the complete records, choose **Local** as the data source and open
   **Settings > Backups > Back up now**. Keep an additional private copy of this backup.
2. Transfer the backup ZIP privately using a USB drive or your own private storage.
   It contains garage records, photos and receipts. Keep it outside this public repository.
3. On the laptop, open **Settings > Backups > Restore**, select the ZIP, inspect its counts,
   then confirm. Restore writes the records into Local and relocates the photo links.
4. Select **Local** on the laptop and verify a vehicle, a job, a part, a photo and a receipt.

Backups move records and photos. Configure the printer, Google sign-in, phone pairing and
Codex/Claude MCP connection separately on each computer. Follow the setup guide for each.

## Choose how records stay consistent

| Method | What it does | Current limitation |
| --- | --- | --- |
| Local + private backups | Works offline; restore a backup when changing computers | No automatic merging or continuous sync; use one authoritative copy |
| Google Sheet + Drive | Both computers can use the same private workbook and photos | Internet required; use one writing computer at a time |
| GitHub | Shares program changes and version history | Does not sync garage records, photos, settings or installed releases |

Do not put the active Local SQLite database in a cloud-synced folder or point both computers
at copies of it expecting changes to merge. You may sync completed backup ZIPs privately,
but restoring is a deliberate replacement, not a merge. Back up both computers first if
each already contains unique changes.

The Google store uses a lock file on each computer. It does not coordinate writes across
computers: two simultaneous edits can overwrite each other, even though each individual
Google batch is atomic. Until shared locking or conflict handling is added, finish edits
and close Torino and its MCP sessions on one computer before editing on the other.
Read caches may take about eight seconds to refresh. This is shared online storage,
not offline bidirectional sync.

## Connect both computers to the same Google records

Do this only after the Local backup is verified. Set up Google on the source computer using
the setup guide, then use **Settings > Move data between Local and Google** if your real
records are currently Local. Inspect the transfer preview before confirming.

On the second computer, use the same Google account and Desktop OAuth client. Before
connecting, privately obtain the existing `google.spreadsheet_id`, `google.folder_id` and
`google.photos_folder_id` from the source computer's settings. Set those same values on
the second computer using its local CLI:

```powershell
.venv\Scripts\torino config set google.spreadsheet_id "<existing workbook ID>"
.venv\Scripts\torino config set google.folder_id "<existing parent folder ID>"
.venv\Scripts\torino config set google.photos_folder_id "<existing photos folder ID>"
```

Then connect Google in Settings with the same OAuth client JSON and sign in locally.
Do not commit that JSON or copy sign-in tokens through Git. The setup code creates new
resources when the configured IDs are absent or inaccessible. If it reports creating a
new workbook, stop before entering or transferring records and check account, client and
IDs. Verify that both computers show the same workbook URL and existing records.
Connecting Google independently with blank IDs creates separate inventories.

## Keep program changes in sync

`main` is the stable baseline. `develop` holds ongoing work and starts at the same version.
Merge tested changes from `develop` into `main` before updating daily-use installations.

Before working on either machine:

```powershell
git switch develop
git pull --ff-only
```

After a change, review the exact files you will publish and commit only intended source files:

```powershell
git status --short
git diff
git add path/to/changed-source.py
git diff --cached
git commit -m "Describe the change"
git push
```

On the other computer, pull the same branch and reinstall the source package:

```powershell
git pull --ff-only
.venv\Scripts\python -m pip install -e ".[dev]"
```

For stable use, switch to `main` and pull there instead. Source pulls do not update a
previously built EXE; rebuild or install the matching complete release. Avoid editing the
same files on both computers before pushing and pulling.

Use a GitHub no-reply email for commits if you want to keep your email private. The initial
public snapshot uses that identity and excludes the previous local history. Older local
branches remain private; never push all branches or merge those old branches into the
public branches. Port any desired changes as reviewed patches instead.

## Check missing functionality on a new computer

- **Empty or sample records:** check the Demo / Local / Google chip and selected workbook.
- **Different screens or commands:** compare the branch and commit, reinstall the source
  package, and check whether a shortcut launches an older EXE.
- **Photos missing:** restore the full backup, or connect to the same Google photos folder.
- **Labels preview but do not print:** configure that computer's driver, b-PAC 64-bit,
  live printing mode, template and physical printer connection. A USB printer attached to
  the desktop is not automatically available to the laptop.
- **Phone cannot connect:** pair it with the computer currently running Torino and follow
  that computer's certificate, private-network and firewall setup.
- **Codex/Claude has fewer tools or different data:** register that computer's current CLI
  path and check the selected source. MCP registration is local to each machine.

The repository establishes a common code baseline. Actual laptop hardware, Google access,
phone camera and physical printing still need verification on that computer.
