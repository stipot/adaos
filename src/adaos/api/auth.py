from fastapi import Header, HTTPException, status
import os

ADAOS_TOKEN = os.getenv("ADAOS_TOKEN", "dev-local-token")


async def require_token(x_adaos_token: str | None = Header(default=None)) -> None:
    if not ADAOS_TOKEN:
        # Если токен не задан — считаем открытую установку, но лучше всегда задавать
        return
    if x_adaos_token != ADAOS_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-AdaOS-Token",
        )
