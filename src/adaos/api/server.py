# src/adaos/api/server.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import platform, time

from adaos.api.auth import require_token
from adaos.sdk.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS

# наши роутеры
from adaos.api import tool_bridge
from adaos.api import subnet_api
from adaos.api import observe_api
from adaos.api import node_api
from adaos.agent.core.lifecycle import run_boot_sequence, shutdown, is_ready
from adaos.agent.core.observe import start_observer, stop_observer, attach_http_trace_headers
from adaos.api import scenarios

app: FastAPI  # объявим ниже


@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    await start_observer()
    await run_boot_sequence(app)

    # Диагностика: распечатать все маршруты
    print("\n[AdaOS] Mounted routes:")
    for r in app.router.routes:
        try:
            print(" -", r.methods, r.path)
        except Exception:
            pass
    print()

    yield
    # shutdown
    await stop_observer()
    await shutdown()


app = FastAPI(title="AdaOS API", version="0.1.0", lifespan=lifespan)
app.include_router(tool_bridge.router, prefix="/api")
app.include_router(subnet_api.router, prefix="/api")
app.include_router(node_api.router, prefix="/api/node")
app.include_router(observe_api.router, prefix="/api/observe")
app.include_router(scenarios.router, prefix="/api/scenarios")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200", "*"],  # под dev и/или произвольный origin
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-AdaOS-Token"],
    allow_credentials=False,  # токен идёт в заголовке, куки не нужны
)

# --- базовые эндпоинты (для проверки, что всё живо) ---


@app.get("/api/ping")
async def ping():
    return {"ok": True, "ts": time.time()}


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice: str | None = Field(default=None, description="Опционально: имя/идентификатор голоса")


class SayResponse(BaseModel):
    ok: bool
    duration_ms: int


def _make_tts():
    mode = get_tts_backend()
    # можно расширить OVOS/Rhasspy позже
    return NativeTTS()


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status():
    return {
        "ok": True,
        "time": time.time(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "adaos": app.version,
    }


@app.post("/api/say", response_model=SayResponse, dependencies=[Depends(require_token)])
async def say(payload: SayRequest):
    t0 = time.perf_counter()
    _make_tts().say(payload.text)
    dt = int((time.perf_counter() - t0) * 1000)
    return SayResponse(ok=True, duration_ms=dt)


# --- health endpoints (без авторизации; удобно для оркестраторов/проб) ---
@app.get("/health/live")
async def health_live():
    return {"ok": True}


@app.get("/health/ready")
async def health_ready():
    # 200 только когда прошёл boot sequence
    if not is_ready():
        raise HTTPException(status_code=503, detail="not ready")
    return {"ok": True}
