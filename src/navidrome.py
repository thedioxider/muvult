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
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = await self._auth_header()
        async with httpx.AsyncClient() as c:
            r = await c.request(method, f"{self._base}{path}", headers=headers, **kwargs)
            if r.status_code == 401:
                self._token = None
                headers = await self._auth_header()
                r = await c.request(method, f"{self._base}{path}", headers=headers, **kwargs)
            r.raise_for_status()
            return r

    async def create_library(self, username: str) -> int:
        r = await self._request("POST", "/api/library", json={"name": username, "path": f"{self._music_path}/{username}"})
        return r.json()["id"]

    async def delete_library(self, library_id: int) -> None:
        await self._request("DELETE", f"/api/library/{library_id}")

    async def update_library(self, library_id: int, username: str) -> None:
        await self._request("PUT", f"/api/library/{library_id}", json={"name": username, "path": f"{self._music_path}/{username}"})

    async def get_user_id(self, username: str) -> str:
        r = await self._request("GET", "/api/user")
        for u in r.json():
            if u.get("userName") == username:
                return u["id"]
        raise ValueError(f"Navidrome user not found: {username}")

    async def set_user_library(self, navidrome_user_id: str, library_id: int) -> None:
        await self._request("PUT", f"/api/user/{navidrome_user_id}/library", json={"libraryIds": [library_id]})
