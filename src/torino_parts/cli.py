"""`torino` command line."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .config import config_path, home_dir, load_config, save_config, set_value


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _inv():
    from .app import build_inventory

    inv = build_inventory()
    inv.labels.startup()
    return inv


def cmd_serve(args) -> None:
    from .server import main

    main(source=args.source)


def cmd_app(args) -> None:
    from .desktop import main as desktop_main

    desktop_main((["--headless"] if args.headless else []) + (["--local-only"] if args.local_only else [])
                 + (["--print-url"] if args.print_url else []))


def cmd_demo(args) -> None:
    from .demo import run_demo

    ok = run_demo(out_dir=Path(args.out) if args.out else None)
    if not ok:
        sys.exit(1)


def cmd_setup_google(args) -> None:
    from .google_setup import setup_google

    setup_google(args.client_secret, args.name)


def cmd_use(args) -> None:
    cfg = load_config()
    if args.backend == "google" and not cfg["google"].get("spreadsheet_id"):
        sys.exit("No Google workbook yet: run `torino setup-google --client-secret <file>` first.")
    cfg["backend"] = args.backend
    save_config(cfg)
    print(f"Backend set to {args.backend}")


def cmd_data(args) -> None:
    from .datasafety import DataSafetyError, backup_current, list_backups, restore_into_local, transfer

    try:
        if args.action == "backup":
            _print(backup_current("manual"))
        elif args.action == "backups":
            _print(list_backups())
        elif args.action == "restore":
            if not args.target:
                sys.exit("data restore <backup.zip> [--yes]")
            _print(restore_into_local(args.target, confirm=args.yes))
        elif args.action == "transfer":
            if not args.target:
                sys.exit("data transfer local_to_google|google_to_local [--yes]")
            _print(transfer(args.target, confirm=args.yes, replace_local=args.replace_local, log=print))
    except DataSafetyError as e:
        sys.exit(str(e))


def cmd_config(args) -> None:
    cfg = load_config()
    if args.action == "show":
        print(f"# {config_path()}")
        _print(cfg)
    elif args.action == "set":
        try:
            set_value(cfg, args.key, args.value)
        except (KeyError, ValueError) as e:
            sys.exit(str(e))
        save_config(cfg)
        print(f"{args.key} = {args.value}")


def cmd_search(args) -> None:
    _print(_inv().search_parts(args.query, make=args.make, model=args.model, year=args.year,
                               vehicle_id=args.vehicle, include_out_of_stock=args.all))


def cmd_bin(args) -> None:
    inv = _inv()
    _print(inv.list_bin(args.code) if args.code else inv.list_bins())


def cmd_printer(args) -> None:
    cfg = load_config()
    if args.action == "status":
        _print(_inv().labels.status())
        return
    if args.action == "configure":
        p = cfg["printer"]
        if args.queue:
            p["queue"] = args.queue
        if args.template:
            t = Path(args.template).expanduser().resolve()
            if not t.is_file():
                sys.exit(f"template not found: {t}")
            p["template"] = str(t)
        if args.tape:
            set_value(cfg, "printer.tape_mm", args.tape)
        if args.mode:
            set_value(cfg, "printer.mode", args.mode)
        if args.auto_print:
            set_value(cfg, "printer.auto_print", args.auto_print)
        save_config(cfg)
        _print(cfg["printer"])
        return
    if args.action == "make-template":
        from .labels.lbx import check_with_bpac, default_template_path, write_template

        tape = float(args.tape or cfg["printer"]["tape_mm"])
        out = Path(args.out).expanduser().resolve() if args.out else default_template_path(tape)
        queue = args.queue or cfg["printer"]["queue"]
        write_template(out, tape, printer_name=queue)
        result: dict = {"template": str(out), "tape_mm": int(tape) if tape.is_integer() else tape}
        try:
            chk = check_with_bpac(out)
            result["bpac_check"] = chk
            if not chk.get("ok"):
                sys.exit(f"b-PAC rejected the template: {chk}")
        except ImportError:
            result["bpac_check"] = "skipped (pywin32 not available)"
        except Exception as e:  # noqa: BLE001 - b-PAC not installed: template is still written
            result["bpac_check"] = f"skipped: {e}"
        set_value(cfg, "printer.tape_mm", tape)
        cfg["printer"]["template"] = str(out)
        if args.queue:
            cfg["printer"]["queue"] = args.queue
        save_config(cfg)
        result["printer_config"] = cfg["printer"]
        _print(result)
        return
    if args.action == "test":
        from .labels import LabelQueue, LabelService
        from .labels.printers import BpacBackend, PreviewBackend
        from .config import data_dir

        pcfg = dict(cfg["printer"])
        live = args.live or (pcfg["mode"] == "live" and not args.preview)
        backend = BpacBackend(pcfg) if live else PreviewBackend(pcfg)
        ddir = data_dir(cfg)
        svc = LabelService(LabelQueue(ddir / "labels.db"), pcfg, ddir / "label-previews", backend=backend)
        svc.queue.recover()
        rid = args.request_id or f"test-{datetime.now():%Y%m%d-%H%M%S}"
        if live:
            st = backend.status()
            print("Printer check:")
            _print(st)
        job = svc.request("test", rid, "P-TEST", args.name, args.bin, copies=1, send=True)
        if job["status"] in ("failed", "pending"):
            # definitely not printed: don't leave a test job lying around in the queue
            svc.resolve(job["job_id"], "cancel")
        _print(job)
        if live and job["status"] != "printed":
            print("Test label NOT printed: see last_error / printer check above.", file=sys.stderr)
            sys.exit(1)


def cmd_labels(args) -> None:
    inv = _inv()
    if args.action == "list":
        _print(inv.labels.jobs(args.status, args.limit))
    elif args.action == "process":
        _print(inv.labels.process_pending())
    elif args.action == "resolve":
        _print(inv.labels.resolve(args.job_id, args.resolution))
    elif args.action == "print":
        rid = args.request_id or f"cli-reprint-{datetime.now():%Y%m%d-%H%M%S}"
        _print(inv.print_label(rid, args.part_id, args.bin, args.copies))


def cmd_mcp_config(args) -> None:
    exe = shutil.which("torino") or str(Path(sys.executable).with_name("torino.exe"))
    exe = str(Path(exe).resolve())
    esc = exe.replace("\\", "\\\\")
    print("# Claude Code (run in PowerShell):")
    print(f'claude mcp add torino-parts --scope user -- "{exe}" serve')
    print()
    print("# Codex / ChatGPT desktop local Codex (run in PowerShell):")
    print(f'codex mcp add torino-parts -- "{exe}" serve')
    print()
    print("# ...or add this to %USERPROFILE%\\.codex\\config.toml :")
    print("[mcp_servers.torino-parts]")
    print(f'command = "{esc}"')
    print('args = ["serve"]')
    print("startup_timeout_sec = 30")
    print("tool_timeout_sec = 120")


def cmd_instructions(args) -> None:
    from importlib import resources

    print(resources.files("torino_parts").joinpath("AI_INSTRUCTIONS.md").read_text(encoding="utf-8"))


def cmd_info(args) -> None:
    from .app import describe

    cfg = load_config()
    _print({"config_file": str(config_path()), **describe(_inv(), cfg)})


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="torino", description="Automotive parts inventory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the MCP server on stdio")
    p.add_argument("--source", default="mcp", help="name recorded in the activity log, e.g. codex or claude")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("app", help="open the Torino desktop app (same as Torino.exe)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--local-only", action="store_true", help="don't listen on the home network (no phone)")
    p.add_argument("--print-url", action="store_true", help="print the desktop URL with its session token")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("demo", help="reset the local demo data and run the end-to-end demo")
    p.add_argument("--out", help="folder for the demo report and label previews")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("setup-google", help="Google sign-in; create the workbook and private photo folder")
    p.add_argument("--client-secret", help="OAuth desktop client JSON downloaded from Google Cloud")
    p.add_argument("--name", help="workbook/folder name (default: Torino Parts Inventory)")
    p.set_defaults(fn=cmd_setup_google)

    p = sub.add_parser("use", help="choose the data source: demo (sample), local (this PC), google")
    p.add_argument("backend", choices=["demo", "local", "google"])
    p.set_defaults(fn=cmd_use)

    p = sub.add_parser("data", help="Local data: backup / backups / restore / transfer")
    p.add_argument("action", choices=["backup", "backups", "restore", "transfer"])
    p.add_argument("target", nargs="?", help="restore: backup .zip; transfer: local_to_google | google_to_local")
    p.add_argument("--yes", action="store_true", help="restore/transfer: do it (without: preview only)")
    p.add_argument("--replace-local", action="store_true",
                   help="transfer google_to_local into a non-empty Local database (backed up first)")
    p.set_defaults(fn=cmd_data)

    p = sub.add_parser("config", help="show or change settings")
    p.add_argument("action", choices=["show", "set"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("search", help="search parts")
    p.add_argument("query")
    p.add_argument("--make")
    p.add_argument("--model")
    p.add_argument("--year", type=int)
    p.add_argument("--vehicle")
    p.add_argument("--all", action="store_true", help="include out-of-stock parts")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("bin", help="list bins, or one bin's contents")
    p.add_argument("code", nargs="?")
    p.set_defaults(fn=cmd_bin)

    p = sub.add_parser("printer", help="printer status / configure / make-template / test label")
    p.add_argument("action", choices=["status", "configure", "make-template", "test"])
    p.add_argument("--out", help="make-template: where to write the .lbx (default Documents\\Labels\\torino-<tape>mm.lbx)")
    p.add_argument("--queue", help="Windows printer queue name")
    p.add_argument("--template", help="P-touch Editor .lbx template path")
    p.add_argument("--tape", help="TZe tape width in mm (3.5, 6, 9, 12, 18, 24)")
    p.add_argument("--mode", choices=["preview", "live"])
    p.add_argument("--auto-print", choices=["on", "off"])
    p.add_argument("--live", action="store_true", help="test: print one physical label via b-PAC")
    p.add_argument("--preview", action="store_true", help="test: preview only")
    p.add_argument("--name", default="TEST LABEL Door hinge LH", help="test: text for the name line")
    p.add_argument("--bin", default="B-04", help="test: bin code to print")
    p.add_argument("--request-id", help="test: reuse to retry the same test job without reprinting")
    p.set_defaults(fn=cmd_printer)

    p = sub.add_parser("labels", help="label jobs: list / process / resolve / print")
    p.add_argument("action", choices=["list", "process", "resolve", "print"])
    p.add_argument("job_id", nargs="?", help="resolve: job id")
    p.add_argument("resolution", nargs="?", choices=["reprint", "mark_printed", "cancel"])
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--part-id")
    p.add_argument("--bin")
    p.add_argument("--copies", type=int, default=1)
    p.add_argument("--request-id")
    p.set_defaults(fn=cmd_labels)

    sub.add_parser("mcp-config", help="print MCP registration commands for Claude Code and Codex").set_defaults(fn=cmd_mcp_config)
    sub.add_parser("info", help="show backend, data location and printer mode").set_defaults(fn=cmd_info)
    sub.add_parser("instructions", help="print the ready-to-paste AI client instructions").set_defaults(fn=cmd_instructions)

    args = ap.parse_args(argv)
    if args.cmd == "config" and args.action == "set" and (not args.key or args.value is None):
        ap.error("config set KEY VALUE")
    if args.cmd == "labels" and args.action == "resolve" and not (args.job_id and args.resolution):
        ap.error("labels resolve JOB_ID reprint|mark_printed|cancel")
    if args.cmd == "labels" and args.action == "print" and not args.part_id:
        ap.error("labels print --part-id P-00001 [--bin B-04]")
    try:
        args.fn(args)
    except Exception as e:  # noqa: BLE001
        from .inventory import InventoryError

        if isinstance(e, InventoryError):
            _print(e.to_dict())
            sys.exit(2)
        raise


if __name__ == "__main__":
    main()
