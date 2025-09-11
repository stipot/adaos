from fastapi import Header, HTTPException, status
from adaos.agent.core.node_config import load_config


def _expected_token() -> str:
    try:
        return load_config().token or "dev-local-token"
    except Exception:
        return "dev-local-token"


async def require_token(
    x_adaos_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """
    Принимаем либо X-AdaOS-Token, либо Authorization: Bearer <token>.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_adaos_token:
        token = x_adaos_token

    if token != _expected_token():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-AdaOS-Token",
        )
