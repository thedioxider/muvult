import pytest
from aiogram.types import InlineKeyboardButton


def test_oversized_callback_data_is_caught():
    # Meta-test for the autouse guard in conftest.py (_enforce_callback_data_limit):
    # proves it actually fires, not just that it's wired in.
    with pytest.raises(AssertionError, match="too long"):
        InlineKeyboardButton(text="x", callback_data="y" * 65)


def test_callback_data_at_limit_is_allowed():
    InlineKeyboardButton(text="x", callback_data="y" * 64)
