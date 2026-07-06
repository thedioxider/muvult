import pytest
from aiogram.types import InlineKeyboardButton


@pytest.fixture(autouse=True)
def _enforce_callback_data_limit(monkeypatch):
    """Fail any test whose code path constructs an oversized callback_data.

    Telegram rejects callback_data over 64 bytes with BUTTON_DATA_INVALID at
    send time, not at construction -- a keyboard-building function can look
    fine and still blow up in prod once fed a long enough value (bit us once
    with a filename embedded in callback_data). Patching construction here
    means every test that exercises a keyboard-building code path gets this
    check for free, with no per-test assertion needed -- including future
    keyboards nobody remembers to check by hand.
    """
    original_init = InlineKeyboardButton.__init__

    def checked_init(self, **data):
        original_init(self, **data)
        callback_data = data.get("callback_data")
        if callback_data is not None:
            size = len(callback_data.encode())
            assert size <= 64, f"callback_data too long ({size}B): {callback_data!r}"

    monkeypatch.setattr(InlineKeyboardButton, "__init__", checked_init)
    yield
