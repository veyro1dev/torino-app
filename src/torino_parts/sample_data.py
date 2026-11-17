"""Sample (DEMO) data: a 1970 Torino rebuild in progress, a Monte Carlo and a donor Montego.

Used by `torino demo` and when the desktop app opens an empty demo workbook. Spec values here are
placeholders on purpose - real values must come from the service manual.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def _photo(path: Path, kind: str) -> Path:
    img = Image.new("RGB", (800, 600), (92, 88, 80))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 420, 800, 600], fill=(60, 58, 55))
    if kind == "crank":
        d.ellipse([180, 240, 290, 350], fill=(200, 200, 205), outline=(120, 120, 125), width=5)
        d.line([235, 295, 590, 220], fill=(210, 210, 215), width=32)
        d.ellipse([550, 150, 650, 250], fill=(30, 30, 30))
    else:  # hinge in place on a door
        d.rectangle([120, 60, 700, 560], fill=(40, 70, 130))
        d.rectangle([140, 180, 330, 330], fill=(150, 150, 150), outline=(80, 80, 80), width=6)
        for y in (205, 300):
            d.ellipse([170, y - 14, 198, y + 14], fill=(60, 60, 60))
            d.ellipse([270, y - 14, 298, y + 14], fill=(60, 60, 60))
    img = img.filter(ImageFilter.GaussianBlur(1.0))
    ImageDraw.Draw(img).text((14, 575), "DEMO PHOTO (synthetic)", fill=(235, 235, 235))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=88)
    return path


def seed_sample_data(g, photo_dir: Path | None = None) -> dict[str, str]:
    """Create the sample garage. Idempotent (fixed request IDs)."""
    photo_dir = Path(photo_dir or Path(g.photos.folder).parent / "sample-photos")
    ids: dict[str, str] = {}
    for code, desc, loc in [("A-01", "Door hardware", "Shelf A, top"), ("A-02", "Door hardware overflow", "Shelf A, middle"),
                            ("B-04", "Brake parts", "Shelf B"), ("C-01", "New old stock / small parts", "Cabinet C"),
                            ("T-01", "Teardown bags - Torino", "Bench drawer")]:
        try:
            g.create_bin(f"sample-bin-{code}", code, desc, loc)
        except Exception as e:  # noqa: BLE001 - re-seeding
            if "already exists" not in str(e):
                raise
    ids["torino"] = g.add_vehicle("sample-veh-torino", 1970, "Ford", "Torino", nickname="Torino", trim="GT",
                                  engine="351 Cleveland 4V")["vehicle_id"]
    ids["monte"] = g.add_vehicle("sample-veh-monte", 1987, "Chevrolet", "Monte Carlo", nickname="Monte", trim="SS",
                                 engine="305 V8")["vehicle_id"]
    ids["montego"] = g.add_vehicle("sample-veh-montego", 1971, "Mercury", "Montego", nickname="Parts Montego",
                                   trim="MX", notes="donor / parts car")["vehicle_id"]
    T, M, D = ids["torino"], ids["monte"], ids["montego"]
    fit_torino = {"make": "Ford", "model": "Torino", "year_from": 1970, "year_to": 1971, "verified": True, "source": "user"}

    def part(rid, bin_code, qty, cond, np=None, pid=None, **kw):
        return g.receive_stock(f"sample-{rid}", bin_code, qty, cond, part_id=pid, new_part=np, print_label=False, **kw)["part"]["part_id"]

    ids["hinge_lh"] = part("hinge-lh-a01", "A-01", 2, "used", donor_vehicle_id=D, np={
        "name": "Door hinge, upper", "side_position": "left front", "category": "body",
        "fitment": [fit_torino, {"make": "Mercury", "model": "Montego", "year_from": 1970, "year_to": 1971},
                    {"make": "Ford", "model": "Ranchero", "year_from": 1970, "year_to": 1971}]})
    part("hinge-lh-a02", "A-02", 1, "used", pid=ids["hinge_lh"], donor_vehicle_id=D)
    part("hinge-lh-c01", "C-01", 1, "new", pid=ids["hinge_lh"], notes="NOS in box")
    ids["hinge_rh"] = part("hinge-rh", "A-01", 1, "used", donor_vehicle_id=D, np={
        "name": "Door hinge, upper", "side_position": "right front", "category": "body", "fitment": [fit_torino]})
    ids["caliper_kit"] = part("caliper-kit", "B-04", 2, "new", np={
        "name": "Caliper rebuild kit", "category": "brakes", "fitment": [fit_torino]})
    ids["pads"] = part("pads", "B-04", 1, "new", np={"name": "Front brake pad set", "category": "brakes",
                                                      "fitment": [fit_torino]})
    ids["fluid"] = part("fluid", "C-01", 2, "new", np={"name": "Brake fluid DOT 3, 12 oz", "category": "brakes",
                                                        "fitment_scope": "universal"})
    ids["bushings"] = part("bushings", "C-01", 1, "new", np={"name": "Sway bar bushing kit, front", "category": "suspension",
                                                              "fitment": [fit_torino]})
    ids["crank"] = part("crank", "A-02", 1, "used", donor_vehicle_id=D, photo_path=str(_photo(photo_dir / "crank.jpg", "crank")),
                        np={"name": "Window crank handle", "category": "interior", "notes": "chrome arm, black knob",
                            "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970, "year_to": 1971,
                                         "source": "photo identification"}]})
    ids["lens"] = part("lens", "A-02", 1, "used", np={"name": "Tail lamp lens", "side_position": "left rear",
                                                      "category": "lighting", "fitment": [
                                                          {"make": "Chevrolet", "model": "Monte Carlo", "year_from": 1983,
                                                           "year_to": 1988, "verified": True, "source": "user"}]})
    ids["unknown"] = part("unknown", "C-01", 1, "used", np={"unidentified": True,
                                                             "notes": "L-shaped steel bracket, two 8 mm holes"})

    # ---- rebuild jobs (Torino)
    td = g.create_job("sample-job-td-door", T, "Strip left door for hinge work", "body", kind="teardown",
                      goal="Door off the car, hardware bagged and labelled", estimate_min=60,
                      steps=["Support door on stand", "Mark hinge positions", "Unbolt hinges", "Bag and label hardware"])
    ids["job_teardown"] = td["job_id"]
    calipers = g.create_job("sample-job-calipers", T, "Rebuild front brake calipers", "brakes",
                            goal="Both front calipers rebuilt, no leaks, ready to mount", priority=1, estimate_min=120,
                            steps=["Remove calipers", "Strip and clean", "Replace seals and boots", "Bench bleed",
                                   "Paint"], parts=[{"part_id": ids["caliper_kit"], "qty": 2},
                                                    {"part_id": ids["fluid"], "qty": 1}])
    ids["job_calipers"] = calipers["job_id"]
    rotors = g.create_job("sample-job-rotors", T, "Install front rotors and pads", "brakes",
                          goal="Front brakes assembled and bled", priority=1, estimate_min=90,
                          depends_on=[calipers["job_id"]],
                          steps=["Pack wheel bearings", "Mount rotors", "Install pads", "Mount calipers", "Bleed"],
                          parts=[{"description": "Front disc brake rotor (1970 Torino)", "qty": 2},
                                 {"part_id": ids["pads"], "qty": 1}])
    ids["job_rotors"] = rotors["job_id"]
    door = g.create_job("sample-job-hang-door", T, "Hang left door", "body", goal="Door hung, gaps even, latches cleanly",
                        priority=2, estimate_min=45, depends_on=[td["job_id"]],
                        steps=["Inspect hinge pins and bushings", "Bolt hinges to door", "Hang door", "Align gaps"],
                        parts=[{"part_id": ids["hinge_lh"], "qty": 1, "check": "inspect"}])
    ids["job_door"] = door["job_id"]
    sway = g.create_job("sample-job-sway", T, "Replace front sway bar bushings", "suspension", priority=3,
                        estimate_min=30, steps=["Raise and support front", "Unbolt brackets", "Fit new bushings",
                                                "Torque brackets to spec"],
                        parts=[{"part_id": ids["bushings"], "qty": 1}])
    ids["job_sway"] = sway["job_id"]
    crank = g.create_job("sample-job-crank", T, "Install left window crank handle", "interior", priority=4,
                         estimate_min=10, depends_on=[door["job_id"]],
                         parts=[{"part_id": ids["crank"], "qty": 1}])
    ids["job_crank"] = crank["job_id"]
    harness = g.create_job("sample-job-harness", T, "Install dash wiring harness", "electrical", priority=2,
                           goal="Gauges and lights work", parts=[{"description": "Instrument panel wiring harness", "qty": 1}])
    ids["job_harness"] = harness["job_id"]

    # progress on the teardown: hinges came off, bagged and labelled, linked to the hang-door job
    steps = g.get_job(td["job_id"])["checklist"]
    g.update_job("sample-td-progress", td["job_id"], status="in_progress",
                 steps_done=[s["step_id"] for s in steps[:2]], note="Door on stand; hinge positions scribed.")
    rm = g.record_removal("sample-removal-hinge-bolts", T, "T-01", 6, "used", removed_from="left door hinges",
                          job_id=td["job_id"], reinstall_job_id=door["job_id"], print_label=False,
                          photo_path=str(_photo(photo_dir / "hinge-in-place.jpg", "hinge")),
                          new_part={"name": "Door hinge bolt", "category": "hardware", "notes": "5/16-18, grade 5 marks"})
    ids["hinge_bolts"] = rm["part"]["part_id"]
    cs = g.get_job(calipers["job_id"])["checklist"]
    g.update_job("sample-calipers-progress", calipers["job_id"], status="in_progress",
                 steps_done=[cs[0]["step_id"], cs[1]["step_id"]],
                 note="Both calipers off and cleaned. Left piston was stuck - freed with air.")
    g.record_spec("sample-spec-caliper", "Caliper anchor plate bolt torque", "(DEMO placeholder - look up)",
                  source="1970 Ford Car Shop Manual - look up the real value before use", unit="ft-lb",
                  job_id=calipers["job_id"])
    # Monte Carlo job
    ids["job_lens"] = g.create_job("sample-job-lens", M, "Replace left tail lamp lens", "body", estimate_min=20,
                                   parts=[{"part_id": ids["lens"], "qty": 1}])["job_id"]

    # ---- v0.3 examples (added after the v0.2 records, so earlier sample IDs are unchanged) ----
    from datetime import date, timedelta

    g.create_bin("sample-bin-bench", "BENCH", "Work bench", "garage, left wall", kind="work_area")
    box = g.receive_stock("sample-steering-box", "C-01", 1, "used", print_label=False,
                          new_part={"name": "Power steering gear box", "category": "steering",
                                    "fitment": [{"make": "Ford", "model": "Torino", "year_from": 1970, "year_to": 1971}]})
    ids["steering_box"] = box["part"]["part_id"]
    steer = g.create_job("sample-job-steering", T, "Install rebuilt steering box", "steering", priority=2,
                         estimate_min=90, steps=["Bolt box to frame", "Connect pitman arm", "Fill and bleed"],
                         parts=[{"part_id": ids["steering_box"], "qty": 1}])
    ids["job_steering"] = steer["job_id"]
    g.send_out("sample-send-steering", ids["steering_box"], "Smith Machine Shop", 1, "rebuild and reseal",
               expected_back=(date.today() + timedelta(days=12)).isoformat(), job_id=steer["job_id"])
    rotor_need = next(n["need_id"] for n in g.get_job(rotors["job_id"])["parts_needed"] if not n["part_id"])
    rot = g.add_shopping_item("sample-shop-rotors", T, "Front disc brake rotor", 2, "ordered", vendor="RockAuto",
                              est_price=48.79, job_id=rotors["job_id"], need_id=rotor_need, category="parts")
    ago = lambda n: (date.today() - timedelta(days=n)).isoformat()  # noqa: E731
    g.update_shopping_item("sample-shop-rotors-paid", rot["item_id"], order_ref="RA-DEMO-1", paid_amount=97.58,
                           paid_date=ago(12))
    harness_need = next(n["need_id"] for n in g.get_job(harness["job_id"])["parts_needed"])
    g.add_shopping_item("sample-shop-harness", T, "Instrument panel wiring harness (reproduction)", 1, "considering",
                        vendor="(compare 2 suppliers)", est_price=389.0, job_id=harness["job_id"], need_id=harness_need,
                        category="parts", notes="DEMO: check connector count before buying")
    g.add_shopping_item("sample-shop-fluid", T, "Brake fluid DOT 3, 32 oz", 1, "ready", est_price=11.99,
                        category="fluids")
    g.set_budget("sample-budget", T, 15000, notes="DEMO budget")
    g.set_budget("sample-budget-tools", T, 300, category="tools", notes="DEMO")
    g.record_expense("sample-exp-manual", T, 64.95, vendor="Detroit Iron", category="other", date=ago(30),
                     description="1970 Ford Car Shop Manual set (reprint)")
    g.record_expense("sample-exp-deposit", T, 150, vendor="Smith Machine Shop", category="machine shop",
                     description="Steering box rebuild deposit", job_id=steer["job_id"], date=ago(20))
    g.record_expense("sample-exp-tool", T, 34.99, vendor="Harbor Freight", category="tools",
                     description="Brake line flaring tool", date=ago(9))
    g.record_expense("sample-exp-refund", T, 10, kind="refund", vendor="Harbor Freight", category="tools",
                     description="Returned unused adapter", date=ago(3))
    g.add_note("sample-note-gaps", "Door gap at the rear edge measured 5/32 in at the top, 7/32 in at the bottom.",
               title="Left door gaps before teardown", tags="body, measure", job_id=td["job_id"])
    return ids
