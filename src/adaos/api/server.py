# src/adaos/api/server.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
import platform, time

from adaos.api.auth import require_token
from adaos.sdk.env import get_tts_backend
from adaos.agent.audio.tts.native_tts import NativeTTS

# наши роутеры
from adaos.api import tool_bridge
from adaos.api import subnet_api
from adaos.agent.core.lifecycle import run_boot_sequence, shutdown, is_ready

app: FastAPI  # объявим ниже


@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
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
    # SHUTDOWN — если нужно:
    # from adaos.agent.core.lifecycle import shutdown
    # await shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    await run_boot_sequence(app)
    yield
    # shutdown
    await shutdown()


app = FastAPI(title="AdaOS API", version="0.1.0", lifespan=lifespan)
app.include_router(tool_bridge.router, prefix="/api")
app.include_router(subnet_api.router, prefix="/api")


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
