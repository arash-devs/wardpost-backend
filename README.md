# Wardpost backend — Week 2

The Python service that polls Edgegap's Deployment Status API, normalizes the
response into Wardpost's platform-agnostic shape, and serves it to the
dashboard. This is the beating heart of Wardpost — everything else eventually
sits on top of it.

## What this does

1. Every 30 seconds, polls `GET /v1/status/{request_id}` on Edgegap.
2. Translates Edgegap's fields into Wardpost's own shape (so when we add
   GameLift or another platform later, only one file changes).
3. Detects status transitions and adds them to an in-memory Keeper's Log.
4. Serves the live data at `/api/beacon`, `/api/moorings`, `/api/log`, `/api/health`.
5. Serves the Wardpost dashboard HTML at `/`.

## First-time setup (once)

Everything below assumes you're in the `wardpost-backend/` directory in your
terminal.

**1. Install Python 3.10+** if you don't have it already.
   Check with `python --version`. If it says 3.9 or lower, upgrade.

**2. Create a virtual environment** (isolates dependencies for this project):

```bash
python -m venv .venv
```

**3. Activate the virtual environment.**

Windows PowerShell:
```powershell
.venv\Scripts\Activate.ps1
```
Windows Command Prompt:
```cmd
.venv\Scripts\activate.bat
```
Mac / Linux:
```bash
source .venv/bin/activate
```

You should see `(.venv)` appear at the start of your terminal prompt.
Any time you come back to work on this project, you need to activate the
environment again.

**4. Install dependencies:**

```bash
pip install -r requirements.txt
```

**5. Create your `.env` file:**

```bash
# Mac/Linux:
cp .env.example .env
# Windows:
copy .env.example .env
```

Then open `.env` in a text editor and fill in:
- `EDGEGAP_TOKEN` — from Edgegap dashboard → User Settings → Tokens
- `EDGEGAP_REQUEST_ID` — from your currently-active deployment (see next section)

## Every-time you want to see live data

**1. Deploy something on Edgegap.** Go to app.edgegap.com → Applications →
   wardpost_demo → demo-app-tutorial → Deploy. Grab the `request_id` off the
   deployment detail page.

**2. Update `EDGEGAP_REQUEST_ID` in `.env`** to that new request_id.

**3. Run the backend:**

```bash
python main.py
```

You should see log lines like:
```
09:14:22 │ INFO    │ Poller starting — watching deployment de833cff7ebf
09:14:22 │ INFO    │ Started server process
09:14:23 │ INFO    │ Uvicorn running on http://127.0.0.1:8000
09:14:23 │ INFO    │ Poll OK — Ready in Toronto, Canada (uptime 12s)
```

**4. Open http://localhost:8000 in your browser.** You should see the Wardpost
dashboard rendering real, live data from your Edgegap deployment.

## API endpoints

If you want to inspect the raw data (great for debugging), hit these directly:

- http://localhost:8000/api/health — is the backend alive?
- http://localhost:8000/api/beacon — the aggregate health score
- http://localhost:8000/api/moorings — one entry per monitored deployment
- http://localhost:8000/api/log — recent state-change events

## File map

- `main.py` — the whole backend, structured in ten labeled sections. Read it top to bottom.
- `static/index.html` — the dashboard UI. Plain HTML/JS/CSS, no build step.
- `requirements.txt` — Python dependencies.
- `.env.example` — template for the config. Copy to `.env` and fill in.
- `.env` — your actual config. **Never commit this to git.**

## What happens when the Edgegap deployment expires

The free tier caps deployments at 15 minutes. When yours expires, the poller
will see a 404 from the Status API and log:
```
Deployment no longer exists (terminated or expired).
```

That's expected. To resume: deploy again on Edgegap, update `EDGEGAP_REQUEST_ID`
in `.env`, and restart the backend (`Ctrl+C` then `python main.py`).

## What's next (Week 3)

- Package this + the Wardpost visual as the "Launch Readiness Audit" offer.
- Support multiple deployments per client, not just one.
- Add auth so we can eventually host this for real clients.
