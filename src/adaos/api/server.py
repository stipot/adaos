# src\adaos\api\server.py
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, Field
import platform
import time

from adaos.api.auth import require_token
from adaos.sdk.env import get_tts_backend

# Подключаем ваш TTS-адаптер: OVOS или нативный
try:
    from adaos.integrations.ovos_adapter.tts import OVOSTTSAdapter
except Exception:
    OVOSTTSAdapter = None

try:
    from adaos.integrations.rhasspy_adapter.tts import RhasspyTTSAdapter
except Exception:
    RhasspyTTSAdapter = None

from adaos.agent.audio.tts.native_tts import NativeTTS  # ваш нативный
from adaos.api import tool_bridge

app = FastAPI(title="AdaOS API", version="0.1.0")
app.include_router(tool_bridge.router, prefix="/api")  # /api/tools/call


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice: str | None = Field(default=None, description="Опционально: имя/идентификатор голоса")


class SayResponse(BaseModel):
    ok: bool
    duration_ms: int


def _make_tts():
    mode = get_tts_backend()
    if mode == "ovos":
        if OVOSTTSAdapter is None:
            raise RuntimeError("OVOS TTS недоступен (пакеты/конфиг).")
        return OVOSTTSAdapter()
    if mode == "rhasspy":
        if RhasspyTTSAdapter is None:
            raise RuntimeError("Rhasspy TTS недоступен.")
        return RhasspyTTSAdapter()
    return NativeTTS()


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status():
    return {"ok": True, "time": time.time(), "platform": platform.platform(), "python": platform.python_version(), "adaos": app.version}


@app.post("/api/say", response_model=SayResponse, dependencies=[Depends(require_token)])
async def say(payload: SayRequest):
    t0 = time.perf_counter()
    tts = _make_tts()
    tts.say(payload.text)
    dt = int((time.perf_counter() - t0) * 1000)
    return SayResponse(ok=True, duration_ms=dt)
