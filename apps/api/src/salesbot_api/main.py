"""SalesBot HTTP API."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from salesbot_api import api_v1, db
from salesbot_api.auth import AuthUser, current_user
from salesbot_api.demo import routes as demo_routes
from salesbot_api.settings import Settings, get_settings
from salesbot_api.voice import tools as voice_tools
from salesbot_api.voice import webhooks as voice_webhooks
from salesbot_core.packs import load_all_packs

app = FastAPI(title="SalesBot API", version="0.1.0")
app.include_router(api_v1.router)
app.include_router(voice_tools.router)
app.include_router(voice_webhooks.router)
# Local demo page (docs/DEMO.md): only with DEMO_MODE=true and APP_ENV=local.
demo_routes.mount(app, get_settings())


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with field names only. The default body echoes the input back, which on the voice
    and intake endpoints can be personal data."""
    fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
    body: dict[str, Any] = {"detail": "invalid request", "fields": fields}
    if request.url.path.startswith("/api/v1/voice/"):
        body.update(ok=False, code="invalid_request")
    return JSONResponse(status_code=422, content=body)


SettingsDep = Annotated[Settings, Depends(get_settings)]


@app.get("/health")
def health(settings: SettingsDep) -> dict[str, str]:
    """Liveness. Never touches the database so it works with no DB configured."""
    return {"status": "ok", "env": settings.app_env}


@app.get("/health/db")
def health_db(settings: SettingsDep) -> dict[str, str]:
    return {"database": db.check(settings)}


@app.get("/packs")
def list_packs() -> list[dict[str, Any]]:
    return [
        {"id": p.id, "name": p.name, "version": p.version, "languages": p.languages}
        for p in load_all_packs().values()
    ]


@app.get("/me")
def me(user: Annotated[AuthUser, Depends(current_user)]) -> dict[str, Any]:
    return {"id": str(user.id), "email": user.email}


# --- Webhooks (stubs). Voice webhooks live in salesbot_api.voice (api_v1 router). ---


@app.post("/webhooks/whatsapp")
def whatsapp_webhook() -> None:
    # TODO: verify signature, record inbound message as a lead touch.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "WhatsApp provider not decided")


@app.get("/integrations/google/callback")
def google_oauth_callback() -> None:
    # TODO: exchange code for tokens, store refresh token server-side per business.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Google Calendar OAuth not built yet")
