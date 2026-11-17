"""Build the Windows x64 release ZIP.

    .venv\\Scripts\\python packaging\\build_release.py

Produces release\\Torino-<version>-win-x64.zip containing a Torino\\ folder with Torino.exe,
torino-cli.exe and everything they need. Fails if anything that looks like a credential or
personal data ends up in the folder.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from torino_parts import __version__  # noqa: E402

import os  # noqa: E402

# TORINO_BUILD_DIR lets you build while a copy from build\dist is still running (its files are locked).
BUILD = Path(os.environ.get("TORINO_BUILD_DIR") or ROOT / "build")
DIST = BUILD / "dist"
WORK = BUILD / "work"
# Each version gets its own folder (release\v0.3.0\...) so older releases are never overwritten.
OUT = Path(os.environ.get("TORINO_RELEASE_DIR") or ROOT / "release" / f"v{__version__}")

FORBIDDEN_NAMES = re.compile(r"(client_secret|google-token|token\.json|devices\.json|torino-ca\.key|server\.key|"
                             r"config\.json|workbook\.json|labels\.db|run\.json|\.lbx$|torino\.db|"
                             r"^torino-(local|demo|google)-.*\.zip$)", re.I)


def main() -> None:
    if struct.calcsize("P") * 8 != 64:
        sys.exit("Build with 64-bit Python (b-PAC 64-bit and WebView2 x64 must match).")
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
                    "--workpath", str(WORK), str(ROOT / "packaging" / "torino.spec")], check=True)
    app = DIST / "Torino"
    for f in ("SETUP.md",):
        shutil.copyfile(ROOT / "packaging" / f, app / f)
    shutil.copyfile(ROOT / "src" / "torino_parts" / "AI_INSTRUCTIONS.md", app / "AI_INSTRUCTIONS.md")

    bad = [p for p in app.rglob("*") if FORBIDDEN_NAMES.search(p.name)]
    if bad:
        sys.exit(f"Refusing to package credential/personal files: {bad}")
    home_marker = str(Path.home()).lower().encode()
    for p in app.rglob("*.md"):
        if home_marker in p.read_bytes().lower():
            sys.exit(f"{p} contains this PC's home folder path")

    OUT.mkdir(parents=True, exist_ok=True)
    zpath = OUT / f"Torino-{__version__}-win-x64.zip"
    if zpath.exists() and not os.environ.get("TORINO_REBUILD"):
        sys.exit(f"{zpath} already exists; set TORINO_REBUILD=1 to replace this version's ZIP")
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, Path("Torino") / p.relative_to(app))
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (OUT / f"{zpath.name}.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
    size = zpath.stat().st_size / 1e6
    print(f"\nRelease: {zpath}  ({size:.1f} MB)\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
