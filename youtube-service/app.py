"""YouTube Music source. Credentials stay in sources-data, never in track URLs.

Metadata uses ytmusicapi; playable audio uses maintained yt-dlp extractors.
No password collection, OAuth impersonation, or bypass of account entitlements.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from ytmusicapi import YTMusic, OAuthCredentials
from ytmusicapi.exceptions import YTMusicError
import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from proxy.env_merge import parse_env_file

app = FastAPI(docs_url=None, redoc_url=None)
DATA = Path(os.environ.get("YOUTUBE_DATA_DIR") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or str(ROOT / "sources-data"), "youtube"))
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_lock = threading.RLock()
_key_lock = threading.Lock()
_jobs = asyncio.Semaphore(2)
_metadata: dict[str, dict] = {}
_urls: dict[str, tuple[float, dict]] = {}
_searches: dict[str, tuple[float, list]] = {}
_catalog_mode = "youtube_music"
_oauth_lock = threading.RLock()
_oauth_pending: dict = {}
_oauth_search_backoff = 0.0
_lyric_cache: dict = {}


def oauth_read() -> dict:
    path = DATA / "oauth.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def oauth_write(value: dict):
    atomic_private(DATA / "oauth.json", json.dumps(value))


def google_post(session, endpoint: str, data: dict) -> dict:
    # Both endpoints are fixed; never accept a token/verification URL from users.
    response = session.post("https://oauth2.googleapis.com/" + endpoint, data=data)
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("Invalid OAuth response")
    if response.status_code >= 400 and not result.get("error"):
        raise ValueError("OAuth request failed")
    return result


def oauth_status_sync() -> dict:
    with _oauth_lock:
        config = oauth_read()
        pending = _oauth_pending if _oauth_pending.get("expires_at", 0) > time.time() else {}
        return {"ok": True, "client_id": config.get("client_id", ""),
                "client_configured": bool(config.get("client_id") and config.get("client_secret")),
                "authorized": bool(config.get("token", {}).get("refresh_token")),
                "expires_at": config.get("token", {}).get("expires_at"),
                "pending": bool(pending), "user_code": pending.get("user_code"),
                "verification_url": "https://www.google.com/device" if pending else None,
                "interval": pending.get("interval", 5),
                "device_expires_at": pending.get("expires_at"),
                "playback_requires_cookies": True}


def oauth_token(session):
    with _oauth_lock:
        config = oauth_read()
        token = config.get("token")
        if not token:
            return None
        if token.get("expires_at", 0) < time.time() + 120:
            result = google_post(session, "token", {"client_id": config["client_id"],
                "client_secret": config["client_secret"], "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"]})
            if not result.get("access_token"):
                raise ValueError("OAuth refresh failed; authorize again")
            token.update({k: result[k] for k in ("access_token", "refresh_token", "scope", "token_type", "expires_in") if k in result})
            token["expires_at"] = int(time.time()) + int(result["expires_in"])
            oauth_write(config)
        return dict(token), OAuthCredentials(config["client_id"], config["client_secret"], session=session)


def oauth_action(action: str, values: dict) -> dict:
    with _oauth_lock:
        config = oauth_read()
        if action == "config":
            client_id = str(values.get("client_id", "")).strip()
            secret = str(values.get("client_secret", "")).strip()
            if not client_id.endswith(".apps.googleusercontent.com") or len(client_id) > 512:
                raise ValueError("Use a Google TV / limited-input-device client ID")
            if not secret and client_id == config.get("client_id"):
                secret = config.get("client_secret", "")
            if not secret or len(secret) > 1024 or any(c in secret + client_id for c in "\r\n\x00"):
                raise ValueError("Client ID and secret are required")
            if (client_id, secret) != (config.get("client_id"), config.get("client_secret")):
                config = {"client_id": client_id, "client_secret": secret}
                oauth_write(config)
                _oauth_pending.clear()
        elif action == "logout":
            # Forget local tokens; revocation remains available in Google Account.
            config.pop("token", None)
            oauth_write(config)
            _oauth_pending.clear()
        elif action == "start":
            if not config.get("client_id") or not config.get("client_secret"):
                raise ValueError("Configure your Google OAuth client first")
            with _session() as session:
                result = google_post(session, "device/code", {"client_id": config["client_id"],
                    "scope": "https://www.googleapis.com/auth/youtube"})
            if not all(result.get(k) for k in ("device_code", "user_code", "expires_in")):
                raise ValueError("Google rejected the device request; check client type and API configuration")
            _oauth_pending.clear()
            _oauth_pending.update(device_code=result["device_code"], user_code=result["user_code"],
                expires_at=time.time() + min(int(result["expires_in"]), 1800),
                interval=max(5, int(result.get("interval", 5))), next_poll=0)
        elif action == "poll":
            if _oauth_pending.get("expires_at", 0) <= time.time():
                _oauth_pending.clear()
                raise ValueError("Device code expired; start authorization again")
            if time.monotonic() < _oauth_pending.get("next_poll", 0):
                return oauth_status_sync()
            _oauth_pending["next_poll"] = time.monotonic() + _oauth_pending["interval"]
            with _session() as session:
                result = google_post(session, "token", {"client_id": config["client_id"],
                    "client_secret": config["client_secret"], "device_code": _oauth_pending["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            error = result.get("error")
            if error == "slow_down":
                _oauth_pending["interval"] += 5
                _oauth_pending["next_poll"] = time.monotonic() + _oauth_pending["interval"]
            elif error == "authorization_pending":
                pass
            elif error:
                _oauth_pending.clear()
                raise ValueError("Google declined authorization or the device code expired")
            else:
                if not all(result.get(k) for k in ("access_token", "refresh_token", "expires_in")):
                    raise ValueError("Incomplete OAuth token response")
                config["token"] = {k: result[k] for k in ("access_token", "refresh_token", "expires_in", "token_type", "scope") if k in result}
                config["token"].setdefault("scope", "https://www.googleapis.com/auth/youtube")
                config["token"].setdefault("token_type", "Bearer")
                config["token"]["expires_at"] = int(time.time()) + int(result["expires_in"])
                oauth_write(config)
                _oauth_pending.clear()
        else:
            raise ValueError("Unknown OAuth action")
        with _lock:
            _searches.clear()
        return oauth_status_sync()


def settings() -> dict:
    fallback = Path("/repo/.env") if Path("/repo/.env").exists() else ROOT / ".env"
    values, _ = parse_env_file(Path(os.environ.get("FNMUSIC_ENV_FILE", fallback)))
    return dict(values)


def proxy_url() -> str:
    value = settings().get("YOUTUBE_PROXY", "").strip()
    if value and (urlsplit(value).scheme not in ("http", "https", "socks5", "socks5h")
                  or not urlsplit(value).hostname or any(c in value for c in "\r\n\x00")):
        raise ValueError("invalid YouTube proxy")
    return value


def atomic_private(path: Path, text: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    fd, tmp = tempfile.mkstemp(dir=DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def cookie_jar(text: str) -> http.cookiejar.MozillaCookieJar:
    if len(text.encode()) > 262144 or not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValueError("Upload a Netscape cookies.txt exported for youtube.com (max 256 KiB)")
    # The parser validates structure; inspect all domains before persisting anything.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cookies.txt"
        path.write_text(text, encoding="utf-8")
        jar = http.cookiejar.MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=False)
    if not jar or len(jar) > 500:
        raise ValueError("No unexpired YouTube cookies")
    if not any(c.name == "__Secure-3PAPISID" for c in jar):
        raise ValueError("Missing signed-in YouTube account cookie")
    for c in jar:
        domain = c.domain.lstrip(".").lower()
        if domain != "youtube.com" and not domain.endswith(".youtube.com"):
            raise ValueError("Only youtube.com cookies are accepted; export this site only")
        if any(ch in c.name + c.value for ch in "\r\n\x00"):
            raise ValueError("Invalid cookie value")
    return jar


def _session() -> requests.Session:
    session = requests.Session()
    from functools import partial
    session.request = partial(session.request, timeout=15)
    session.trust_env = False
    proxy = proxy_url()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def client(session: requests.Session, use_oauth: bool = True) -> YTMusic:
    oauth = oauth_token(session) if use_oauth else None
    if oauth:
        auth, credentials = oauth
        return YTMusic(auth=auth, oauth_credentials=credentials, requests_session=session)
    path = DATA / "cookies.txt"
    auth = None
    if path.is_file():
        jar = cookie_jar(path.read_text(encoding="utf-8"))
        cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        from ytmusicapi.helpers import get_authorization
        sapisid = next((c.value for c in jar if c.name == "SAPISID"),
                       next(c.value for c in jar if c.name == "__Secure-3PAPISID"))
        auth = {"authorization": get_authorization(sapisid + " https://music.youtube.com"), "cookie": cookie, "x-goog-authuser": "0", "x-origin": "https://music.youtube.com",
                "user-agent": "Mozilla/5.0", "accept": "*/*", "content-type": "application/json"}
    return YTMusic(auth=auth, requests_session=session)


def video_id(raw: str) -> str:
    value = raw.removeprefix("youtube:")
    if not VIDEO_ID.fullmatch(value):
        raise ValueError("Invalid YouTube video ID")
    return value


def normalize(row: dict) -> dict | None:
    vid = row.get("videoId", "")
    if not VIDEO_ID.fullmatch(vid):
        return None
    length = row.get("duration_seconds")
    if length is None:
        try:
            length = 0
            for part in str(row.get("duration") or "0").split(":"):
                length = length * 60 + int(part)
        except ValueError:
            length = 0
    album = row.get("album") or {}
    thumbs = row.get("thumbnails") or []
    cover = thumbs[-1].get("url", "") if thumbs else ""
    return {"id": "youtube:" + vid, "source": "youtube", "title": row.get("title") or vid,
            "artist": ", ".join(a.get("name", "") for a in row.get("artists", [])),
            "album": album.get("name", ""), "duration": length, "duration_s": length,
            "cover_url": cover, "ext": "m4a", "lyric": ""}


def search_sync(keyword: str, limit: int) -> list:
    global _catalog_mode, _oauth_search_backoff
    key = json.dumps([keyword, limit, proxy_url(), (DATA / "cookies.txt").stat().st_mtime_ns
                      if (DATA / "cookies.txt").is_file() else 0])
    with _lock:
        cached = _searches.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    try:
        with _session() as session:
            try:
                rows = client(session, use_oauth=time.monotonic() >= _oauth_search_backoff).search(keyword, filter="songs", limit=limit, ignore_spelling=True)
            except YTMusicError:
                # A granted Google token can still be rejected by Music's
                # private API. Public search can use the separately saved
                # browser session without discarding or altering OAuth tokens.
                if not oauth_status_sync().get("authorized"):
                    raise
                _oauth_search_backoff = time.monotonic() + 600
                rows = client(session, use_oauth=False).search(keyword, filter="songs", limit=limit, ignore_spelling=True)
    except requests.RequestException:
        rows = []
    items = [item for row in rows if (item := normalize(row))][:limit]
    _catalog_mode = "youtube_music"
    if not items:
        # Some guest sessions expose only podcasts on the Music API. Keep the
        # same video IDs and audio resolver, but disclose this catalog fallback.
        items = search_youtube_sync(keyword, limit)
        _catalog_mode = "youtube_audio_fallback"
    with _lock:
        _metadata.update({it["id"]: it for it in items})
        if len(_metadata) > 3000:
            for k in list(_metadata)[:1500]:
                _metadata.pop(k, None)
        if len(_searches) > 100:
            _searches.clear()
        _searches[key] = (time.monotonic() + 120, items)
    return items


def search_youtube_sync(keyword: str, limit: int) -> list:
    opts = {"extract_flat": True, "skip_download": True, "quiet": True,
            "no_warnings": True, "logger": QuietLogger(), "proxy": proxy_url(),
            "socket_timeout": 10, "retries": 1, "extractor_retries": 1,
            "js_runtimes": {"node": {}}, "noplaylist": True}
    with _lock:
        path = DATA / "cookies.txt"
        if path.is_file():
            opts["cookiefile"] = str(path)
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"ytsearch{limit}:{keyword}", download=False)
        if path.is_file():
            os.chmod(path, 0o600)
    items = []
    for row in data.get("entries") or []:
        vid = row.get("id", "")
        if not VIDEO_ID.fullmatch(vid):
            continue
        item = normalize({"videoId": vid, "title": row.get("title") or vid,
            "artists": [{"name": row.get("channel") or row.get("uploader") or ""}],
            "duration_seconds": row.get("duration") or 0, "thumbnails": row.get("thumbnails") or []})
        item["catalog"] = "youtube_audio_fallback"
        items.append(item)
    return items[:limit]


class QuietLogger:
    # yt-dlp diagnostics can contain account data and signed media URLs.
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def cdn_url(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and bool(p.hostname) and p.hostname.endswith(".googlevideo.com") and not p.username


def resolve_sync(raw: str, fresh: bool = False) -> dict:
    vid = video_id(raw)
    proxy = proxy_url()
    key = vid + proxy
    with _lock:
        cached = _urls.get(key)
        if not fresh and cached and cached[0] > time.monotonic():
            return cached[1]
    opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True,
            "skip_download": True, "noplaylist": True, "socket_timeout": 15, "retries": 1,
            "extractor_retries": 1, "logger": QuietLogger(), "proxy": proxy,
            "js_runtimes": {"node": {}}, "cachedir": str(DATA / "extractor-cache")}
    cookiefile = DATA / "cookies.txt"
    # yt-dlp updates the cookie jar. Serialize use with imports/deletion.
    with _lock:
        cached = _urls.get(key)
        if not fresh and cached and cached[0] > time.monotonic():
            return cached[1]
        if cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://music.youtube.com/watch?v=" + vid, download=False)
        if cookiefile.is_file():
            os.chmod(cookiefile, 0o600)
        url = info.get("url", "")
        if not cdn_url(url):
            raise ValueError("YouTube did not return a playable HTTPS audio stream")
        item = {"url": url, "ext": info.get("ext") or "m4a"}
        _urls[key] = (time.monotonic() + 180, item)
        if len(_urls) > 200:
            _urls.pop(next(iter(_urls)))
        _metadata.setdefault("youtube:" + vid, {"id": "youtube:" + vid, "source": "youtube",
            "title": info.get("track") or info.get("title") or vid,
            "artist": info.get("artist") or info.get("uploader") or "",
            "album": info.get("album") or "", "duration_s": info.get("duration") or 0,
            "cover_url": info.get("thumbnail") or "", "ext": item["ext"], "lyric": ""})
        _metadata["youtube:" + vid]["ext"] = item["ext"]
        _metadata["youtube:" + vid]["audio_spec"] = {
            "bitrate": int(float(info["abr"]) * 1000) if info.get("abr") else None,
            "sampleRate": info.get("asr"), "channel": info.get("audio_channels"),
            "codec": info.get("acodec"), "bitDepth": None}
        return item


def lyrics_sync(raw: str) -> dict:
    vid = video_id(raw)
    cached = _lyric_cache.get(vid)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with _session() as session:
        music = client(session, use_oauth=False)
        tab = music.get_watch_playlist(videoId=vid, limit=1).get("lyrics")
        data = None
        if tab:
            try:
                data = music.get_lyrics(tab, timestamps=True)
            except (YTMusicError, requests.RequestException):
                data = music.get_lyrics(tab, timestamps=False)
        text = ""
        if data:
            if data.get("hasTimestamps"):
                lines = []
                for line in data.get("lyrics", []):
                    ms = line.get("start_time", 0) if isinstance(line, dict) else line.start_time
                    lyric = line.get("text", "") if isinstance(line, dict) else line.text
                    seconds = max(0, ms) / 1000
                    lines.append(f"[{int(seconds // 60):02d}:{seconds % 60:05.2f}]{lyric}")
                text = "\n".join(lines)
            else:
                text = str(data.get("lyrics") or "")
        result = {"ok": True, "lyric": text, "timed": bool(data and data.get("hasTimestamps"))}
        if len(_lyric_cache) > 300:
            _lyric_cache.clear()
        _lyric_cache[vid] = (time.monotonic() + (3600 if text else 300), result)
        return result


@app.get("/lyrics")
async def lyrics(id: str):
    try:
        return await asyncio.to_thread(lyrics_sync, id)
    except ValueError:
        raise HTTPException(400, "Invalid video ID")
    except Exception:
        raise HTTPException(502, "Lyrics provider unavailable")


@app.get("/healthz")
async def health():
    return {"ok": True, "service": "youtube", "yt_dlp": yt_dlp.version.__version__,
            "catalog_mode": _catalog_mode}


@app.get("/search")
async def search(keyword: str = Query(min_length=1, max_length=200), limit: int = Query(20, ge=1, le=50)):
    try:
        async with _jobs:
            items = await asyncio.to_thread(search_sync, keyword, limit)
        if items:
            async def warm_first():
                try:
                    await asyncio.to_thread(resolve_sync, items[0]["id"])
                except Exception:
                    pass
            asyncio.create_task(warm_first())
        return {"ok": True, "items": items, "catalog_mode": _catalog_mode}
    except Exception:
        raise HTTPException(502, "YouTube Music search unavailable; check network and account session")


@app.get("/info")
async def info(id: str):
    try:
        vid = video_id(id)
        if "youtube:" + vid not in _metadata:
            async with _jobs:
                await asyncio.to_thread(resolve_sync, id)
        return {"ok": True, **_metadata["youtube:" + vid]}
    except ValueError:
        raise HTTPException(400, "Invalid video ID or unavailable media")
    except Exception:
        raise HTTPException(502, "YouTube media unavailable; check network and session")


@app.get("/stream")
async def stream(request: Request, id: str):
    try:
        video_id(id)
        async with _jobs:
            resolved = await asyncio.to_thread(resolve_sync, id)
        headers = {"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0"}
        if request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        transport = httpx.AsyncClient(proxy=proxy_url() or None, trust_env=False,
            timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15))
        response = None
        for attempt in range(2):
            url = resolved["url"]
            for hop in range(4):
                if not cdn_url(url):
                    raise ValueError("Invalid media redirect")
                response = await transport.send(transport.build_request("GET", url, headers=headers), stream=True)
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                url = response.headers.get("location", "")
                await response.aclose()
            if response.status_code not in (403, 410) or attempt:
                break
            await response.aclose()
            async with _jobs:
                resolved = await asyncio.to_thread(resolve_sync, id, True)
        if response.status_code not in (200, 206, 416):
            raise ValueError("Media server refused stream")
        content_type = response.headers.get("content-type", "audio/mp4")
        if any(x in content_type.lower() for x in ("text/", "json")):
            raise ValueError("Non-audio response")
        out = {k: v for k, v in response.headers.items() if k in
               ("content-length", "content-range", "accept-ranges", "content-type")}
        async def body():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await transport.aclose()
        return StreamingResponse(body(), status_code=response.status_code, headers=out)
    except Exception:
        if 'response' in locals() and response is not None:
            await response.aclose()
        if 'transport' in locals():
            await transport.aclose()
        raise HTTPException(502, "YouTube audio unavailable; refresh session or update the extractor")


# Only the WebUI forwards management requests. This private random key is shared
# via the data volume, not sent to browsers or written into the package.
def management_key() -> str:
    path = DATA / "management.key"
    with _key_lock:
        if not path.exists():
            atomic_private(path, secrets.token_urlsafe(32))
        return path.read_text().strip()


@app.on_event("startup")
async def initialize():
    management_key()


def authorize(request: Request):
    if not secrets.compare_digest(request.headers.get("x-youtube-manage-key", ""), management_key()):
        raise HTTPException(403, "Forbidden")


@app.get("/auth")
async def auth_status(request: Request):
    authorize(request)
    path = DATA / "cookies.txt"
    return {"ok": True, "configured": path.is_file(), "method": "browser-cookies",
            "updated_at": path.stat().st_mtime if path.is_file() else None,
            "catalog_mode": _catalog_mode,
            "extractor_version": yt_dlp.version.__version__}


@app.api_route("/oauth", methods=["GET", "POST"])
async def oauth_manage(request: Request):
    authorize(request)
    if request.method == "GET":
        return await asyncio.to_thread(oauth_status_sync)
    raw = await request.body()
    if len(raw) > 8192:
        raise HTTPException(413, "OAuth request too large")
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError("Expected an object")
        return await asyncio.to_thread(oauth_action, values.get("action", ""), values)
    except ValueError as exc:
        # All our ValueErrors are fixed messages, but upstream JSON errors must
        # never expose a response, secret or token.
        raise HTTPException(400, "OAuth operation failed; check client settings, device expiry and Google consent") from None
    except Exception:
        raise HTTPException(502, "Google OAuth unavailable; check this source's network connection") from None


@app.put("/auth")
async def auth_import(request: Request):
    authorize(request)
    raw = await request.body()
    if len(raw) > 300000:
        raise HTTPException(413, "Cookie file too large")
    try:
        text = json.loads(raw).get("cookies", "")
        def save():
            cookie_jar(text)
            with _lock:
                atomic_private(DATA / "cookies.txt", text)
                _urls.clear()
                _searches.clear()
        await asyncio.to_thread(save)
        return {"ok": True, "configured": True}
    except Exception:
        raise HTTPException(400, "Invalid cookies.txt; export unexpired youtube.com cookies only")


@app.delete("/auth")
async def auth_delete(request: Request):
    authorize(request)
    def clear():
        with _lock:
            (DATA / "cookies.txt").unlink(missing_ok=True)
            _urls.clear()
            _searches.clear()
    await asyncio.to_thread(clear)
    return {"ok": True, "configured": False}
