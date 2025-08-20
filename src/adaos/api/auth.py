from fastapi import Header, HTTPException, status
from adaos.agent.core.node_config import load_config
import os


def _expected_token() -> str:
    try:
        return load_config().token or "dev-local-token"
    except Exception:
        return "dev-local-token"


async def require_token(x_adaos_token: str | None = Header(default=None)) -> None:
    if x_adaos_token != _expected_token():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-AdaOS-Token",
        )
