**ALPHA SYSTEM - NOT STRESS TESTED IN REAL HEALTCHARE ENVIROMENT!! /**

# Community Call — Life Safety & Nurse Call System

Community Call is a self-hosted, real-time nurse call and life safety management platform. It receives alarm signals from wireless pendants, pull cords, and environmental sensors, displays them on a live dashboard, and dispatches notifications through multiple channels. The System also has integreations for some of the Top Wander Management systems in Healthcare. The system runs entirely on your local network with no cloud dependency.

---

## Features

**Real-time Dashboard**
- Live call cards showing device name, location, priority, and elapsed time
- Colour-coded priority levels: Normal, Urgent, Emergency
- Acknowledge and clear calls directly from the dashboard
- Automatic call deduplication — one active call per device at a time
- WebSocket-driven updates; all connected clients stay in sync instantly

**Wireless Device Support**
- Innovonics 900 MHz wireless coordinator (TCP/IP via Lantronix or MOXA serial-to-IP)
- Pull Cords, Pendants, Universal TX, Even Proprietary Hardware built on innovonics, making System Takeovers a breeze.
- Tamper detection and supervision monitoring
- Repeater check-in tracking with last-seen timestamps

**Location Tracking**
- Securitas RTLS integration via the Aeroscout Location Engine (ALE) backend
- Real-time tag location, battery status, and location quality
- Controller/exciter device status monitoring (EX5700, DC1000, GW3100)

**Wander Management**
- Roam Alert wander system integration - with code control support
- WanderGuard Blue Integreation - full device control support.
- Door controller monitoring with online/offline status
- Resident tag tracking and door-crossing event logging

**Notifications**
- Email (SMTP with STARTTLS/SSL)
- SMS via Twilio
- Telegram bot messages
- Pogsac Pager protocol
- Browser push notifications (Web Push — works when the PWA is in the background or closed)
- Flexible notification rules: filter by device, priority, area, and trigger (call / clear / both)
- Template variables in message bodies: `{device_name}`, `{location}`, `{priority}`, `{area}`, etc.

**Relay / Dome Light Control**
- Supports RCM and ESP-based relay controllers
- Per-device, per-apartment, and per-area relay assignments. Giving the most freedom of control of Inputs
- Automatic activation on call, deactivation on clear

**Programmable Input Alarms**
- Using ESP Input controller the system can monitor inputs and fire diffferent levels of alarms, useful for door monitors/panic alarms. 


**Staff Features**
- Role-based access: Admin, Staff, Viewer
- Staff-to-staff real-time chat (visible to all logged-in users)
- Staff Emergency button — fires a dashboard alarm with acknowledge/clear workflow and alerts all connected clients
- Per-priority audio alerts (1/2/3 beeps at configurable intervals)

**Organisation**
- Areas (floors, wings, buildings)
- Apartments/units grouped within areas
- Devices assigned to apartments with optional relay overrides

**Progressive Web Application**
- Installable on Android, iOS, and desktop as a standalone app
- Service worker with network-first HTML and stale-while-revalidate assets
- Double-click the logo for a hard cache reset

---

## Architecture

```
Browser / PWA
     │  WebSocket (/ws) + REST (/api/...)
     ▼
 Nginx (TLS termination, port 443)
     │  HTTP proxy
     ▼
 FastAPI app (uvicorn, port 8000)
     ├── SQLite database (nurse_call.db)
     ├── Innovonics TCP listener
     ├── Roam Alert TCP listener
     ├── AeroScout ALE TCP listener
     └── ESP input polling monitor
```

All services run as Docker containers. The application source and database are bind-mounted from the host, making the deployment fully portable.

---

## Prerequisites

- Docker and Docker Compose (v2)
- A machine on your local network (Raspberry Pi 4+, mini PC, or server)
- Devices accessible on the same LAN as the server

---

## Quick Start

### 1. Clone / copy the project folder

```bash
# Copy the Alpha/ folder to your server, then enter it
cd community-call
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# IP address of this server on your LAN
# The TLS certificate will include this IP as a Subject Alternative Name
# so browsers can access the system by IP without a hostname
SERVER_IP=192.168.1.100

# Optional: change the certificate CN (default: community-call)
SERVER_NAME=community-call
```

### 3. Start

```bash
docker compose up -d
```

On first boot, nginx generates a local CA certificate and a server certificate signed by it. Both are stored in `./certs/`.

### 4. Install the CA certificate on each device (one-time)

Visit `https://<SERVER_IP>/sslcert` on each device and follow the instructions for your OS. Once the CA certificate is installed, the green padlock appears and push notifications work.

### 5. Access the dashboard

Open `https://<SERVER_IP>` in a browser or install the PWA.

---

## File Layout

```
community-call/
├── docker-compose.yml      # Service definitions
├── Dockerfile              # App container
├── nginx/
│   ├── Dockerfile
│   ├── nginx.conf          # Reverse proxy + WebSocket config
│   └── entrypoint.sh       # Certificate generation + nginx start
├── main.py                 # FastAPI entry point + lifespan
├── database.py             # SQLite schema + migrations
├── modules/
│   ├── call_manager.py     # Call create/ack/clear + notifications
│   ├── innovonics.py       # Innovonics wireless coordinator
│   ├── aeroscout.py        # AeroScout ALE client
│   ├── roam_alert.py       # Roam Alert wander system
│   ├── input_monitor.py    # ESP/ESPHome digital input polling
│   ├── push_manager.py     # Web Push (VAPID) manager
│   ├── ws_manager.py       # WebSocket broadcast manager
│   └── ...                 # Email, pager, relay, etc.
├── routes/                 # FastAPI routers
├── static/
│   ├── index.html
│   ├── sw.js               # Service worker
│   ├── manifest.json
│   ├── css/style.css
│   └── js/app.js
├── nurse_call.db           # SQLite database (created on first run)
└── certs/                  # TLS certificates (created on first run)
    ├── ca.crt              # CA certificate — install on devices
    ├── ca.key              # CA private key  — stays on server
    ├── cert.pem            # Server certificate (signed by CA)
    └── key.pem             # Server private key
```

---

## Configuration

All configuration is done through the Settings page in the UI (Admin role required or Auth disabled).

### Innovonics Coordinator

Settings → Innovonics

| Field | Description |
|-------|-------------|
| Mode | `tcp` (Lantronix/MOXA) or `serial` (direct USB) |
| Host | IP address of the serial-to-IP converter |
| Port | TCP port (default 3000) |
| NID  | Network ID of this coordinator (0–255) |

### Roam Alert

Settings → Roam Alert — add one entry per network controller. Each controller connects on TCP port 10001.

### AeroScout

Settings → AeroScout — configure the ALE server address (default port 1411), username, and password.

### Notification Rules

Settings → Notifications — each rule specifies:
- **Trigger**: on new call, on clear, or both
- **Device filter**: all devices or a specific device
- **Priority filter**: all priorities or a specific level
- **Area filter**: all areas or a specific area
- **Action**: Email, SMS, Telegram, pager, or relay

Template variables available in message text: `{device_name}`, `{location}`, `{priority}`, `{PRIORITY}`, `{area}`, `{apartment}`, `{timestamp}`, `{call_id}`, `{event}`, `{acknowledged_by}`.

### Relays

Settings → Relays — configure RCM or ESP relay controllers. Relays can be assigned at three levels (device → apartment → area); the most specific assignment wins.

### Inputs
Settings → Inputs - Configure ESP based inputs that will trigger alarm events on the system.

### SMTP / Email

Settings → SMTP — configure your mail server. Supports STARTTLS (port 587) and SSL (port 465).

### Twilio SMS

Settings → Twilio — account SID, auth token, from/to numbers.

### Telegram

Settings → Telegram — bot token and chat ID.

---

## Device Management

### Registering Devices

Devices → Add Device, or use **Learn Mode** to capture the serial number automatically when a device transmits.

| Field | Description |
|-------|-------------|
| Device ID | Unique serial number (hex, e.g. `A1B2C3`) |
| Name | Human-readable label shown on dashboard |
| Location | Room or area description |
| Device Type | Pendant, Bath Pull Cord, Bed Pull Cord, Universal TX, Reed Switch, etc. |
| Vendor Type | `innovonics` (standard), `arial_legacy` (Arial pull cords), `arial_900` |
| Priority | Normal, Urgent, Emergency |
| Apartment | Optional — groups the device under an apartment/unit |
| Area | Optional — groups the device under a floor/wing |
| Relay | Optional — dome light relay to activate on alarm |

### Device Types

**Universal TX / Reed Switch**: clears automatically when the input returns to idle (zero-event frame). No tamper required for clear.

**Arial Legacy pull cords**: the tamper signal (cord returning to holder) is used as the clear signal.

**Standard Innovonics**: RESET or ACK button press clears the call.

### Repeaters

Repeaters appear automatically on Devices → Repeaters when they check in. Assign a name and location for identification.

---

## User Management

Settings → Users

| Role | Dashboard | History | Devices | Areas | Messages | Settings |
|------|-----------|---------|---------|-------|----------|----------|
| Admin | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Staff | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Viewer | ✓ | ✓ | — | — | ✓ | — |

All roles can send Staff Emergency alerts.

Default admin account: `admin` / `admin` — **change the password immediately after first login**.

Authentication can be disabled entirely in Settings → Auth for trusted networks where login is not needed.

---

## Push Notifications

Push notifications deliver alerts to browsers and PWAs even when the app is in the background or the screen is locked.

1. Open the app and click the **bell icon** in the top bar
2. Accept the browser permission prompt
3. Done — the bell turns green

Push notifications are sent for every new call. Emergency calls use `requireInteraction: true` (notification stays on screen until dismissed) and a longer vibration pattern on mobile.

The server auto-generates VAPID keys on first use (stored in the database). No external push service account is needed — the browser's built-in push service (FCM for Chrome, APNs-bridged for Safari) handles delivery.

---

## PWA Installation

**Android (Chrome)**
1. Visit `https://<SERVER_IP>` in Chrome
2. Tap the install banner or Menu → Add to Home Screen

**iPhone / iPad (Safari)**
1. Visit `https://<SERVER_IP>` in Safari
2. Share → Add to Home Screen

**Desktop (Chrome/Edge)**
1. Click the install icon in the address bar

---

## SSL Certificate (Local CA)

The system uses a local Certificate Authority rather than Let's Encrypt, so it works on fully air-gapped networks with no internet access.

**How it works:**
- On first boot, nginx generates a CA key + certificate (`ca.crt`) and a server certificate signed by the CA (`cert.pem`)
- The server certificate has `CA:FALSE` (correct for a server cert)
- The CA certificate has `CA:TRUE` and is what you install on devices
- Once the CA is trusted, all future server certificate renewals are silent — nothing to reinstall

**Certificate files** (in `./certs/`):

| File | Purpose |
|------|---------|
| `ca.crt` | Install this on each device (once) |
| `ca.key` | CA private key — keep on server, never share |
| `cert.pem` | Server TLS certificate (used by nginx) |
| `key.pem` | Server private key (used by nginx) |

**Regenerating certificates** (e.g. after changing `SERVER_IP`):
```bash
docker compose down
rm ./certs/*.pem ./certs/*.crt ./certs/*.key ./certs/*.srl ./certs/*.cnf 2>/dev/null
docker compose up -d
# Then reinstall ca.crt on devices
```

---

## Updating

Because the source code is bind-mounted, most changes take effect immediately when uvicorn's `--reload` detects them. Only rebuild when Python dependencies change:

```bash
docker compose build app
docker compose up -d
```

To update nginx (e.g. after changing `nginx.conf` or `entrypoint.sh`):
```bash
docker compose build nginx
docker compose up -d
```

---

## Backup

The entire system state is in two locations:
- `./nurse_call.db` — SQLite database (all configuration, devices, call history)
- `./certs/` — TLS certificates

Back up both regularly. To restore, copy them to the same paths on a new machine and `docker compose up -d`.

---

## Troubleshooting

**Dashboard shows "Reconnecting…"**
The WebSocket connection dropped. The client will reconnect automatically every 3 seconds. Check that the server is running: `docker compose ps`.

**Calls not appearing from Innovonics devices**
- Check Settings → Innovonics — confirm host/port are correct
- Check the coordinator status indicator on the dashboard
- Use the Monitor tab (Devices → Monitor) to see raw frames
- Enable Learn Mode to verify the device is transmitting

**Device not clearing**
- Confirm the Device Type is set correctly (Universal TX / Reed Switch vs pendant)
- Check the Monitor tab for the clear frame (STAT1=00, STAT0=00 for Universal TX)
- For Arial pull cords, ensure Vendor Type is `arial_legacy`

**Push notifications not arriving**
- The browser must have notification permission granted
- The PWA must be installed or the tab must have been opened at least once to register the service worker
- Check that the server can reach the browser's push service (FCM/APNs) — outbound HTTPS on port 443 must be allowed if the device is off-network

**Certificate not installing on Android**
- Download `ca.crt` from `https://<SERVER_IP>/sslcert`
- Go to Settings → Security → Encryption & Credentials → Install a Certificate → **CA Certificate**
- If your phone asks for a private key, you are installing the wrong file — ensure you are downloading `ca.crt`, not `cert.pem`

**Certificate not trusted on Mac after install**
- Open Keychain Access → find "Community Call Local CA" → double-click → Trust → set to **Always Trust** → close → enter password
