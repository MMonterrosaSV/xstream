import os
import time
import re
import gzip
import io
from typing import Optional, Dict, List, Tuple, Any
from urllib.parse import urljoin, urlparse, parse_qs
from xml.etree import ElementTree as ET
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
import httpx

app = FastAPI(title="Simple M3U → Xtream API (Live + Movies + Series + TMDB + Multi-EPG)")

# === CONFIG ===
_raw_live = os.getenv("M3U_URLS", os.getenv("M3U_URL", "https://example.com/your-playlist.m3u"))
LIVE_M3U_URLS = [u.strip() for u in _raw_live.split(",") if u.strip()]

MOVIES_M3U_URL = os.getenv(
    "MOVIES_M3U_URL",
    "https://raw.githubusercontent.com/MMonterrosaSV/IPTV/refs/heads/main/MOVIES"
)
SERIES_M3U_URL = os.getenv(
    "SERIES_M3U_URL",
    "https://raw.githubusercontent.com/MMonterrosaSV/IPTV/refs/heads/main/TV%20SHOWS"
)

USERNAME = os.getenv("XTREAM_USER", "demo")
PASSWORD = os.getenv("XTREAM_PASS", "demo")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "60"))
SERVER_URL = os.getenv("SERVER_URL", "your-render-url.onrender.com")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

# Multi-EPG sources (comma-separated). Supports .xml and .xml.gz
_raw_epg = os.getenv("EPG_URLS", "")
EPG_URLS = [u.strip() for u in _raw_epg.split(",") if u.strip()]
EPG_CACHE_SECONDS = int(os.getenv("EPG_CACHE_SECONDS", "3600"))

UPSTREAM_HEADERS = {
    "User-Agent": os.getenv("UPSTREAM_USER_AGENT", "VLC/3.0.20 LibVLC/3.0.20"),
}

TMDB_IMG = "https://image.tmdb.org/t/p"

_cache = {
    "live_channels": [],
    "live_categories": [],
    "vod_streams": [],
    "vod_categories": [],
    "series_list": [],
    "series_categories": [],
    "series_episodes": {},
    "fetched_at": 0,
}

_epg_cache = {
    "channels": {},      # channel_id -> {"id", "display_name", "icon", "normalized"}
    "programmes": [],    # list of dicts
    "by_channel": {},    # channel_id -> list of programmes (sorted by start)
    "fetched_at": 0,
}

_tmdb_movie_cache: Dict[str, dict] = {}
_tmdb_series_cache: Dict[str, dict] = {}


def _get_category_id(group_name: str, category_ids: dict, categories: list) -> str:
    if not group_name:
        group_name = "Uncategorized"
    if group_name not in category_ids:
        new_id = str(len(category_ids) + 1)
        category_ids[group_name] = new_id
        categories.append({
            "category_id": new_id,
            "category_name": group_name,
            "parent_id": 0,
        })
    return category_ids[group_name]


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r"\b(hd|fhd|uhd|4k|sd|hevc|h265|h264)\b", "", n)
    n = re.sub(r"\[.*?\]|\(.*?\)", "", n)
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _extract_movie_ids(url: str, name: str) -> Tuple[Optional[str], Optional[str]]:
    imdb_id = None
    tmdb_id = None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        raw = (qs.get("url") or [None])[0]
        if raw:
            raw = raw.strip()
            if re.match(r"^tt\d+$", raw, re.I):
                imdb_id = raw.lower()
            elif raw.isdigit():
                tmdb_id = raw
    except Exception:
        pass
    if not imdb_id:
        m = re.search(r"(tt\d+)", name, re.I)
        if m:
            imdb_id = m.group(1).lower()
    return imdb_id, tmdb_id


def _extract_series_id_from_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        raw = (qs.get("url") or [None])[0]
        if raw:
            parts = raw.strip().split("/")
            if parts and parts[0].isdigit():
                return parts[0]
    except Exception:
        pass
    return None


def _tmdb_get(path: str, params: dict = None) -> Optional[dict]:
    if not TMDB_API_KEY:
        return None
    params = params or {}
    params["api_key"] = TMDB_API_KEY
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.get(f"https://api.themoviedb.org/3{path}", params=params)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


def _fetch_movie_metadata(imdb_id: Optional[str] = None, tmdb_id: Optional[str] = None) -> Optional[dict]:
    cache_key = imdb_id or (f"tmdb:{tmdb_id}" if tmdb_id else None)
    if cache_key and cache_key in _tmdb_movie_cache:
        return _tmdb_movie_cache[cache_key]

    movie_id = None
    if imdb_id:
        data = _tmdb_get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        if data and data.get("movie_results"):
            movie_id = data["movie_results"][0]["id"]
    elif tmdb_id:
        movie_id = tmdb_id

    if not movie_id:
        return None

    details = _tmdb_get(
        f"/movie/{movie_id}",
        {"append_to_response": "credits,videos", "language": "en-US"}
    )
    if not details:
        return None

    poster = f"{TMDB_IMG}/w500{details['poster_path']}" if details.get("poster_path") else ""
    poster_big = f"{TMDB_IMG}/original{details['poster_path']}" if details.get("poster_path") else ""
    backdrop = f"{TMDB_IMG}/original{details['backdrop_path']}" if details.get("backdrop_path") else ""

    genres_list = [g["name"] for g in details.get("genres", [])]
    primary_genre = genres_list[0] if genres_list else ""
    genres = ", ".join(genres_list)

    director = ""
    cast_list = []
    credits = details.get("credits") or {}
    for person in credits.get("crew", []):
        if person.get("job") == "Director":
            director = person.get("name", "")
            break
    for person in credits.get("cast", [])[:8]:
        cast_list.append(person.get("name", ""))
    cast = ", ".join(cast_list)

    trailer = ""
    for v in (details.get("videos") or {}).get("results", []):
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            trailer = v.get("key", "")
            break

    runtime = details.get("runtime") or 0
    rating = round(details.get("vote_average") or 0, 1)

    meta = {
        "tmdb_id": str(details.get("id", "")),
        "imdb_id": details.get("imdb_id") or imdb_id or "",
        "name": details.get("title") or details.get("original_title") or "",
        "o_name": details.get("original_title") or "",
        "movie_image": poster,
        "cover_big": poster_big,
        "backdrop_path": [backdrop] if backdrop else [],
        "plot": details.get("overview") or "",
        "description": details.get("overview") or "",
        "releasedate": details.get("release_date") or "",
        "genre": genres,
        "primary_genre": primary_genre,
        "director": director,
        "actors": cast,
        "cast": cast,
        "rating": str(rating),
        "rating_5based": round(rating / 2, 1),
        "duration_secs": runtime * 60,
        "duration": f"{runtime // 60:02d}:{runtime % 60:02d}:00" if runtime else "00:00:00",
        "episode_run_time": str(runtime),
        "youtube_trailer": trailer,
        "country": ", ".join(c["name"] for c in details.get("production_countries", [])),
        "age": "",
        "status": details.get("status") or "",
    }

    if cache_key:
        _tmdb_movie_cache[cache_key] = meta
    if meta["imdb_id"]:
        _tmdb_movie_cache[meta["imdb_id"]] = meta
    if meta["tmdb_id"]:
        _tmdb_movie_cache[f"tmdb:{meta['tmdb_id']}"] = meta

    return meta


def _fetch_series_metadata(tmdb_id: str) -> Optional[dict]:
    cache_key = f"tmdb:{tmdb_id}"
    if cache_key in _tmdb_series_cache:
        return _tmdb_series_cache[cache_key]

    details = _tmdb_get(
        f"/tv/{tmdb_id}",
        {"append_to_response": "credits,videos", "language": "en-US"}
    )
    if not details:
        return None

    poster = f"{TMDB_IMG}/w500{details['poster_path']}" if details.get("poster_path") else ""
    poster_big = f"{TMDB_IMG}/original{details['poster_path']}" if details.get("poster_path") else ""
    backdrop = f"{TMDB_IMG}/original{details['backdrop_path']}" if details.get("backdrop_path") else ""

    genres_list = [g["name"] for g in details.get("genres", [])]
    primary_genre = genres_list[0] if genres_list else ""
    genres = ", ".join(genres_list)

    cast_list = []
    credits = details.get("credits") or {}
    for person in credits.get("cast", [])[:8]:
        cast_list.append(person.get("name", ""))
    cast = ", ".join(cast_list)

    created_by = ", ".join(c.get("name", "") for c in details.get("created_by", [])[:3])

    trailer = ""
    for v in (details.get("videos") or {}).get("results", []):
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            trailer = v.get("key", "")
            break

    rating = round(details.get("vote_average") or 0, 1)
    runtime = (details.get("episode_run_time") or [0])[0] if details.get("episode_run_time") else 0

    meta = {
        "tmdb_id": str(details.get("id", "")),
        "name": details.get("name") or details.get("original_name") or "",
        "cover": poster,
        "cover_big": poster_big,
        "backdrop_path": [backdrop] if backdrop else [],
        "plot": details.get("overview") or "",
        "cast": cast,
        "director": created_by,
        "genre": genres,
        "primary_genre": primary_genre,
        "releaseDate": details.get("first_air_date") or "",
        "last_modified": str(int(time.time())),
        "rating": str(rating),
        "rating_5based": round(rating / 2, 1),
        "youtube_trailer": trailer,
        "episode_run_time": str(runtime),
    }

    _tmdb_series_cache[cache_key] = meta
    return meta


# ---------------------------------------------------------------------------
# EPG helpers
# ---------------------------------------------------------------------------

def _parse_xmltv_text(content: str) -> Tuple[Dict[str, dict], List[dict]]:
    channels: Dict[str, dict] = {}
    programmes: List[dict] = []

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return channels, programmes

    for ch in root.findall("channel"):
        cid = ch.get("id") or ""
        if not cid:
            continue
        display = ""
        for dn in ch.findall("display-name"):
            if dn.text:
                display = dn.text.strip()
                break
        icon = ""
        icon_el = ch.find("icon")
        if icon_el is not None and icon_el.get("src"):
            icon = icon_el.get("src")
        channels[cid] = {
            "id": cid,
            "display_name": display,
            "icon": icon,
            "normalized": _normalize_name(display),
        }

    for prog in root.findall("programme"):
        channel = prog.get("channel") or ""
        start = prog.get("start") or ""
        stop = prog.get("stop") or ""
        title = ""
        title_el = prog.find("title")
        if title_el is not None and title_el.text:
            title = title_el.text.strip()
        desc = ""
        desc_el = prog.find("desc")
        if desc_el is not None and desc_el.text:
            desc = desc_el.text.strip()
        if not channel or not start:
            continue
        programmes.append({
            "channel": channel,
            "start": start,
            "stop": stop,
            "title": title,
            "desc": desc,
        })

    return channels, programmes


def _download_epg(url: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            r = client.get(url, headers=UPSTREAM_HEADERS)
            r.raise_for_status()
            data = r.content
            # Detect gzip
            if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"EPG fetch failed {url}: {e}")
        return None


def fetch_and_parse_epg(force: bool = False):
    if not EPG_URLS:
        return
    now = time.time()
    if not force and _epg_cache["channels"] and (now - _epg_cache["fetched_at"]) < EPG_CACHE_SECONDS:
        return

    all_channels: Dict[str, dict] = {}
    all_programmes: List[dict] = []

    for url in EPG_URLS:
        text = _download_epg(url)
        if not text:
            continue
        chs, progs = _parse_xmltv_text(text)
        all_channels.update(chs)
        all_programmes.extend(progs)

    # Index programmes by channel, sorted by start
    by_channel: Dict[str, list] = {}
    for p in all_programmes:
        by_channel.setdefault(p["channel"], []).append(p)
    for cid in by_channel:
        by_channel[cid].sort(key=lambda x: x["start"])

    _epg_cache["channels"] = all_channels
    _epg_cache["programmes"] = all_programmes
    _epg_cache["by_channel"] = by_channel
    _epg_cache["fetched_at"] = now


def _match_epg_id(channel_name: str, existing_tvg_id: str = "") -> str:
    """Return best EPG channel id for this live channel."""
    if not _epg_cache["channels"]:
        return existing_tvg_id or ""

    # 1. Exact tvg-id match
    if existing_tvg_id and existing_tvg_id in _epg_cache["channels"]:
        return existing_tvg_id

    # 2. Normalized name match
    target = _normalize_name(channel_name)
    if not target:
        return existing_tvg_id or ""

    for cid, info in _epg_cache["channels"].items():
        if info["normalized"] == target:
            return cid

    # 3. Partial / contains match (weaker)
    for cid, info in _epg_cache["channels"].items():
        n = info["normalized"]
        if n and (target in n or n in target) and abs(len(n) - len(target)) < 8:
            return cid

    return existing_tvg_id or ""


def _xmltv_time_to_unix(t: str) -> int:
    """Convert XMLTV time (YYYYMMDDHHMMSS +ZZZZ) to unix timestamp."""
    try:
        # Take first 14 digits
        core = re.sub(r"[^\d]", "", t)[:14]
        if len(core) < 14:
            return 0
        struct = time.strptime(core, "%Y%m%d%H%M%S")
        return int(time.mktime(struct))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# M3U parsing
# ---------------------------------------------------------------------------

def _parse_m3u_text(
    content: str,
    live_channels: list,
    live_cat_ids: dict,
    live_categories: list,
    vod_streams: list,
    vod_cat_ids: dict,
    vod_categories: list,
    series_map: dict,
    series_cat_ids: dict,
    series_categories: list,
    next_live_id: int,
    next_vod_id: int,
    next_series_id: int,
) -> Tuple[int, int, int]:
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            name_match = re.search(r",(.+)$", line)
            name = name_match.group(1).strip() if name_match else "Unknown"

            group = "Uncategorized"
            logo = ""
            item_type = "live"
            tvg_id = ""

            group_match = re.search(r'group-title="([^"]*)"', line)
            if group_match and group_match.group(1).strip():
                group = group_match.group(1).strip()

            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            if logo_match:
                logo = logo_match.group(1)

            type_match = re.search(r'type="([^"]*)"', line)
            if type_match:
                item_type = type_match.group(1).lower()

            tvg_match = re.search(r'tvg-id="([^"]*)"', line)
            if tvg_match:
                tvg_id = tvg_match.group(1).strip()

            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i >= len(lines):
                break
            url = lines[i].strip()
            if not url or url.startswith("#"):
                i += 1
                continue

            if item_type == "movie":
                cat_id = _get_category_id(group, vod_cat_ids, vod_categories)
                imdb_id, tmdb_id = _extract_movie_ids(url, name)

                meta = None
                if TMDB_API_KEY and (imdb_id or tmdb_id):
                    cache_key = imdb_id or f"tmdb:{tmdb_id}"
                    meta = _tmdb_movie_cache.get(cache_key)

                display_name = meta["name"] if meta else name
                stream_icon = (meta["movie_image"] if meta else logo) or logo
                rating = meta["rating"] if meta else "0"
                rating_5 = meta["rating_5based"] if meta else 0

                final_group = group
                if meta and meta.get("primary_genre"):
                    final_group = meta["primary_genre"]
                    cat_id = _get_category_id(final_group, vod_cat_ids, vod_categories)

                vod_streams.append({
                    "num": next_vod_id,
                    "name": display_name,
                    "stream_type": "movie",
                    "stream_id": next_vod_id,
                    "stream_icon": stream_icon,
                    "rating": rating,
                    "rating_5based": rating_5,
                    "added": str(int(time.time())),
                    "category_id": cat_id,
                    "category_name": final_group,
                    "container_extension": "mp4",
                    "custom_sid": "",
                    "direct_source": url,
                    "url": url,
                    "_imdb_id": imdb_id,
                    "_tmdb_id": tmdb_id,
                    "_original_group": group,
                })
                next_vod_id += 1

            elif item_type == "series":
                series_name = name
                season_num = 1
                episode_num = 1
                episode_title = name

                se_match = re.search(r"(.+?)\s+S(\d+)E(\d+)\s*(.*)$", name, re.IGNORECASE)
                if se_match:
                    series_name = se_match.group(1).strip()
                    season_num = int(se_match.group(2))
                    episode_num = int(se_match.group(3))
                    episode_title = se_match.group(4).strip() or f"Episode {episode_num}"

                cat_id = _get_category_id(group, series_cat_ids, series_categories)
                tmdb_series_id = _extract_series_id_from_url(url)

                if series_name not in series_map:
                    series_map[series_name] = {
                        "num": next_series_id,
                        "name": series_name,
                        "series_id": next_series_id,
                        "cover": logo,
                        "plot": "",
                        "cast": "",
                        "director": "",
                        "genre": group,
                        "releaseDate": "",
                        "last_modified": str(int(time.time())),
                        "rating": "0",
                        "rating_5based": 0,
                        "backdrop_path": [],
                        "youtube_trailer": "",
                        "episode_run_time": "0",
                        "category_id": cat_id,
                        "category_name": group,
                        "episodes": {},
                        "_tmdb_id": tmdb_series_id,
                        "_original_group": group,
                    }
                    next_series_id += 1

                series = series_map[series_name]
                if tmdb_series_id and not series.get("_tmdb_id"):
                    series["_tmdb_id"] = tmdb_series_id

                season_key = str(season_num)
                if season_key not in series["episodes"]:
                    series["episodes"][season_key] = []

                ep_id = next_vod_id
                next_vod_id += 1

                series["episodes"][season_key].append({
                    "id": str(ep_id),
                    "episode_num": episode_num,
                    "title": episode_title,
                    "container_extension": "mp4",
                    "info": {
                        "movie_image": logo,
                        "plot": "",
                        "releasedate": "",
                        "rating": 0,
                    },
                    "custom_sid": "",
                    "added": str(int(time.time())),
                    "season": season_num,
                    "direct_source": url,
                    "url": url,
                })

            else:
                # LIVE
                cat_id = _get_category_id(group, live_cat_ids, live_categories)
                # Match against loaded EPG if available
                epg_id = _match_epg_id(name, tvg_id)
                live_channels.append({
                    "num": next_live_id,
                    "name": name,
                    "stream_type": "live",
                    "stream_id": next_live_id,
                    "stream_icon": logo,
                    "epg_channel_id": epg_id,
                    "category_id": cat_id,
                    "category_name": group,
                    "url": url,
                    "_tvg_id_raw": tvg_id,
                })
                next_live_id += 1

        i += 1

    return next_live_id, next_vod_id, next_series_id


def fetch_and_parse_all():
    now = time.time()
    if _cache["live_channels"] and (now - _cache["fetched_at"]) < CACHE_SECONDS:
        return

    # Load EPG first so matching works during M3U parse
    fetch_and_parse_epg()

    live_channels = []
    live_cat_ids = {}
    live_categories = []
    vod_streams = []
    vod_cat_ids = {}
    vod_categories = []
    series_map = {}
    series_cat_ids = {}
    series_categories = []

    next_live_id = 1
    next_vod_id = 1
    next_series_id = 1
    errors = []

    all_urls = LIVE_M3U_URLS + [MOVIES_M3U_URL, SERIES_M3U_URL]

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in all_urls:
            if not url:
                continue
            try:
                r = client.get(url, headers=UPSTREAM_HEADERS)
                r.raise_for_status()
                next_live_id, next_vod_id, next_series_id = _parse_m3u_text(
                    r.text,
                    live_channels, live_cat_ids, live_categories,
                    vod_streams, vod_cat_ids, vod_categories,
                    series_map, series_cat_ids, series_categories,
                    next_live_id, next_vod_id, next_series_id,
                )
            except Exception as e:
                errors.append(f"{url}: {e}")

    if not live_channels and not vod_streams and not series_map and errors:
        raise HTTPException(status_code=502, detail=f"Failed to fetch any M3U source: {'; '.join(errors)}")

    series_list = list(series_map.values())
    series_episodes = {s["series_id"]: s["episodes"] for s in series_list}

    _cache["live_channels"] = live_channels
    _cache["live_categories"] = live_categories
    _cache["vod_streams"] = vod_streams
    _cache["vod_categories"] = vod_categories
    _cache["series_list"] = series_list
    _cache["series_categories"] = series_categories
    _cache["series_episodes"] = series_episodes
    _cache["fetched_at"] = now


def check_auth(username: Optional[str], password: Optional[str]):
    if username != USERNAME or password != PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "M3U Xtream proxy (Live + Movies + Series + TMDB + Multi-EPG)",
        "live_sources": len(LIVE_M3U_URLS),
        "epg_sources": len(EPG_URLS),
        "epg_channels_loaded": len(_epg_cache["channels"]),
        "epg_programmes_loaded": len(_epg_cache["programmes"]),
        "movies_url": MOVIES_M3U_URL,
        "series_url": SERIES_M3U_URL,
        "tmdb_enabled": bool(TMDB_API_KEY),
        "tmdb_cached_movies": len(_tmdb_movie_cache),
        "tmdb_cached_series": len(_tmdb_series_cache),
        "xmltv_endpoint": "/xmltv.php",
    }


@app.get("/player_api.php")
async def player_api(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    series_id: Optional[str] = Query(None),
    vod_id: Optional[str] = Query(None),
    stream_id: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    check_auth(username, password)
    fetch_and_parse_all()

    if action is None:
        return JSONResponse({
            "user_info": {
                "username": USERNAME,
                "password": PASSWORD,
                "message": "Active",
                "auth": 1,
                "status": "Active",
                "exp_date": "4102444800",
                "is_trial": "0",
                "active_cons": "0",
                "created_at": "1609459200",
                "max_connections": "1",
                "allowed_output_formats": ["m3u8", "ts", "mp4"],
            },
            "server_info": {
                "url": SERVER_URL,
                "port": "443",
                "https_port": "443",
                "server_protocol": "https",
                "rtmp_port": "0",
                "timezone": "UTC",
                "timestamp_now": int(time.time()),
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        })

    # LIVE
    if action == "get_live_categories":
        return JSONResponse(_cache["live_categories"])

    if action == "get_live_streams":
        streams = []
        for ch in _cache["live_channels"]:
            streams.append({
                "num": ch["num"],
                "name": ch["name"],
                "stream_type": "live",
                "stream_id": ch["stream_id"],
                "stream_icon": ch["stream_icon"],
                "epg_channel_id": ch.get("epg_channel_id") or "",
                "added": str(int(time.time())),
                "category_id": ch["category_id"],
                "custom_sid": "",
                "tv_archive": 0,
                "direct_source": ch["url"],
                "tv_archive_duration": 0,
            })
        return JSONResponse(streams)

    # Short EPG / simple data table (Xtream style)
    if action in ("get_short_epg", "get_simple_data_table"):
        if not stream_id:
            return JSONResponse([])
        try:
            sid = int(stream_id)
        except ValueError:
            return JSONResponse([])

        channel = None
        for ch in _cache["live_channels"]:
            if ch["stream_id"] == sid:
                channel = ch
                break
        if not channel:
            return JSONResponse([])

        epg_id = channel.get("epg_channel_id") or ""
        progs = _epg_cache["by_channel"].get(epg_id, []) if epg_id else []

        now = int(time.time())
        limit_n = limit or 4
        result = []
        for p in progs:
            start_ts = _xmltv_time_to_unix(p["start"])
            stop_ts = _xmltv_time_to_unix(p["stop"]) if p.get("stop") else start_ts + 3600
            # Keep programmes that end in the future or recently started
            if stop_ts < now - 3600:
                continue
            result.append({
                "id": f"{epg_id}_{p['start']}",
                "epg_id": epg_id,
                "title": p.get("title") or "Unknown",
                "lang": "en",
                "start": str(start_ts),
                "end": str(stop_ts),
                "description": p.get("desc") or "",
                "channel_id": str(sid),
                "start_timestamp": start_ts,
                "stop_timestamp": stop_ts,
            })
            if len(result) >= limit_n:
                break
        return JSONResponse({"epg_listings": result} if action == "get_short_epg" else result)

    # VOD / MOVIES
    if action == "get_vod_categories":
        return JSONResponse(_cache["vod_categories"])

    if action == "get_vod_streams":
        clean = []
        for v in _cache["vod_streams"]:
            clean.append({
                "num": v["num"],
                "name": v["name"],
                "stream_type": "movie",
                "stream_id": v["stream_id"],
                "stream_icon": v["stream_icon"],
                "rating": v["rating"],
                "rating_5based": v["rating_5based"],
                "added": v["added"],
                "category_id": v["category_id"],
                "container_extension": "mp4",
                "custom_sid": "",
                "direct_source": v["url"],
            })
        return JSONResponse(clean)

    if action == "get_vod_info":
        if not vod_id:
            return JSONResponse({})

        target = None
        for v in _cache["vod_streams"]:
            if str(v["stream_id"]) == str(vod_id):
                target = v
                break
        if not target:
            return JSONResponse({})

        meta = None
        if TMDB_API_KEY:
            meta = _fetch_movie_metadata(
                imdb_id=target.get("_imdb_id"),
                tmdb_id=target.get("_tmdb_id"),
            )

        if meta:
            target["name"] = meta["name"]
            target["stream_icon"] = meta["movie_image"] or target["stream_icon"]
            target["rating"] = meta["rating"]
            target["rating_5based"] = meta["rating_5based"]

            new_group = meta.get("primary_genre") or target.get("_original_group") or "Uncategorized"
            cat_ids = {c["category_name"]: c["category_id"] for c in _cache["vod_categories"]}
            if new_group not in cat_ids:
                new_id = str(len(_cache["vod_categories"]) + 1)
                _cache["vod_categories"].append({
                    "category_id": new_id,
                    "category_name": new_group,
                    "parent_id": 0,
                })
                cat_ids[new_group] = new_id
            target["category_id"] = cat_ids[new_group]
            target["category_name"] = new_group

            return JSONResponse({
                "info": meta,
                "movie_data": {
                    "stream_id": target["stream_id"],
                    "name": meta["name"],
                    "added": target["added"],
                    "category_id": target["category_id"],
                    "container_extension": "mp4",
                    "custom_sid": "",
                    "direct_source": target["url"],
                }
            })
        else:
            return JSONResponse({
                "info": {
                    "name": target["name"],
                    "movie_image": target["stream_icon"],
                    "plot": "",
                    "cast": "",
                    "director": "",
                    "genre": "",
                    "releaseDate": "",
                    "rating": target.get("rating", "0"),
                    "duration_secs": 0,
                    "duration": "00:00:00",
                },
                "movie_data": {
                    "stream_id": target["stream_id"],
                    "name": target["name"],
                    "added": target["added"],
                    "category_id": target["category_id"],
                    "container_extension": "mp4",
                    "custom_sid": "",
                    "direct_source": target["url"],
                }
            })

    # SERIES
    if action == "get_series_categories":
        return JSONResponse(_cache["series_categories"])

    if action == "get_series":
        result = []
        for s in _cache["series_list"]:
            result.append({
                "num": s["num"],
                "name": s["name"],
                "series_id": s["series_id"],
                "cover": s["cover"],
                "plot": s["plot"],
                "cast": s["cast"],
                "director": s["director"],
                "genre": s["genre"],
                "releaseDate": s["releaseDate"],
                "last_modified": s["last_modified"],
                "rating": s["rating"],
                "rating_5based": s["rating_5based"],
                "backdrop_path": s["backdrop_path"],
                "youtube_trailer": s["youtube_trailer"],
                "episode_run_time": s["episode_run_time"],
                "category_id": s["category_id"],
            })
        return JSONResponse(result)

    if action == "get_series_info":
        if not series_id:
            return JSONResponse({})
        sid = int(series_id)

        target = None
        for s in _cache["series_list"]:
            if s["series_id"] == sid:
                target = s
                break
        if not target:
            return JSONResponse({})

        meta = None
        if TMDB_API_KEY and target.get("_tmdb_id"):
            meta = _fetch_series_metadata(target["_tmdb_id"])

        if meta:
            target["name"] = meta["name"]
            target["cover"] = meta["cover"] or target["cover"]
            target["plot"] = meta["plot"]
            target["cast"] = meta["cast"]
            target["director"] = meta["director"]
            target["genre"] = meta["genre"] or target["genre"]
            target["releaseDate"] = meta["releaseDate"]
            target["rating"] = meta["rating"]
            target["rating_5based"] = meta["rating_5based"]
            target["backdrop_path"] = meta["backdrop_path"]
            target["youtube_trailer"] = meta["youtube_trailer"]
            target["episode_run_time"] = meta["episode_run_time"]

            new_group = meta.get("primary_genre") or target.get("_original_group") or "Uncategorized"
            cat_ids = {c["category_name"]: c["category_id"] for c in _cache["series_categories"]}
            if new_group not in cat_ids:
                new_id = str(len(_cache["series_categories"]) + 1)
                _cache["series_categories"].append({
                    "category_id": new_id,
                    "category_name": new_group,
                    "parent_id": 0,
                })
                cat_ids[new_group] = new_id
            target["category_id"] = cat_ids[new_group]
            target["category_name"] = new_group

        return JSONResponse({
            "seasons": [
                {"season_number": int(k), "name": f"Season {k}", "cover": target["cover"]}
                for k in sorted(target["episodes"].keys(), key=int)
            ],
            "info": {
                "name": target["name"],
                "cover": target["cover"],
                "plot": target["plot"],
                "cast": target["cast"],
                "director": target["director"],
                "genre": target["genre"],
                "releaseDate": target["releaseDate"],
                "last_modified": target["last_modified"],
                "rating": target["rating"],
                "rating_5based": target["rating_5based"],
                "backdrop_path": target["backdrop_path"],
                "youtube_trailer": target["youtube_trailer"],
                "episode_run_time": target["episode_run_time"],
                "category_id": target["category_id"],
            },
            "episodes": target["episodes"],
        })

    return JSONResponse([])


@app.get("/xmltv.php")
@app.get("/epg.xml")
async def xmltv_endpoint(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
):
    """Merged XMLTV from all EPG_URLS, filtered to channels present in the live list."""
    check_auth(username, password)
    fetch_and_parse_all()
    fetch_and_parse_epg()

    # Only include EPG channels that match at least one live stream
    used_ids = set()
    for ch in _cache["live_channels"]:
        eid = ch.get("epg_channel_id") or ""
        if eid:
            used_ids.add(eid)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="xstream-multi-epg">',
    ]

    for cid in sorted(used_ids):
        info = _epg_cache["channels"].get(cid)
        if not info:
            continue
        lines.append(f'  <channel id="{_xml_escape(cid)}">')
        lines.append(f'    <display-name>{_xml_escape(info["display_name"])}</display-name>')
        if info.get("icon"):
            lines.append(f'    <icon src="{_xml_escape(info["icon"])}" />')
        lines.append("  </channel>")

    for p in _epg_cache["programmes"]:
        if p["channel"] not in used_ids:
            continue
        start = p.get("start") or ""
        stop = p.get("stop") or ""
        stop_attr = f' stop="{_xml_escape(stop)}"' if stop else ""
        lines.append(
            f'  <programme start="{_xml_escape(start)}"{stop_attr} channel="{_xml_escape(p["channel"])}">'
        )
        lines.append(f'    <title>{_xml_escape(p.get("title") or "Unknown")}</title>')
        if p.get("desc"):
            lines.append(f'    <desc>{_xml_escape(p["desc"])}</desc>')
        lines.append("  </programme>")

    lines.append("</tv>")
    return PlainTextResponse("\n".join(lines), media_type="application/xml")


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@app.get("/get.php")
async def get_php(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    type: Optional[str] = Query("m3u_plus"),
    output: Optional[str] = Query("ts"),
):
    check_auth(username, password)
    fetch_and_parse_all()

    lines = ["#EXTM3U"]

    for ch in _cache["live_channels"]:
        logo = f' tvg-logo="{ch["stream_icon"]}"' if ch["stream_icon"] else ""
        group = f' group-title="{ch["category_name"]}"' if ch["category_name"] else ""
        tvg = f' tvg-id="{ch.get("epg_channel_id") or ""}"' if ch.get("epg_channel_id") else ""
        lines.append(f'#EXTINF:-1{tvg}{logo}{group},{ch["name"]}')
        lines.append(ch["url"])

    for v in _cache["vod_streams"]:
        logo = f' tvg-logo="{v["stream_icon"]}"' if v["stream_icon"] else ""
        group = f' group-title="{v.get("category_name", "Movies")}"'
        lines.append(f'#EXTINF:-1 type="movie"{logo}{group},{v["name"]}')
        lines.append(v["url"])

    for s in _cache["series_list"]:
        for season, eps in s["episodes"].items():
            for ep in eps:
                logo = f' tvg-logo="{s["cover"]}"' if s.get("cover") else ""
                group = f' group-title="{s.get("category_name", "Series")}"'
                name = f'{s["name"]} S{season}E{ep["episode_num"]} {ep.get("title", "")}'.strip()
                lines.append(f'#EXTINF:-1 type="series"{logo}{group},{name}')
                lines.append(ep["url"])

    return PlainTextResponse("\n".join(lines), media_type="audio/x-mpegurl")


async def _proxy_stream(target_url: str):
    if target_url.endswith(".m3u8") or "m3u8" in target_url.lower():
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
            r = await client.get(target_url)
            r.raise_for_status()
            rewritten_lines = []
            for line in r.text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    rewritten_lines.append(urljoin(target_url, stripped))
                else:
                    rewritten_lines.append(line)
            return PlainTextResponse(
                "\n".join(rewritten_lines),
                media_type="application/vnd.apple.mpegurl",
            )

    async def proxy_bytes():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
            async with client.stream("GET", target_url) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(proxy_bytes(), media_type="video/mp2t")


@app.get("/live/{user}/{passwd}/{stream_id_ext}")
async def live_stream(user: str, passwd: str, stream_id_ext: str):
    check_auth(user, passwd)
    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    stream_id = int(match.group(1))

    fetch_and_parse_all()
    for ch in _cache["live_channels"]:
        if ch["stream_id"] == stream_id:
            return await _proxy_stream(ch["url"])
    raise HTTPException(status_code=404, detail="Stream not found")


@app.get("/movie/{user}/{passwd}/{stream_id_ext}")
async def movie_stream(user: str, passwd: str, stream_id_ext: str):
    check_auth(user, passwd)
    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    stream_id = int(match.group(1))

    fetch_and_parse_all()
    for v in _cache["vod_streams"]:
        if v["stream_id"] == stream_id:
            return await _proxy_stream(v["url"])
    raise HTTPException(status_code=404, detail="Movie not found")


@app.get("/series/{user}/{passwd}/{stream_id_ext}")
async def series_stream(user: str, passwd: str, stream_id_ext: str):
    check_auth(user, passwd)
    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    episode_id = match.group(1)

    fetch_and_parse_all()
    for series_id, seasons in _cache["series_episodes"].items():
        for season, eps in seasons.items():
            for ep in eps:
                if str(ep["id"]) == episode_id:
                    return await _proxy_stream(ep["url"])
    raise HTTPException(status_code=404, detail="Episode not found")
