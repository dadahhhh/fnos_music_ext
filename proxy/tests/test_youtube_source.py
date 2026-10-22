from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from proxy import app as p


def test_youtube_uses_own_enable_flag(monkeypatch):
    monkeypatch.setitem(p.CONF, "youtube_enabled", True)
    monkeypatch.setitem(p.CONF, "musicdl_enabled", False)
    monkeypatch.setitem(p.CONF, "online_sources", "KuwoMusicClient")
    assert p._source_enabled("online:youtube:abcdefghijk")
    assert p.source_display_prefix("youtube") == "[YouTube Music] "
    monkeypatch.setitem(p.CONF, "youtube_enabled", False)
    assert not p._source_enabled("online:youtube:abcdefghijk")


@pytest.mark.asyncio
async def test_youtube_metadata_does_not_call_musicdl(monkeypatch):
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={
        "ok": True, "id": "youtube:abcdefghijk", "title": "Fixture", "source": "youtube"})))
    monkeypatch.setattr(p, "get_youtube_client", lambda app: client)
    monkeypatch.setattr(p, "get_musicdl_client", lambda app: pytest.fail("wrong source"))
    data = await p._fetch_online_info(SimpleNamespace(app=None), "online:youtube:abcdefghijk")
    assert data["title"] == "Fixture"
    assert client.get.call_args.kwargs["params"]["id"] == "youtube:abcdefghijk"


@pytest.mark.asyncio
async def test_youtube_transcode_uses_internal_relay(monkeypatch):
    monkeypatch.setattr(p, "find_cache_file", lambda guid, **kwargs: None)
    monkeypatch.setitem(p.CONF, "youtube_enabled", True)
    monkeypatch.setitem(p.CONF, "youtube_url", "http://127.0.0.1:8776")
    url, headers = await p._transcode_source(SimpleNamespace(app=None), "online:youtube:abcdefghijk")
    assert url.startswith("http://127.0.0.1:8776/stream?")
    assert "youtube%3Aabcdefghijk" in url and headers is None
