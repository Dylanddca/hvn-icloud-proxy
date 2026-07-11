import os
import aiohttp
from aiohttp import web

# ─── iCloud Shared Albums proxy ────────────────────────────────────────────
# Reads PUBLIC "Shared Album" links (the ones with "Public Website" enabled).
# This never touches anyone's personal photos or Apple ID — only content a
# model has explicitly published to a public shared-album link.
# Standalone service — no dependency on any Discord bot.

async def _icloud_webstream(token: str):
    """Fetch album metadata + photo list. Apple redirects you to a specific
    partition host on first request — we follow that redirect ourselves."""
    url = f"https://sharedstreams.icloud.com/{token}/sharedstreams/webstream"
    payload = {"streamCtag": None}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
            host = data.get("X-Apple-MMe-Host")
            if host:
                url2 = f"https://{host}/{token}/sharedstreams/webstream"
                async with session.post(url2, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                    data2 = await resp2.json(content_type=None)
                    return data2, host
            return data, "sharedstreams.icloud.com"


async def _icloud_asset_urls(token: str, host: str, checksums: list):
    if not checksums:
        return {}, {"skipped": "no checksums"}
    url = f"https://{host}/{token}/sharedstreams/webasseturls"
    payload = {"photoGuids": checksums}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
            return data.get("items", {}), {"status": resp.status, "raw": data}


async def fetch_icloud_album(token: str):
    """Returns {'albumName': str, 'ctag': str, 'photos': [ {guid, caption,
    created, isVideo, url, checksum}, ... ]} for a public shared-album token."""
    stream_data, host = await _icloud_webstream(token)
    photos = stream_data.get("photos", [])

    best_checksum_by_guid = {}
    for p in photos:
        derivatives = p.get("derivatives") or {}
        if not derivatives:
            continue
        best = max(derivatives.values(), key=lambda d: int(d.get("fileSize") or 0))
        best_checksum_by_guid[p["photoGuid"]] = best.get("checksum")

    checksums_requested = [p["photoGuid"] for p in photos]
    items, raw_asset_response = await _icloud_asset_urls(token, host, checksums_requested)

    photos_out = []
    for p in photos:
        guid = p.get("photoGuid")
        checksum = best_checksum_by_guid.get(guid)
        item = items.get(checksum, {}) if checksum else {}
        item_host = item.get("url_host") or item.get("host")
        url_path = item.get("url_path")
        full_url = f"https://{item_host}{url_path}" if item_host and url_path else None
        photos_out.append({
            "guid": guid,
            "checksum": checksum,
            "caption": p.get("caption", ""),
            "created": p.get("dateCreated") or p.get("batchDateCreated"),
            "isVideo": (p.get("mediaAssetType") == "video"),
            "url": full_url,
        })
    # Newest first
    photos_out.sort(key=lambda x: x.get("created") or "", reverse=True)
    return {
        "albumName": stream_data.get("streamName", ""),
        "ctag": stream_data.get("streamCtag", ""),
        "photos": photos_out,
        "_debug": {
            "host": host,
            "checksums_requested": checksums_requested,
            "items_raw": items,
            "raw_asset_response": raw_asset_response,
            "first_photo_raw": photos[0] if photos else None,
        },
    }


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


async def handle_icloud_album(request: web.Request):
    token = request.match_info.get("token", "")
    if not token:
        return web.json_response({"error": "missing token"}, status=400, headers=_cors_headers())
    try:
        result = await fetch_icloud_album(token)
        return web.json_response(result, headers=_cors_headers())
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502, headers=_cors_headers())


async def handle_icloud_options(request: web.Request):
    return web.Response(status=204, headers=_cors_headers())


async def handle_health(request: web.Request):
    return web.json_response({"status": "ok", "service": "icloud-shared-album-proxy"})


def build_app():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/icloud/album/{token}", handle_icloud_album)
    app.router.add_route("OPTIONS", "/icloud/album/{token}", handle_icloud_options)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(build_app(), host="0.0.0.0", port=port)
