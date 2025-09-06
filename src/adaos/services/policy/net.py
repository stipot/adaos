from __future__ import annotations
from urllib.parse import urlparse


class NetPolicy:
    def __init__(self, allowlist: list[str] | None = None, denylist: list[str] | None = None):
        self.allowset = set((allowlist or []))
        self.denyset = set((denylist or []))

    @staticmethod
    def _host_of(url: str) -> str:
        u = urlparse(url)
        # поддержим ssh-формы git: git@host:org/repo.git
        if not u.scheme and "@" in url and ":" in url:
            host = url.split("@", 1)[1].split(":", 1)[0]
            return host.lower()
        return (u.hostname or "").lower()

    def allow(self, *domains: str) -> None:
        self.allowset.update(d.lower() for d in domains)

    def deny(self, *domains: str) -> None:
        self.denyset.update(d.lower() for d in domains)

    def is_allowed_url(self, url: str) -> bool:
        host = self._host_of(url)
        if not host:  # локальные пути/не-URL — блокируем
            return False
        if host in self.denyset:
            return False
        if not self.allowset:
            return True
        return host in self.allowset

    def require_url(self, url: str) -> None:
        if not self.is_allowed_url(url):
            raise PermissionError(f"net policy: '{url}' is not allowed")
