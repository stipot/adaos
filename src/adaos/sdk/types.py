from typing import Any, Awaitable, Callable, Dict, Protocol

Topic = str
Payload = Dict[str, Any]
Handler = Callable[[Payload], Awaitable[Any]]


class ToolFn(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...
