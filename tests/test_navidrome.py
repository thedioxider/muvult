import pytest
import respx
import httpx
from src.navidrome import NavidromeClient

BASE = "http://nd.test:4533"


@pytest.fixture
def client():
    return NavidromeClient(base_url=BASE, user="admin", password="secret", music_path="/muvult")


def _mock_auth():
    """Every request goes through _auth_header, which logs in first."""
    respx.post(f"{BASE}/auth/login").mock(
        return_value=httpx.Response(200, json={"token": "tok"})
    )


@pytest.mark.asyncio
async def test_create_library(client):
    with respx.mock:
        _mock_auth()
        # create_library lists existing libraries first, then creates.
        respx.get(f"{BASE}/api/library").mock(return_value=httpx.Response(200, json=[]))
        respx.post(f"{BASE}/api/library").mock(
            return_value=httpx.Response(200, json={"id": 7, "name": "alice", "path": "/muvult/alice"})
        )
        lib_id = await client.create_library("alice")
    assert lib_id == 7


@pytest.mark.asyncio
async def test_create_library_existing(client):
    with respx.mock:
        _mock_auth()
        # Existing library with the target path is reused, no POST.
        respx.get(f"{BASE}/api/library").mock(
            return_value=httpx.Response(200, json=[{"id": 3, "path": "/muvult/alice"}])
        )
        lib_id = await client.create_library("alice")
    assert lib_id == 3


@pytest.mark.asyncio
async def test_delete_library(client):
    with respx.mock:
        _mock_auth()
        respx.delete(f"{BASE}/api/library/7").mock(return_value=httpx.Response(200))
        await client.delete_library(7)


@pytest.mark.asyncio
async def test_get_user(client):
    with respx.mock:
        _mock_auth()
        respx.get(f"{BASE}/api/user").mock(
            return_value=httpx.Response(200, json=[
                {"id": "nd-uid-1", "userName": "alice"},
                {"id": "nd-uid-2", "userName": "bob"},
            ])
        )
        user = await client.get_user("alice")
    assert user["id"] == "nd-uid-1"


@pytest.mark.asyncio
async def test_get_user_not_found(client):
    with respx.mock:
        _mock_auth()
        respx.get(f"{BASE}/api/user").mock(
            return_value=httpx.Response(200, json=[{"id": "nd-uid-1", "userName": "bob"}])
        )
        with pytest.raises(ValueError, match="alice"):
            await client.get_user("alice")


@pytest.mark.asyncio
async def test_set_user_library(client):
    with respx.mock:
        _mock_auth()
        respx.put(f"{BASE}/api/user/nd-uid-1/library").mock(return_value=httpx.Response(200, json=[]))
        await client.set_user_library("nd-uid-1", 7)


@pytest.mark.asyncio
async def test_update_library(client):
    with respx.mock:
        _mock_auth()
        respx.put(f"{BASE}/api/library/7").mock(return_value=httpx.Response(200, json={}))
        await client.update_library(7, "alice-new")
