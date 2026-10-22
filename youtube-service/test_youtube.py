import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

spec = importlib.util.spec_from_file_location("youtube_under_test", Path(__file__).with_name("app.py"))
y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(y)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(y, "DATA", tmp_path / "youtube")
    monkeypatch.setattr(y, "settings", lambda: {})
    y._metadata.clear(); y._urls.clear(); y._searches.clear()


def cookies(domain=".youtube.com", expires=None):
    return "# Netscape HTTP Cookie File\n" + "\t".join([
        domain, "TRUE", "/", "TRUE", str(expires or int(time.time() + 3600)),
        "__Secure-3PAPISID", "fixture-not-a-real-cookie"]) + "\n"


@pytest.mark.parametrize("raw", ["https://youtube.com/watch?v=abcdefghijk", "../file", "x", "abcdefghijk\n"])
def test_identifier_rejects_untrusted_input(raw):
    with pytest.raises(ValueError): y.video_id(raw)


def test_music_metadata_and_replay_id():
    item = y.normalize({"videoId": "abcdefghijk", "title": "Test", "artists": [{"name": "A"}],
                        "album": {"name": "B"}, "duration": "3:25", "thumbnails": [{"url": "https://i.ytimg.com/a"}]})
    assert item["id"] == "youtube:abcdefghijk" and item["duration_s"] == 205
    assert y.video_id(item["id"]) == "abcdefghijk"
    assert item["source"] == "youtube" and item["lyric"] == ""


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://googlevideo.com.evil.test/x",
    "http://a.googlevideo.com/x", "https://localhost/x", "https://user:pass@a.googlevideo.com/x"])
def test_stream_cannot_fetch_other_hosts(url):
    assert not y.cdn_url(url)


def test_signed_cdn_allowed():
    assert y.cdn_url("https://rr1---sn-test.googlevideo.com/videoplayback?expire=1")


def test_cookie_validation():
    assert len(y.cookie_jar(cookies())) == 1
    for text in [cookies(".google.com"), cookies(".youtube.com.evil.test"), cookies(expires=1),
                 "password=test", cookies().replace("__Secure-3PAPISID", "VISITOR_INFO1_LIVE")]:
        with pytest.raises(ValueError): y.cookie_jar(text)


def test_private_atomic_storage():
    y.atomic_private(y.DATA / "cookies.txt", cookies())
    assert y.DATA.joinpath("cookies.txt").read_text().startswith("# Netscape")
    if os.name != "nt":
        assert (y.DATA.stat().st_mode & 0o777) == 0o700
        assert (y.DATA.joinpath("cookies.txt").stat().st_mode & 0o777) == 0o600


def test_client_session_has_timeout_and_explicit_proxy(monkeypatch):
    monkeypatch.setattr(y, "settings", lambda: {"YOUTUBE_PROXY": "http://proxy.test:3128"})
    s = y._session()
    assert not s.trust_env and s.proxies["https"] == "http://proxy.test:3128"
    assert s.request.keywords["timeout"] == 15
    s.close()


def test_auth_headers_generated_without_collecting_password(monkeypatch):
    y.atomic_private(y.DATA / "cookies.txt", cookies())
    ctor = Mock(); monkeypatch.setattr(y, "YTMusic", ctor)
    with y._session() as session: y.client(session)
    headers = ctor.call_args.kwargs["auth"]
    assert "authorization" in headers and "__Secure-3PAPISID=" in headers["cookie"]
    assert headers["x-origin"] == "https://music.youtube.com"


@pytest.mark.asyncio
async def test_management_auth_import_delete_without_exposing_cookie():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=y.app), base_url="http://test") as c:
        assert (await c.get("/auth")).status_code == 403
        headers = {"X-Youtube-Manage-Key": y.management_key()}
        assert not (await c.get("/auth", headers=headers)).json()["configured"]
        r = await c.put("/auth", headers=headers, json={"cookies": cookies()})
        assert r.status_code == 200
        r = await c.get("/auth", headers=headers)
        assert r.json()["configured"] and "fixture-not-a-real-cookie" not in r.text
        assert (await c.put("/auth", headers=headers, json={"cookies": cookies(".google.com")})).status_code == 400
        assert y.DATA.joinpath("cookies.txt").is_file()
        assert (await c.delete("/auth", headers=headers)).status_code == 200
        assert not y.DATA.joinpath("cookies.txt").exists()


@pytest.mark.asyncio
async def test_errors_do_not_leak_upstream_credentials(monkeypatch):
    def fail(*args): raise RuntimeError("Cookie=SECRET Authorization=SECRET proxypass=SECRET")
    monkeypatch.setattr(y, "search_sync", fail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=y.app), base_url="http://test") as c:
        r = await c.get("/search", params={"keyword": "test"})
        assert r.status_code == 502 and "SECRET" not in r.text


def test_resolver_reuses_short_cache_and_refreshes(monkeypatch):
    count = []
    class FakeDL:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            count.append(url)
            assert not download and self.opts["noplaylist"]
            return {"url": "https://rr1.googlevideo.com/videoplayback?signature=secret", "ext": "m4a"}
    monkeypatch.setattr(y.yt_dlp, "YoutubeDL", FakeDL)
    y.resolve_sync("youtube:abcdefghijk"); y.resolve_sync("youtube:abcdefghijk")
    assert len(count) == 1
    y.resolve_sync("youtube:abcdefghijk", fresh=True)
    assert len(count) == 2


def test_empty_music_catalog_uses_explicit_youtube_audio_fallback(monkeypatch):
    ctor = Mock(return_value=Mock(search=Mock(return_value=[])))
    monkeypatch.setattr(y, "YTMusic", ctor)
    item = {"id": "youtube:abcdefghijk", "source": "youtube", "title": "Fixture"}
    fallback = Mock(return_value=[item])
    monkeypatch.setattr(y, "search_youtube_sync", fallback)
    assert y.search_sync("fixture", 5) == [item]
    assert y._catalog_mode == "youtube_audio_fallback"
    assert y.search_sync("fixture", 5) == [item]
    fallback.assert_called_once_with("fixture", 5)


def test_nonempty_music_catalog_keeps_primary_results(monkeypatch):
    row = {"videoId": "abcdefghijk", "title": "Fixture", "artists": [], "duration_seconds": 60}
    monkeypatch.setattr(y, "YTMusic", Mock(return_value=Mock(search=Mock(return_value=[row]))))
    monkeypatch.setattr(y, "search_youtube_sync", lambda *args: pytest.fail("unnecessary fallback"))
    assert y.search_sync("fixture", 5)[0]["id"] == "youtube:abcdefghijk"
    assert y._catalog_mode == "youtube_music"
