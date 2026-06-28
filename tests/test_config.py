import pytest


def test_settings_loads_all_required_vars(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test:token")
    monkeypatch.setenv("ADMIN_TG_IDS", "111,222")
    monkeypatch.setenv("ND_URL", "http://localhost:37510")
    monkeypatch.setenv("ND_ADMIN_USER", "admin")
    monkeypatch.setenv("ND_ADMIN_PASS", "secret")
    monkeypatch.setenv("ND_MUSIC_PATH", "/muvult")

    from importlib import import_module, reload
    import src.config as cfg_mod
    reload(cfg_mod)
    s = cfg_mod.Settings()

    assert s.bot_token == "test:token"
    assert s.admin_tg_ids == [111, 222]
    assert s.nd_url == "http://localhost:37510"
    assert s.nd_music_path == "/muvult"
