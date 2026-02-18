import os
import secrets
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

app = FastAPI(title="TASS Calendar Bridge")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "")  # https://tass-bridge.onrender.com
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")  # lo pondremos luego
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

def require_env():
    missing = []
    if not GOOGLE_CLIENT_ID: missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET: missing.append("GOOGLE_CLIENT_SECRET")
    if not BASE_URL: missing.append("BASE_URL")
    if missing:
        raise HTTPException(500, detail=f"Missing env vars: {missing}")

@app.get("/")
def root():
    return {"service": "tass-bridge", "status": "up"}

@app.get("/health")
def health():
    return {"ok": True}

def make_flow():
    require_env()
    redirect_uri = f"{BASE_URL}/oauth/callback"

    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

@app.get("/oauth/start")
def oauth_start():
    flow = make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=secrets.token_urlsafe(16),
    )
    return RedirectResponse(auth_url)

@app.get("/oauth/callback")
def oauth_callback(request: Request):
    flow = make_flow()
    flow.fetch_token(authorization_response=str(request.url))

    creds = flow.credentials
    # OJO: esto te devolverá refresh_token la primera vez (con prompt=consent)
    return JSONResponse({
        "ok": True,
        "message": "Copia el refresh_token y guárdalo como env var GOOGLE_REFRESH_TOKEN en Render",
        "refresh_token": creds.refresh_token,
        "note": "No compartas este token. Es equivalente a acceso a tu Calendar.",
    })

def get_credentials() -> Credentials:
    require_env()
    if not GOOGLE_REFRESH_TOKEN:
        raise HTTPException(401, detail="Not authorized. Visit /oauth/start, then set GOOGLE_REFRESH_TOKEN env var.")

    creds = Credentials(
        token=None,  # se renovará
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    # fuerza refresh para obtener access token válido
    creds.refresh(GoogleRequest())
    return creds

@app.post("/events")
def create_event(
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
    calendar_id: str = "primary",
):
    """
    start_iso / end_iso ejemplo:
    2026-02-18T15:00:00-05:00
    """
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)

    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "conferenceData": {
            "createRequest": {
                "requestId": secrets.token_urlsafe(12)
            }
        },
    }

    created = service.events().insert(
        calendarId=calendar_id,
        body=body,
        conferenceDataVersion=1
    ).execute()

    meet_link = None
    cd = created.get("conferenceData", {})
    for ep in cd.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri")
            break

    return {
        "eventId": created.get("id"),
        "htmlLink": created.get("htmlLink"),
        "meetLink": meet_link,
        "status": created.get("status"),
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
