"""
Nurse Call System — FastAPI application entry point.

Serves:
  - REST API  under /api/...
  - WebSocket at /ws
  - Static UI  at /

Start with:
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from database import init_db
from modules.ws_manager import manager as ws_manager
from modules.innovonics import start_listener, stop_listener
from modules.roam_alert import start_listener as ra_start, stop_listener as ra_stop
from modules.aeroscout import start_listener as ale_start, stop_listener as ale_stop
from modules.input_monitor import start_monitor as input_start, stop_monitor as input_stop
from routes import calls, devices, settings, users, apartments, areas
from routes import auth as auth_router
from routes import roam_alert as ra_router
from routes import aeroscout as aeroscout_router
from routes import staff as staff_router
from routes import push as push_router

# ---------------------------------------------------------------------------
# Installer backdoor — run once to disable auth requirement
#   python main.py --ccrootreset
# ---------------------------------------------------------------------------
if "--ccrootreset" in sys.argv:
    init_db()
    from database import db
    with db() as _conn:
        _conn.execute("UPDATE system_config SET auth_required=0 WHERE id=1")
    print("[ccrootreset] Authentication requirement disabled. Restart the server normally.")
    sys.exit(0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Nurse Call System starting…")
    init_db()
    # Start Innovonics listener
    listener_task = asyncio.create_task(start_listener())
    # Start Roam Alert listener (connects only if networks are configured)
    ra_task  = asyncio.create_task(ra_start())
    # Start AeroScout ALE listener (connects only if configured and enabled)
    ale_task = asyncio.create_task(ale_start())
    # Start ESP input polling monitor
    input_task = asyncio.create_task(input_start())
    yield
    # Graceful shutdown
    await stop_listener()
    listener_task.cancel()
    await ra_stop()
    ra_task.cancel()
    await ale_stop()
    ale_task.cancel()
    await input_stop()
    input_task.cancel()
    for t in (listener_task, ra_task, ale_task, input_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Community Call System",
    version="1.0.1-alpha",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (CSS, JS)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------------------------------------------------------------------
# API routers
# ---------------------------------------------------------------------------

app.include_router(auth_router.router, prefix="/api/auth",       tags=["Auth"])
app.include_router(calls.router,       prefix="/api/calls",      tags=["Calls"])
app.include_router(devices.router,     prefix="/api/devices",    tags=["Devices"])
app.include_router(settings.router,    prefix="/api/settings",   tags=["Settings"])
app.include_router(users.router,       prefix="/api/users",      tags=["Users"])
app.include_router(apartments.router,  prefix="/api/apartments", tags=["Apartments"])
app.include_router(areas.router,       prefix="/api/areas",      tags=["Areas"])
app.include_router(ra_router.router,        prefix="/api/ra",         tags=["RoamAlert"])
app.include_router(aeroscout_router.router, prefix="/api/aeroscout",  tags=["AeroScout"])
app.include_router(staff_router.router,     prefix="/api/staff",      tags=["Staff"])
app.include_router(push_router.router,      prefix="/api/push",       tags=["Push"])


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    log.info("WebSocket client connected (%d total)", ws_manager.count)
    try:
        # Send current active calls on connect so the UI bootstraps immediately
        from database import db
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM calls WHERE status IN ('active','acknowledged') ORDER BY timestamp DESC"
            ).fetchall()
        active = [dict(r) for r in rows]
        await websocket.send_text(
            json.dumps({"event": "init", "data": {"calls": active}})
        )
        # Keep connection alive; client sends pings
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text(
                    json.dumps({"event": "pong", "data": {}})
                )
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket)
        log.info("WebSocket client disconnected (%d remaining)", ws_manager.count)


# ---------------------------------------------------------------------------
# Root — serve the UI
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/aeroscoutraw")
async def aeroscout_raw_monitor():
    return FileResponse("static/aeroscoutraw.html")


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json", media_type="application/json")


@app.get("/health")
async def health():
    return {"status": "ok", "ws_clients": ws_manager.count}


# ---------------------------------------------------------------------------
# SSL certificate download — allows devices to install the self-signed cert
#   GET /sslcert        → download the .crt file directly
#   GET /sslcert?info=1 → show an HTML install-guide page
#
# Certificate lookup order:
#   1. SSL_CERT_FILE  environment variable
#   2. certs/server.crt  relative to this file
#   3. /etc/commcall/server.crt  (system-level install)
# ---------------------------------------------------------------------------

def _find_cert() -> Path | None:
    # Serve ca.crt — the CA certificate users install on their devices.
    # This has basicConstraints=CA:TRUE so Android/iOS/Mac accept it as a
    # trusted root. The server cert (cert.pem, CA:FALSE) is used by nginx
    # internally and is NOT what devices should install.
    candidates = [
        os.environ.get("SSL_CERT_FILE", ""),        # explicit override
        Path("/etc/nginx/ssl/ca.crt"),               # 2-tier PKI (Docker)
        Path(__file__).parent / "certs" / "ca.crt", # local dev fallback
    ]
    for c in candidates:
        p = Path(c) if c else None
        if p and p.is_file():
            return p
    return None


@app.get("/sslcert")
async def ssl_cert(info: bool = False):
    cert = _find_cert()

    if info or cert is None:
        # Human-readable install guide (also shown when no cert is found yet)
        found_note = (
            f"<p class='found'>Certificate found at: <code>{cert}</code></p>"
            if cert else
            "<p class='missing'>No certificate file found. Place your certificate at "
            "<code>certs/server.crt</code> inside the application directory, "
            "or set the <code>SSL_CERT_FILE</code> environment variable.</p>"
        )
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Install SSL Certificate — Community Call</title>
  <style>
    body{{font-family:system-ui,sans-serif;max-width:600px;margin:40px auto;padding:0 20px;color:#1e293b}}
    h1{{font-size:22px;margin-bottom:4px}}
    .sub{{color:#64748b;margin-bottom:24px;font-size:14px}}
    .found{{background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:10px 14px;color:#166534}}
    .missing{{background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:10px 14px;color:#991b1b}}
    .btn{{display:inline-block;background:#2563eb;color:#fff;padding:10px 20px;border-radius:8px;
          text-decoration:none;font-weight:600;margin:16px 0}}
    ol li{{margin-bottom:8px;line-height:1.6}}
    code{{background:#f1f5f9;padding:2px 5px;border-radius:4px;font-size:13px}}
    .section{{margin-top:28px}}
    h2{{font-size:16px;margin-bottom:8px}}
  </style>
</head>
<body>
  <h1>Install SSL Certificate</h1>
  <p class="sub">Community Call — Life Safety System</p>

  {found_note}

  {"<a class='btn' href='/sslcert'>Download Certificate (.crt)</a>" if cert else ""}

  <div class="section">
    <h2>Android</h2>
    <ol>
      <li>Tap <strong>Download Certificate</strong> above.</li>
      <li>When prompted, name the certificate (e.g. <em>CommCall</em>) and choose
          <strong>CA certificate</strong> as the credential use.</li>
      <li>If your browser saves it as a file instead: open
          <strong>Settings → Security → Encryption &amp; credentials →
          Install a certificate → CA certificate</strong> and pick the downloaded file.</li>
      <li>Accept the warning. The certificate is now trusted system-wide.</li>
    </ol>
    <p style="margin-top:8px;font-size:13px;color:#64748b">
      Path varies by manufacturer — on Samsung try
      <em>Settings → Biometrics &amp; Security → Other security settings</em>.
    </p>
  </div>

  <div class="section">
    <h2>iPhone / iPad</h2>
    <ol>
      <li>Tap <strong>Download Certificate</strong> above — Safari will prompt to allow the profile.</li>
      <li>Go to <strong>Settings → General → VPN &amp; Device Management</strong> and tap the profile.</li>
      <li>Tap <strong>Install</strong>, enter your passcode, and confirm.</li>
      <li>Go to <strong>Settings → General → About → Certificate Trust Settings</strong>
          and toggle on full trust for the certificate.</li>
    </ol>
  </div>

  <div class="section">
    <h2>Mac</h2>
    <ol>
      <li>Download the certificate and double-click the file — it opens in <strong>Keychain Access</strong>.</li>
      <li>The cert appears in your login keychain. Double-click it.</li>
      <li>Expand <strong>Trust</strong> and set <em>When using this certificate</em> to
          <strong>Always Trust</strong>.</li>
      <li>Close the window and enter your Mac password to save. Chrome/Safari will now trust the site.</li>
    </ol>
    <p style="margin-top:8px;font-size:13px;color:#64748b">
      Firefox manages its own trust store — import via
      <em>Settings → Privacy &amp; Security → Certificates → View Certificates → Authorities → Import</em>.
    </p>
  </div>

  <div class="section">
    <h2>Windows</h2>
    <ol>
      <li>Download the certificate and double-click it.</li>
      <li>Click <strong>Install Certificate → Local Machine → Trusted Root Certification Authorities</strong>.</li>
    </ol>
  </div>
</body>
</html>"""
        return HTMLResponse(html)

    # Serve the raw certificate for direct download / Android auto-install
    return FileResponse(
        str(cert),
        media_type="application/x-x509-ca-cert",
        filename="commcall-ca.crt",
        headers={"Content-Disposition": 'attachment; filename="commcall-ca.crt"'},
    )
