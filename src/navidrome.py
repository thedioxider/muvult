import time

import httpx

_TOKEN_TTL = 6 * 3600  # seconds


class NavidromeClient:
    def __init__(self, base_url: str, user: str, password: str, music_path: str):
        self._base = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._music_path = music_path.rstrip("/")
        self._token: str | None = None
        self._token_ts: float = 0.0

    async def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() - self._token_ts > _TOKEN_TTL:
            r = await httpx.AsyncClient().post(
                f"{self._base}/auth/login",
                json={"username": self._user, "password": self._password},
            )
            r.raise_for_status()
            self._token = r.json()["token"]
            self._token_ts = time.monotonic()
        return {"X-ND-Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        import logging
        log = logging.getLogger(__name__)
        headers = await self._auth_header()
        async with httpx.AsyncClient() as c:
            r = await c.request(method, f"{self._base}{path}", headers=headers, **kwargs)
            if r.status_code == 401:
                self._token = None
                headers = await self._auth_header()
                r = await c.request(method, f"{self._base}{path}", headers=headers, **kwargs)
            if not r.is_success:
                log.error("Navidrome %s %s → %s: %s", method, path, r.status_code, r.text)
            r.raise_for_status()
            return r

    async def create_library(self, username: str) -> int:
        path = f"{self._music_path}/{username}"
        r = await self._request("GET", "/api/library")
        for lib in r.json():
            if lib.get("path") == path:
                return lib["id"]
        r = await self._request("POST", "/api/library", json={"name": f"{username}'s library", "path": path})
        return r.json()["id"]

    async def delete_library(self, library_id: int) -> None:
        await self._request("DELETE", f"/api/library/{library_id}")

    async def update_library(self, library_id: int, username: str) -> None:
        await self._request("PUT", f"/api/library/{library_id}", json={"name": f"{username}'s library", "path": f"{self._music_path}/{username}"})

    async def get_user(self, username: str) -> dict:
        r = await self._request("GET", "/api/user")
        for u in r.json():
            if u.get("userName") == username:
                return u
        raise ValueError(f"Navidrome user not found: {username}")

    async def set_user_library(self, navidrome_user_id: str, library_id: int) -> None:
        await self._request("PUT", f"/api/user/{navidrome_user_id}/library", json={"libraryIds": [library_id]})
