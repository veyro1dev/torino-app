# PyInstaller spec: builds Torino.exe (windowed) + torino-cli.exe (console) into one folder.
# Build with:  .venv\Scripts\python packaging\build_release.py
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
HERE = SPECPATH  # noqa: F821 - provided by PyInstaller

datas = (
    collect_data_files("torino_parts")            # AI instructions + desktop/phone web pages
    + collect_data_files("googleapiclient")       # offline Sheets/Drive discovery documents
    + collect_data_files("webview")               # WebView2 interop DLLs
    + collect_data_files("barcode")
)
hidden = (
    collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
    + collect_submodules("mcp_types")
    + collect_submodules("uvicorn")
    + collect_submodules("torino_parts")
    + ["webview.platforms.edgechromium", "webview.platforms.winforms", "clr", "clr_loader",
       "win32com.client", "pythoncom", "win32print", "win32gui", "zxingcpp", "segno",
       "google_auth_oauthlib.flow", "googleapiclient.discovery"]
)
excludes = ["tkinter", "matplotlib", "pytest", "IPython"]


def analysis(script):
    return Analysis(  # noqa: F821
        [script], pathex=[HERE + "/../src"], datas=datas, hiddenimports=hidden, excludes=excludes,
        cipher=block_cipher, noarchive=False,
    )


a_app = analysis(HERE + "/torino_app.py")
a_cli = analysis(HERE + "/torino_cli.py")

pyz_app = PYZ(a_app.pure, cipher=block_cipher)  # noqa: F821
pyz_cli = PYZ(a_cli.pure, cipher=block_cipher)  # noqa: F821

exe_app = EXE(  # noqa: F821
    pyz_app, a_app.scripts, [], exclude_binaries=True, name="Torino", console=False,
    icon=HERE + "/torino.ico", upx=False,
)
exe_cli = EXE(  # noqa: F821
    pyz_cli, a_cli.scripts, [], exclude_binaries=True, name="torino-cli", console=True,
    icon=HERE + "/torino.ico", upx=False,
)
coll = COLLECT(  # noqa: F821
    exe_app, a_app.binaries, a_app.datas,
    exe_cli, a_cli.binaries, a_cli.datas,
    strip=False, upx=False, name="Torino",
)
