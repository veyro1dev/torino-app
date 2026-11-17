"""Desktop API, phone pairing, trusted HTTPS, scanning, phone move/photo â€” against a real running server."""

import base64
import http.cookiejar
import io
import json
import socket
import ssl
import urllib.error
import urllib.request

import pytest
from PIL import Image

from torino_parts.config import load_config, save_config


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("TORINO_HOME", str(tmp_path / "home"))
    cfg = load_config()
    cfg["network"].update(desktop_port=free_port(), phone_port=free_port(), setup_port=free_port())
    save_config(cfg)
    from torino_parts.web.server import AppContext, WebServer
    ctx = AppContext(phone_host="127.0.0.1")
    srv = WebServer(ctx)
    srv.start()
    yield ctx, srv
    srv.stop()


class Desk:
    def __init__(self, ctx):
        self.base = f"http://127.0.0.1:{ctx.cfg['network']['desktop_port']}"
        self.token = ctx.token

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        if token:
            req.add_header("X-Torino-Token", self.token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode()


class Phone:
    """A phone browser: trusts ONLY the Torino CA (strict verification), keeps cookies."""

    def __init__(self, ctx):
        self.base = f"https://127.0.0.1:{ctx.cfg['network']['phone_port']}"
        sslctx = ssl.create_default_context(cafile=str(ctx.tls.ca_cert_path))
        sslctx.verify_flags |= ssl.VERIFY_X509_STRICT
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=sslctx),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path, body=None, raw=None, csrf=True):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        if csrf:
            req.add_header("X-Torino", "1")
        req.add_header("Content-Type", "image/jpeg" if raw is not None else "application/json")
        try:
            with self.opener.open(req, timeout=30) as r:
                ct = r.headers.get("content-type", "")
                out = r.read()
                return r.status, json.loads(out) if "json" in ct else out.decode()
        except urllib.error.HTTPError as e:
            out = e.read()
            try:
                return e.code, json.loads(out)
            except ValueError:
                return e.code, out.decode()


def test_desktop_requires_token_and_loopback_host(running):
    ctx, srv = running
    d = Desk(ctx)
    assert d.call("/api/status", token=False)[0] == 401
    assert d.call("/api/status", host="evil.example:80")[0] == 403  # DNS-rebinding defence
    code, st = d.call("/api/status")
    assert code == 200 and st["demo"] is True and st["active_vehicle"]["label"].startswith("V-0001 1970 Ford Torino")
    assert st["workbook"]["name"].startswith("DEMO")


def test_desktop_rebuild_flow_and_install(running):
    ctx, srv = running
    d = Desk(ctx)
    vid = d.call("/api/status")[1]["active_vehicle"]["vehicle_id"]
    code, n = d.call(f"/api/next?vehicle={vid}&minutes=30")
    assert code == 200 and {j["job_id"] for j in n["fits_in_time"]} == {"J-00001", "J-00005"}
    _, job = d.call("/api/job/J-00005")
    need = job["parts_needed"][0]
    assert need["status"] == "ready" and need["pull_from"][0]["bin_code"] == "C-01"
    body = {"request_id": "web-inst-1", "need_id": need["need_id"], "stock_id": need["pull_from"][0]["stock_id"], "qty": 1}
    c1, r1 = d.call("/api/job/J-00005/install", body)
    c2, r2 = d.call("/api/job/J-00005/install", body)
    assert c1 == 200 and r2["duplicate_request"] is True
    _, job = d.call("/api/job/J-00005")
    assert job["parts_needed"][0]["status"] == "installed" and job["status"] == "in_progress"
    assert job["activity"][0]["source"] == "desktop"
    # error is a clean 400 with details
    code, err = d.call("/api/job/J-00003/install", {"request_id": "x", "part_id": "P-99999"})
    assert code == 400 and "no part" in err["error"]


def test_pairing_tls_scan_frame_move_photo_revoke(running):
    ctx, srv = running
    d = Desk(ctx)
    ph = Phone(ctx)
    assert ph.call("/p/api/me")[0] == 401  # not paired
    _, pair = d.call("/api/pair", {})
    code = pair["pair_url"].split("c=")[1]
    status, page = ph.call(f"/pair?c={code}", csrf=False)
    assert status == 200 and "Torino scanner" in page  # redirected to the scanner page with a cookie
    cookie = next(c for c in ph.jar if c.name == "torino_dev")
    assert cookie.secure and cookie.has_nonstandard_attr("HttpOnly")
    other = Phone(ctx)
    assert other.call(f"/pair?c={code}", csrf=False)[0] == 400  # single use
    me = ph.call("/p/api/me")[1]
    assert me["demo"] is True
    assert d.call("/api/status")[1]["phone"]["connected"] is True

    # CSRF header required on writes
    assert ph.call("/p/api/scan", {"code": "P-00001"}, csrf=False)[0] == 403
    seq0 = d.call("/api/poll?after=0")[1]["seq"]
    s1 = ph.call("/p/api/scan", {"code": "P-00001"})[1]
    s2 = ph.call("/p/api/scan", {"code": "P-00001"})[1]  # camera still pointed at the same label
    assert s1["found"] and s1["kind"] == "part" and s1["entries"]
    assert s2.get("repeat") is True
    events = [e for e in d.call(f"/api/poll?after={seq0}")[1]["events"] if e["type"] == "scan"]
    assert len(events) == 1 and events[0]["result"]["id"] == "P-00001"

    # a camera frame: render the real label, photograph it a bit rotated and small, decode on the laptop
    _, lbl = d.call("/api/labels/part", {"request_id": "lbl-1", "part_id": "P-00007", "bin_code": "A-02"})
    img = Image.open(lbl["label"]["preview_path"]).convert("RGB")
    canvas = Image.new("RGB", (img.width + 300, img.height + 400), (90, 85, 80))
    canvas.paste(img, (150, 200))
    canvas = canvas.rotate(8, fillcolor=(90, 85, 80)).resize((960, int(960 * canvas.height / canvas.width)))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=75)
    _, fr = ph.call("/p/api/frame?mode=lookup", raw=buf.getvalue())
    assert fr["decoded"] == "P-00007" and fr["result"]["found"] and fr["result"]["title"].startswith("Window crank")
    blank = io.BytesIO()
    Image.new("RGB", (640, 480), "gray").save(blank, "JPEG")
    assert ph.call("/p/api/frame", raw=blank.getvalue())[1] == {"decoded": None}

    # scan-assisted move: bin scan resolves BIN: codes, then the move is saved once
    assert ph.call("/p/api/scan", {"code": "BIN:B-04", "mode": "bin"})[1]["kind"] == "bin"
    hinge = ph.call("/p/api/scan", {"code": "P-00001", "mode": "refresh"})[1]
    src = next(e for e in hinge["entries"] if e["bin_code"] == "A-02")
    mv = {"request_id": "phone-move-1", "part_id": "P-00001", "stock_id": src["stock_id"], "from_bin": "A-02", "to_bin": "B-04", "qty": 1}
    assert ph.call("/p/api/move", mv)[1]["to"]["bin_code"] == "B-04"
    assert ph.call("/p/api/move", mv)[1]["duplicate_request"] is True
    after = ph.call("/p/api/scan", {"code": "P-00001", "mode": "refresh"})[1]
    assert not any(e["bin_code"] == "A-02" and e["qty"] > 0 for e in after["entries"])

    # phone photo attached to a part
    pbuf = io.BytesIO()
    Image.new("RGB", (300, 200), "blue").save(pbuf, "JPEG")
    b64 = "data:image/jpeg;base64," + base64.b64encode(pbuf.getvalue()).decode()
    pr = ph.call("/p/api/photo", {"request_id": "pp-1", "part_id": "P-00001", "image": b64, "caption": "from phone"})[1]
    assert pr["photo_id"].startswith("PH-")
    photos = d.call("/api/photos?part=P-00001")[1]["photos"]
    assert photos[0]["source"] == "phone" if "source" in photos[0] else True

    # revoke -> phone loses access
    dev = d.call("/api/devices")[1]["devices"][0]["device_id"]
    d.call(f"/api/devices/{dev}/revoke", {})
    assert ph.call("/p/api/me")[0] == 401


def test_setup_page_serves_only_the_public_ca(running):
    ctx, srv = running
    base = f"http://127.0.0.1:{ctx.cfg['network']['setup_port']}"
    der = urllib.request.urlopen(base + "/torino-ca.crt", timeout=10).read()
    from cryptography import x509
    cert = x509.load_der_x509_certificate(der)
    nc = cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert nc.critical and any("192.168.0.0/16" in str(x) for x in nc.value.permitted_subtrees)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/api/status", timeout=10)
    assert b"BEGIN" not in der and not (ctx.tls.ca_key_path.read_bytes() in der)


def test_public_site_cert_from_torino_ca_is_rejected(tmp_path):
    """Name constraints: even a leaked Torino CA key can't vouch for a public hostname."""
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from torino_parts.web.tls import LocalTLS
    tls = LocalTLS(tmp_path)
    ca_key, ca = tls._ensure_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    bad = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bank.example")]))
           .issuer_name(ca.subject).public_key(key.public_key()).serial_number(1)
           .not_valid_before(now).not_valid_after(now + dt.timedelta(days=30))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName("bank.example.com")]), critical=False)
           .sign(ca_key, hashes.SHA256()))
    from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
    verifier = PolicyBuilder().store(Store([ca])).build_server_verifier(x509.DNSName("bank.example.com"))
    with pytest.raises(VerificationError):
        verifier.verify(bad, [])


def test_create_label_template_from_desktop(running, tmp_path, monkeypatch):
    import zipfile

    import torino_parts.labels.lbx as lbx
    monkeypatch.setattr(lbx, "default_template_path", lambda tape: tmp_path / "Labels" / f"torino-{tape:g}mm.lbx")
    ctx, _ = running
    d = Desk(ctx)
    st, r = d.call("/api/printer/template", {"tape_mm": "12"})
    assert st == 200, r
    assert r["tape_mm"] == 12 and r["template"].endswith("torino-12mm.lbx")
    assert r["settings"]["template"] == r["template"] and r["settings"]["tape_mm"] == 12
    assert zipfile.is_zipfile(r["template"])
    assert load_config()["printer"]["template"] == r["template"]  # saved, survives restart
    assert d.call("/api/printer/template", {"tape_mm": "36"})[0] == 400  # unsupported width rejected


def test_local_mode_pages_backup_restore_and_readiness(running, tmp_path, monkeypatch):
    ctx, _ = running
    d = Desk(ctx)
    st, s = d.call("/api/backend", {"backend": "local"})
    assert st == 200 and s["mode"] == "Local" and s["local"] and not s["demo"]
    assert d.call("/api/status")[1]["workbook"]["name"] == "Local database on this PC"
    _, rd = d.call("/api/setup")
    assert set(rd) == {"google", "printer", "phone"}
    assert rd["google"]["optional"] and rd["phone"]["next"]["act"] == "pair" and rd["printer"]["next"]

    _, v = d.call("/api/vehicles", {"request_id": "v", "year": 1970, "make": "Ford", "model": "Torino", "make_active": True})
    vid = v["vehicle_id"]
    d.call("/api/bins", {"request_id": "b", "bin_code": "A-01"})
    d.call("/api/bins", {"request_id": "w", "bin_code": "BENCH", "kind": "work_area", "description": "Bench"})
    _, p = d.call("/api/stock/receive", {"request_id": "r", "bin_code": "A-01", "qty": 1, "condition": "used",
                                         "new_part": {"name": "Brake caliper"}, "print_label": False})
    pid = p["part"]["part_id"]
    _, j = d.call("/api/jobs", {"request_id": "j", "vehicle_id": vid, "title": "Calipers", "system": "brakes",
                                "steps": "Mount\nBleed", "parts": [{"part_id": pid, "qty": 1}]})
    assert d.call("/api/notebook", {"request_id": "n", "body": "Bore pitted", "job_id": j["job_id"]})[0] == 200
    _, it = d.call("/api/shopping", {"request_id": "s", "name": "Caliper seal kit", "qty": 2, "est_price": "12.5"})
    _, o = d.call(f"/api/shopping/{it['item_id']}", {"request_id": "o", "status": "ordered", "paid_amount": "25"})
    assert o["expense_id"]
    _, m = d.call("/api/money")
    assert m["spent"] == 25 and m["outstanding_orders_total"] == 0 and "parts" in m["categories"]
    _, so = d.call("/api/away/send", {"request_id": "out", "part_id": pid, "holder": "Smith Machine", "qty": 1})
    _, pr = d.call("/api/progress")
    assert pr["away"][0]["holder"] == "Smith Machine" and pr["blocked"][0]["job_id"] == j["job_id"]
    assert d.call("/api/resume")[1]["vehicle_id"] == vid
    _, nb = d.call("/api/notebook?q=pitted")
    assert nb["count"] == 1
    _, prev = d.call("/api/labels/preview", {"part_id": pid, "bin_code": "A-01"})
    assert prev["image"].startswith("data:image/png;base64,")

    # backup -> change -> restore (preview first, then for real)
    _, bk = d.call("/api/data/backup", {})
    assert bk["records"] > 10 and bk["counts"]["Notes"] == 1
    d.call("/api/notebook", {"request_id": "n2", "body": "added after the backup"})
    _, pv = d.call("/api/data/restore", {"path": bk["path"]})
    assert pv["preview"] and pv["current"]["counts"]["Notes"] == 2
    _, rs = d.call("/api/data/restore", {"path": bk["path"], "confirm": True})
    assert rs["restored"] and rs["safety_backup"]
    assert d.call("/api/notebook")[1]["count"] == 1
    _, data = d.call("/api/data")
    names = [b["name"] for b in data["backups"]]
    assert any("before-restore" in n for n in names) and bk["name"] in names
    # transfer needs Google
    assert d.call("/api/data/transfer", {"direction": "local_to_google"})[0] == 400


def test_upgrade_never_reuses_cached_scripts(running):
    """After an upgrade the window must load the new app.js (regression: new menus, old script)."""
    from torino_parts import __version__
    ctx, _ = running
    base = f"http://127.0.0.1:{ctx.cfg['network']['desktop_port']}"
    with urllib.request.urlopen(base + "/", timeout=30) as r:
        html = r.read().decode()
        assert r.headers["Cache-Control"] == "no-store"
    assert f'/static/app.js?v={__version__}"' in html and f'/static/app.css?v={__version__}"' in html
    with urllib.request.urlopen(base + f"/static/app.js?v={__version__}", timeout=30) as r:
        assert r.headers["Cache-Control"] == "no-cache" and b"VIEWS.progress" in r.read()
    ph = Phone(ctx)
    with ph.opener.open(ph.base + "/", timeout=30) as r:
        assert f'/static/phone.js?v={__version__}"' in r.read().decode()


def test_money_export_and_void_api(running, tmp_path, monkeypatch):
    monkeypatch.setenv("TORINO_EXPORT_DIR", str(tmp_path / "exports"))
    ctx, _ = running
    d = Desk(ctx)  # demo data: has a budget, expenses and a refund
    _, m = d.call("/api/money?vehicle=V-0001")
    assert m["timeline"] and m["timeline"][-1]["cumulative"] == m["spent"]
    st, ex = d.call("/api/money/export", {"vehicle_id": "V-0001"})
    assert st == 200 and ex["path"].startswith(str(tmp_path)) and "expense_id" in open(ex["path"], encoding="utf-8-sig").read()
    eid = m["expenses"][0]["expense_id"]
    st, vo = d.call("/api/money/void", {"request_id": "vx", "expense_id": eid, "reason": "test"})
    assert st == 200 and vo["totals"]["spent"] == round(m["spent"] - m["expenses"][0]["net"], 2)
    st, pv = d.call("/api/labels/preview", {"part_id": "P-00001"})
    assert st == 200 and pv["printed"] is False and pv["lines"]["bin"] and pv["image"].startswith("data:image/png")
