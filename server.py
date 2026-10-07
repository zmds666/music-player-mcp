"""Music Player MCP with an MCP Apps player widget.

It queries a separately configured Netease-compatible API. No account cookie,
API key, music catalogue, lyrics, or hosted audio is included in this project.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
WIDGET_JS_PATH = BASE_DIR / "dist" / "widget" / "music-player-widget.global.js"
NCM_API_BASE_URL = os.getenv("NCM_API_BASE_URL", "http://127.0.0.1:3939").rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:3941").rstrip("/")
NCM_COOKIE_FILE = os.getenv("NCM_COOKIE_FILE", "")
NCM_COOKIE = os.getenv("NCM_COOKIE", "")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("PORT", os.getenv("MCP_PORT", "3941")))
ALLOWED_AUDIO_HOST_SUFFIXES = tuple(
    item.strip().lower()
    for item in os.getenv("ALLOWED_AUDIO_HOST_SUFFIXES", ".music.126.net").split(",")
    if item.strip()
)

MUSIC_VIEW_URI = "ui://music-player/mcp-app-v1.html"
MUSIC_VIEW_MIME = "text/html;profile=mcp-app"
mcp = FastMCP(
    "music-player",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "music-player-mcp-production.up.railway.app",
            "localhost:*",
            "127.0.0.1:*",
        ]
    ),
)


def widget_html() -> str:
    if not WIDGET_JS_PATH.exists():
        return "<!doctype html><html><body><p>Run <code>npm run build:widget</code> first.</p></body></html>"
    js = WIDGET_JS_PATH.read_text(encoding="utf-8")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>:root{color-scheme:light dark}*{box-sizing:border-box}"
        "html,body{margin:0;padding:0;background:transparent;width:100%;height:fit-content;overflow:hidden}"
        "#root{display:block;width:100%}</style></head>"
        f"<body><div id='root'></div><script>{js}</script></body></html>"
    )


WIDGET_META = {
    "openai/outputTemplate": MUSIC_VIEW_URI,
    "ui": {
        "resourceUri": MUSIC_VIEW_URI,
        "csp": {
            "resourceDomains": ["https://*.music.126.net", PUBLIC_BASE_URL],
            "connectDomains": [PUBLIC_BASE_URL],
        },
    },
}


@mcp.resource(MUSIC_VIEW_URI, mime_type=MUSIC_VIEW_MIME, name="music-player", meta={"ui": WIDGET_META["ui"]})
def music_view() -> str:
    return widget_html()


class MusicPayload(BaseModel):
    audioUrl: str
    coverUrl: str = ""
    songName: str = "Unknown track"
    artistName: str = "Unknown artist"
    duration: int = 0
    lyrics: str = ""
    colorPrimary: str = "#6e7c87"
    colorSecondary: str = "#CAE0E8"
    colorBg: str = "#1a1d21"
    colorBgEnd: str = "#2a2d31"


def get_cookie() -> str:
    if NCM_COOKIE:
        return NCM_COOKIE.strip()
    if not NCM_COOKIE_FILE:
        return ""
    try:
        return Path(NCM_COOKIE_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def ncm_get(path: str) -> dict:
    cookie = get_cookie()
    sep = "&" if "?" in path else "?"
    url = f"{NCM_API_BASE_URL}{path}{sep}cookie={quote(cookie, safe='')}" if cookie else f"{NCM_API_BASE_URL}{path}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()


def is_allowed_audio_url(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and any(
        hostname.endswith(suffix) for suffix in ALLOWED_AUDIO_HOST_SUFFIXES
    )


def audio_proxy_url(source_url: str) -> str:
    if not is_allowed_audio_url(source_url):
        raise ValueError("The upstream audio URL is not from an allowed music host.")
    return f"{PUBLIC_BASE_URL}/proxy?url={quote(source_url, safe='')}"


async def get_song_url(song_id: int) -> str | None:
    data = await ncm_get(f"/song/url?id={song_id}&br=128000")
    items = data.get("data", [])
    return audio_proxy_url(items[0]["url"]) if items and items[0].get("url") else None


async def get_song_detail(song_id: int) -> dict:
    data = await ncm_get(f"/song/detail?ids={song_id}")
    songs = data.get("songs", [])
    if not songs:
        return {}
    song = songs[0]
    return {
        "name": song.get("name", "Unknown track"),
        "artist": ", ".join(a.get("name", "") for a in song.get("ar", [])) or "Unknown artist",
        "cover": song.get("al", {}).get("picUrl", ""),
        "duration": song.get("dt", 0) // 1000,
    }


async def get_lyrics(song_id: int) -> str:
    try:
        return (await ncm_get(f"/lyric?id={song_id}")).get("lrc", {}).get("lyric", "")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return ""


async def music_info(song_id: int) -> dict:
    detail = await get_song_detail(song_id)
    if not detail:
        raise ValueError("Track not found.")
    url = await get_song_url(song_id)
    if not url:
        raise ValueError(f"{detail['name']} has no playable URL. It may be restricted.")
    return {"song_id": song_id, "url": url, "lyrics": await get_lyrics(song_id), **detail}


async def search_and_play(keywords: str) -> dict:
    data = await ncm_get(f"/search?keywords={quote(keywords)}&limit=1")
    songs = data.get("result", {}).get("songs", [])
    if not songs:
        raise ValueError(f"No track found for {keywords!r}.")
    return await music_info(int(songs[0]["id"]))


def payload(info: dict, color_primary: str, color_secondary: str, color_bg: str) -> MusicPayload:
    return MusicPayload(
        audioUrl=info["url"], coverUrl=info["cover"], songName=info["name"],
        artistName=info["artist"], duration=info["duration"], lyrics=info["lyrics"],
        colorPrimary=color_primary, colorSecondary=color_secondary, colorBg=color_bg,
        colorBgEnd=color_bg.replace("#1a", "#2a") if color_bg.startswith("#1a") else color_bg,
    )


@mcp.tool(name="play_music", description="Search a track and render a compact in-chat music player.", meta=WIDGET_META)
async def play_music(keywords: str, color_primary: str = "#6e7c87", color_secondary: str = "#CAE0E8", color_bg: str = "#1a1d21") -> MusicPayload:
    return payload(await search_and_play(keywords), color_primary, color_secondary, color_bg)


@mcp.tool(name="play_music_by_id", description="Render a compact in-chat music player for a Netease track ID.", meta=WIDGET_META)
async def play_music_by_id(song_id: int, color_primary: str = "#6e7c87", color_secondary: str = "#CAE0E8", color_bg: str = "#1a1d21") -> MusicPayload:
    return payload(await music_info(song_id), color_primary, color_secondary, color_bg)
async def handle_proxy(request):
    from starlette.responses import Response, StreamingResponse

    target_url = request.query_params.get("url", "")
    if not target_url:
        return Response("Missing url parameter", status_code=400)

    if not is_allowed_audio_url(target_url):
        return Response("Audio host is not allowed", status_code=403)

    headers = {
        "Referer": "https://music.163.com/",
        "User-Agent": "Mozilla/5.0",
    }

    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]

    try:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        )
        upstream = await session.get(target_url, headers=headers)

        if upstream.status not in {200, 206}:
            status = upstream.status
            upstream.close()
            await session.close()
            return Response(
                f"Upstream error: {status}",
                status_code=status,
            )

        response_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=600",
        }

        for header in (
            "Content-Type",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
        ):
            if upstream.headers.get(header):
                response_headers[header] = upstream.headers[header]

        async def stream_audio():
            try:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    yield chunk
            finally:
                upstream.close()
                await session.close()

        return StreamingResponse(
            stream_audio(),
            status_code=upstream.status,
            headers=response_headers,
        )

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        return Response(
            f"Audio proxy error: {error}",
            status_code=502,
        )


app = mcp.streamable_http_app()

from starlette.routing import Route

app.routes.append(Route("/proxy", handle_proxy, methods=["GET"]))


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=MCP_HOST,
        port=MCP_PORT,
)

