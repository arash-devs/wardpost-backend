"""
Wardpost backend — Week 2.

Polls Edgegap's Status API for a deployment, translates the response into
Wardpost's platform-agnostic shape, and serves the result to the dashboard
via a small HTTP API. Also serves the dashboard HTML at http://localhost:8000/.

Read this file top-to-bottom — it's structured to be readable in one pass.
"""

# ─── 1. Imports ────────────────────────────────────────────────────────────
# Standard library first, then third-party. This is a Python convention worth
# building the habit of following.
import asyncio                        # for the background polling loop
import logging                        # so we can see what the poller is doing
import os                             # to read environment variables
from contextlib import asynccontextmanager  # to hook startup/shutdown of FastAPI
from datetime import datetime, timezone
from typing import Optional

import httpx                          # modern HTTP client (async, replaces `requests`)
from dotenv import load_dotenv        # loads .env file into environment variables
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


# ─── 2. Config ────────────────────────────────────────────────────────────
# Load the .env file BEFORE reading any env vars.
load_dotenv()

EDGEGAP_TOKEN = os.getenv("EDGEGAP_TOKEN")
EDGEGAP_REQUEST_ID = os.getenv("EDGEGAP_REQUEST_ID")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
EDGEGAP_API_BASE = "https://api.edgegap.com"

# Fail fast if the token is missing. This is the "don't let a broken config
# silently corrupt everything" habit — matters more when it's a client's token.
if not EDGEGAP_TOKEN:
    raise RuntimeError(
        "EDGEGAP_TOKEN is not set. Copy .env.example to .env and fill it in, "
        "or set the env var directly before running."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wardpost")


# ─── 3. In-memory state ────────────────────────────────────────────────────
# For Week 2 we hold state in memory. When we have multiple clients, we'll swap
# this for Redis or a real DB — but the interface (get/set moorings, add log
# entries) will stay the same, which is why we wrap it in a class.
class WardpostState:
    def __init__(self):
        self.moorings: list[dict] = []
        self.log: list[dict] = []
        self.last_updated: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def add_log_entry(self, text: str, severity: str = "info") -> None:
        entry = {
            "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "text": text,
            "severity": severity,
        }
        self.log.insert(0, entry)   # newest first
        self.log = self.log[:20]    # keep only the last 20

state = WardpostState()


# ─── 4. Edgegap client ────────────────────────────────────────────────────
# A single function that knows how to call Edgegap. Isolating this means
# when we add GameLift or a self-hosted platform, we add a sibling function
# — we don't touch the rest of the code.
async def fetch_edgegap_status(request_id: str) -> dict:
    """Call Edgegap's Deployment Status API. Returns the raw JSON."""
    url = f"{EDGEGAP_API_BASE}/v1/status/{request_id}"
    headers = {"Authorization": f"token {EDGEGAP_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()  # raises if status is 4xx/5xx
        return response.json()


# ─── 5. Normalizer ────────────────────────────────────────────────────────
# The most important layer in the whole file. This translates Edgegap's field
# names into Wardpost's own shape. When we plug in a second platform later,
# THIS is the only file that gets a sibling function. The dashboard doesn't
# know or care what platform the data came from.
def normalize_edgegap_deployment(edgegap_data: dict) -> dict:
    """Translate an Edgegap deployment record → Wardpost's mooring shape."""

    status_label = edgegap_data.get("current_status_label", "Unknown")
    running = edgegap_data.get("running", False)
    error = edgegap_data.get("error")

    # Map Edgegap's status vocabulary to Wardpost's three health buckets.
    if error:
        status = "critical"
    elif status_label == "Ready" and running:
        status = "healthy"
    elif status_label in ("Deploying", "Seeking", "Container Boot"):
        status = "warning"
    else:
        status = "critical"

    location = edgegap_data.get("location") or {}
    city = location.get("city") or "Unknown location"
    country = location.get("country") or ""
    elapsed_seconds = edgegap_data.get("elapsed_time") or 0

    return {
        "id": edgegap_data.get("request_id", "unknown"),
        "name": f"{city}, {country}".strip(", "),
        "status": status,
        "statusLabel": status_label,
        "running": running,
        "publicIp": edgegap_data.get("public_ip"),
        "fqdn": edgegap_data.get("fqdn"),
        "ports": edgegap_data.get("ports") or {},
        "uptimeSeconds": elapsed_seconds,
        "note": _build_plain_english_note(status, city, elapsed_seconds, error),
        "platform": "edgegap",   # future: "gamelift", "self-hosted", etc.
    }


def _build_plain_english_note(
    status: str, city: str, elapsed_seconds: int, error: Optional[str]
) -> str:
    """The one-line human summary shown under each mooring. Leading underscore
    is a Python convention meaning 'internal helper, not part of the public API'."""
    if error:
        return f"Error: {error}"
    if status == "healthy":
        minutes = elapsed_seconds // 60
        seconds = elapsed_seconds % 60
        if minutes >= 1:
            return f"Running in {city} for {minutes}m {seconds}s."
        return f"Running in {city} for {seconds}s."
    if status == "warning":
        return f"Booting up in {city}..."
    return "Not currently running."


# ─── 6. Poller ────────────────────────────────────────────────────────────
async def poll_loop():
    """Background task that keeps state fresh. Runs forever until cancelled."""
    if not EDGEGAP_REQUEST_ID:
        log.warning(
            "EDGEGAP_REQUEST_ID not set — poller idle. "
            "Set it in .env and restart to start monitoring."
        )
        return

    log.info(f"Poller starting — watching deployment {EDGEGAP_REQUEST_ID}")

    while True:
        try:
            raw = await fetch_edgegap_status(EDGEGAP_REQUEST_ID)
            mooring = normalize_edgegap_deployment(raw)

            # Detect state transitions so we can log them.
            previous = state.moorings[0] if state.moorings else None
            if previous is None:
                state.add_log_entry(
                    f"Started monitoring {mooring['name']}", severity="info"
                )
            elif previous["status"] != mooring["status"]:
                severity = "info" if mooring["status"] == "healthy" else "warning"
                state.add_log_entry(
                    f"{mooring['name']} → {mooring['statusLabel']}",
                    severity=severity,
                )

            state.moorings = [mooring]
            state.last_updated = datetime.now(timezone.utc)
            state.last_error = None

            log.info(
                f"Poll OK — {mooring['statusLabel']} in {mooring['name']} "
                f"(uptime {mooring['uptimeSeconds']}s)"
            )

        except httpx.HTTPStatusError as e:
            # API returned an error code (401, 404, 429, 5xx, etc.)
            snippet = e.response.text[:100] if e.response.text else ""
            state.last_error = f"HTTP {e.response.status_code}: {snippet}"
            log.error(f"Edgegap API error: {state.last_error}")

            if e.response.status_code == 404:
                # Deployment expired or was terminated — don't spam logs.
                state.add_log_entry(
                    "Deployment no longer exists (terminated or expired).",
                    severity="warning",
                )

        except Exception as e:
            # Network blip, timeout, etc. — log it and keep going.
            state.last_error = str(e)
            log.error(f"Poller error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ─── 7. FastAPI app ───────────────────────────────────────────────────────
# FastAPI's lifespan hook lets us start the poller when the server starts up
# and cancel it cleanly when the server shuts down.
@asynccontextmanager
async def lifespan(app: FastAPI):
    poller_task = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        poller_task.cancel()

app = FastAPI(title="Wardpost", lifespan=lifespan)

# CORS: allow the dashboard to fetch from any origin during dev.
# In production, tighten to just your dashboard's domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ─── 8. API routes ────────────────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    """For humans and monitoring tools to check the server is alive."""
    return {
        "ok": True,
        "monitoring": EDGEGAP_REQUEST_ID or "(no deployment configured)",
        "lastUpdated": state.last_updated.isoformat() if state.last_updated else None,
        "lastError": state.last_error,
    }


@app.get("/api/moorings")
async def get_moorings():
    """All server instances Wardpost is watching, in Wardpost's own shape."""
    return {
        "moorings": state.moorings,
        "lastUpdated": state.last_updated.isoformat() if state.last_updated else None,
    }


@app.get("/api/log")
async def get_log():
    """The Keeper's Log — recent events."""
    return {"log": state.log}


@app.get("/api/beacon")
async def get_beacon():
    """Aggregate health score across all moorings. This is what the big
    circular gauge on the dashboard shows."""
    if not state.moorings:
        return {"score": 0, "status": "no_data", "lastError": state.last_error}

    healthy_count = sum(1 for m in state.moorings if m["status"] == "healthy")
    total = len(state.moorings)
    score = int((healthy_count / total) * 100)

    if score >= 80:
        overall = "holding_steady"
    elif score >= 50:
        overall = "watch_advised"
    else:
        overall = "take_action"

    return {"score": score, "status": overall, "lastError": state.last_error}


# ─── 9. Serve the dashboard HTML ──────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def dashboard():
    return FileResponse("static/index.html")


# ─── 10. Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
