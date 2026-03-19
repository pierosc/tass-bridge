import os
import secrets
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
from typing import List, Optional, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

app = FastAPI(title="TASS Calendar Bridge")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


# =========================
# Models
# =========================

class EventAttendee(BaseModel):
    email: str
    optional: bool = False


class CreateEventRequest(BaseModel):
    summary: str = Field(..., min_length=1)
    start_iso: str
    end_iso: str
    description: str = ""
    location: str = ""
    calendar_id: str = "primary"
    timezone: Optional[str] = None
    attendees: List[EventAttendee] = []
    send_updates: str = "all"  # all | externalOnly | none


class UpdateEventRequest(BaseModel):
    summary: Optional[str] = None
    start_iso: Optional[str] = None
    end_iso: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    timezone: Optional[str] = None
    attendees: Optional[List[EventAttendee]] = None
    send_updates: str = "all"  # all | externalOnly | none


class FreeBusyCalendarItem(BaseModel):
    calendar_id: str


class FreeBusyRequest(BaseModel):
    time_min: str
    time_max: str
    time_zone: str = "UTC"
    calendars: List[FreeBusyCalendarItem]


# =========================
# Helpers
# =========================

def require_env():
    missing = []

    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not BASE_URL:
        missing.append("BASE_URL")

    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing env vars: {missing}"
        )


def require_api_key(x_api_key: Optional[str]):
    if not INTERNAL_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="INTERNAL_API_KEY is not configured in server env vars."
        )

    if x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


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


def get_credentials() -> Credentials:
    require_env()

    if not GOOGLE_REFRESH_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Not authorized. Visit /oauth/start once and then set GOOGLE_REFRESH_TOKEN in env vars."
        )

    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    try:
        creds.refresh(GoogleRequest())
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Failed to refresh Google access token: {str(e)}"
        )

    return creds


def get_calendar_service():
    creds = get_credentials()
    return build("calendar", "v3", credentials=creds)


def build_event_body_from_create(payload: CreateEventRequest) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "summary": payload.summary,
        "description": payload.description,
        "location": payload.location,
        "start": {"dateTime": payload.start_iso},
        "end": {"dateTime": payload.end_iso},
        "conferenceData": {
            "createRequest": {
                "requestId": secrets.token_urlsafe(12)
            }
        },
    }

    if payload.timezone:
        body["start"]["timeZone"] = payload.timezone
        body["end"]["timeZone"] = payload.timezone

    if payload.attendees:
        body["attendees"] = [
            {
                "email": a.email,
                "optional": a.optional,
            }
            for a in payload.attendees
        ]

    return body


def build_event_body_from_update(payload: UpdateEventRequest) -> Dict[str, Any]:
    body: Dict[str, Any] = {}

    if payload.summary is not None:
        body["summary"] = payload.summary

    if payload.description is not None:
        body["description"] = payload.description

    if payload.location is not None:
        body["location"] = payload.location

    if payload.start_iso is not None:
        body["start"] = {"dateTime": payload.start_iso}

    if payload.end_iso is not None:
        body["end"] = {"dateTime": payload.end_iso}

    if payload.timezone:
        if "start" in body:
            body["start"]["timeZone"] = payload.timezone
        if "end" in body:
            body["end"]["timeZone"] = payload.timezone

    if payload.attendees is not None:
        body["attendees"] = [
            {
                "email": a.email,
                "optional": a.optional,
            }
            for a in payload.attendees
        ]

    return body


def extract_meet_link(event: Dict[str, Any]) -> Optional[str]:
    cd = event.get("conferenceData", {})
    for ep in cd.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            return ep.get("uri")
    return None


def normalize_event_response(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "eventId": event.get("id"),
        "status": event.get("status"),
        "summary": event.get("summary"),
        "description": event.get("description"),
        "location": event.get("location"),
        "htmlLink": event.get("htmlLink"),
        "meetLink": extract_meet_link(event),
        "start": event.get("start"),
        "end": event.get("end"),
        "calendarId": event.get("organizer", {}).get("email"),
        "attendees": event.get("attendees", []),
    }


def handle_google_http_error(e: HttpError):
    status = getattr(e.resp, "status", 500)

    try:
        error_text = e.content.decode("utf-8") if e.content else str(e)
    except Exception:
        error_text = str(e)

    raise HTTPException(
        status_code=status,
        detail=f"Google API error: {error_text}"
    )


# =========================
# Public utility endpoints
# =========================

@app.get("/")
def root():
    return {
        "service": "tass-bridge",
        "status": "up",
        "baseUrl": BASE_URL
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/privacy")
def privacy():
    return {
        "service": "TASS Calendar Bridge",
        "privacy": "This service accesses Google Calendar only to create and manage events authorized by the account owner."
    }


@app.get("/terms")
def terms():
    return {
        "service": "TASS Calendar Bridge",
        "terms": "Use of this service is restricted to authorized internal TASS workflows."
    }


# =========================
# OAuth endpoints
# Use once to obtain refresh token
# =========================

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

    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth callback failed: {str(e)}")

    creds = flow.credentials

    return JSONResponse(
        {
            "ok": True,
            "message": "Copy this refresh_token and save it as GOOGLE_REFRESH_TOKEN in Render.",
            "refresh_token": creds.refresh_token,
            "has_refresh_token": bool(creds.refresh_token),
            "scopes": list(creds.scopes) if creds.scopes else [],
        }
    )


# =========================
# Internal API endpoints
# =========================

@app.post("/events")
def create_event(
    payload: CreateEventRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    try:
        service = get_calendar_service()
        body = build_event_body_from_create(payload)

        created = service.events().insert(
            calendarId=payload.calendar_id,
            body=body,
            conferenceDataVersion=1,
            sendUpdates=payload.send_updates,
        ).execute()

        return normalize_event_response(created)

    except HttpError as e:
        handle_google_http_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error creating event: {str(e)}")


@app.get("/events/{event_id}")
def get_event(
    event_id: str,
    calendar_id: str = "primary",
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    try:
        service = get_calendar_service()
        event = service.events().get(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()

        return normalize_event_response(event)

    except HttpError as e:
        handle_google_http_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error fetching event: {str(e)}")


@app.patch("/events/{event_id}")
def update_event(
    event_id: str,
    payload: UpdateEventRequest,
    calendar_id: str = "primary",
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    try:
        service = get_calendar_service()
        body = build_event_body_from_update(payload)

        if not body:
            raise HTTPException(status_code=400, detail="No fields provided to update.")

        # Si cambia fecha/hora sin Meet aún, mantenemos o pedimos conferenceDataVersion
        updated = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
            conferenceDataVersion=1,
            sendUpdates=payload.send_updates,
        ).execute()

        return normalize_event_response(updated)

    except HttpError as e:
        handle_google_http_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error updating event: {str(e)}")


@app.delete("/events/{event_id}")
def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    send_updates: str = "all",
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    try:
        service = get_calendar_service()
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates,
        ).execute()

        return {
            "ok": True,
            "eventId": event_id,
            "calendarId": calendar_id,
            "deleted": True,
        }

    except HttpError as e:
        handle_google_http_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error deleting event: {str(e)}")


@app.post("/freebusy")
def freebusy(
    payload: FreeBusyRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    try:
        service = get_calendar_service()

        body = {
            "timeMin": payload.time_min,
            "timeMax": payload.time_max,
            "timeZone": payload.time_zone,
            "items": [{"id": c.calendar_id} for c in payload.calendars],
        }

        result = service.freebusy().query(body=body).execute()
        return result

    except HttpError as e:
        handle_google_http_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error on freebusy query: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)