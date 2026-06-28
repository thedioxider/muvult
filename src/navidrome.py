import httpx


class NavidromeClient:
    def __init__(self, base_url: str, user: str, password: str, music_path: str):
        self._base = base_url.rstrip("/")
        self._auth = (user, password)
        self._music_path = music_path.rstrip("/")

    async def create_library(self, username: str) -> int:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._base}/api/library",
                json={"name": username, "path": f"{self._music_path}/{username}"},
                auth=self._auth,
            )
            r.raise_for_status()
            return r.json()["id"]

    async def delete_library(self, library_id: int) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.delete(f"{self._base}/api/library/{library_id}", auth=self._auth)
            r.raise_for_status()

    async def update_library(self, library_id: int, username: str) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{self._base}/api/library/{library_id}",
                json={"name": username, "path": f"{self._music_path}/{username}"},
                auth=self._auth,
            )
            r.raise_for_status()

    async def get_user_id(self, username: str) -> str:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._base}/api/user", auth=self._auth)
            r.raise_for_status()
            for u in r.json():
                if u.get("userName") == username:
                    return u["id"]
        raise ValueError(f"Navidrome user not found: {username}")

    async def set_user_library(self, navidrome_user_id: str, library_id: int) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{self._base}/api/user/{navidrome_user_id}/library",
                json={"libraryIds": [library_id]},
                auth=self._auth,
            )
            r.raise_for_status()
