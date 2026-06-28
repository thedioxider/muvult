import pytest
from src.auth import is_admin


def test_admin_recognized():
    assert is_admin(111, [111, 222]) is True


def test_non_admin():
    assert is_admin(333, [111, 222]) is False


def test_empty_admin_list():
    assert is_admin(111, []) is False
