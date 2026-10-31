#!/usr/bin/env python3
"""rex-stremio: Stremio addons' catalogs as a rex source.

Catalogs come from the Stremio addons you add, streams from every addon that serves them, as in
Stremio. Each film or episode goes to rex with its IMDb id, so Plex names it; nothing is
downloaded: rex plays the stream an addon gives at the moment someone presses play.

One process does three things:

* a relay rex reads bytes from: `GET /<library>/<catalog>/<item>.<ext>` asks the stream addons,
  ranks what they give, and answers 302 to the first link that opens (asked again when the
  cached one is an hour old, as addons' links expire).
* a sync loop that registers with rex's feed API (`docs/feed-api.md` in every rex download),
  declares the libraries, and pushes every item of every chosen catalog, each probed once.
* a settings page at `/` for adding addons, choosing catalogs and ranking streams. It answers
  only your own network and writes `config.json`.

Standard library only; ffprobe is a separate executable.
"""
import argparse
import collections
import copy
import functools
import hashlib
import html
import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("rex-stremio")
MAX_FORM_BYTES = 1 << 20
MAX_FORM_FIELDS = 2000
MAX_JSON_BYTES = 16 << 20

DEFAULTS = {
    "rex_url": "http://127.0.0.1:32400",
    "source": "stremio",
    "listen": "127.0.0.1:9982",
    "relay_url": None,
    "mount": "/mnt/rex-stremio",
    "cache": "cache",
    "ffprobe": "ffprobe",
    "interval_minutes": 360,
    "max_resolution": None,
    "collections": False,
    "max_size_gb": None,
    "addons": [],
    "catalogs": [],
}

# Series' seasons and episodes come from Cinemeta, whose ids are IMDb's.
CINEMETA = "https://v3-cinemeta.strem.io"
# A catalog that lists TMDB ids (`tmdb:…`: Cartoon Network, Nickelodeon, Sitcom Catalogs...) names
# no IMDb id; when no added addon's meta does, the TMDB addon's does (2026-10-24).
TMDB_META = "https://tmdb.elfhosted.com"

# Where each item's probe is kept. "probes2" since an HLS master is relayed as its best variant
# alone: a probe of the whole master names other streams, so every item is probed once more.
PROBES = "probes2"

# ffprobe's container -> the extension rex takes the file by (an HLS playlist is `.m3u8`).
CONTAINERS = {"hls": "m3u8", "matroska,webm": "mkv", "mov,mp4,m4a,3gp,3g2,mj2": "mp4", "mpegts": "ts", "avi": "avi"}

RESOLUTIONS = [(r"2160p|\b4k\b|\buhd\b", 2160), (r"1440p", 1440), (r"1080p|\bfhd\b", 1080), (r"720p|\bhd\b", 720),
               (r"576p|480p|\bsd\b", 480)]

# A catalog's Stremio type -> its default library and the kind of rex library that holds it. A
# live channel (`tv`) is a film-like row: nothing identifies it, the addon's `display` names it.
LIBRARIES = {"movie": "Stremio Films", "series": "Stremio Shows", "tv": "Live TV"}
REX_KINDS = {"movie": "movies", "series": "shows", "tv": "movies"}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "catalog"


def get_json(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": "rex-stremio", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response_json(response)


def response_json(response):
    chunks, total = [], 0
    while True:
        chunk = response.read(min(1 << 16, MAX_JSON_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_JSON_BYTES:
            raise ValueError("JSON response is too large")
    body = b"".join(chunks)
    return json.loads(body or b"{}")


def write_json(path, value, indent=None):
    """Replace a JSON file atomically without exposing addon credentials."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    descriptor, temporary = tempfile.mkstemp(prefix=".%s-" % os.path.basename(path), dir=directory)
    try:
        if hasattr(os, "fchmod"):  # Windows has none, and its files have no mode bits to set
            os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w")
        # fdopen now owns the descriptor. Do not close its old integer again if a later
        # dump/replace fails: that number may already have been reused by another thread.
        descriptor = None
        with handle:
            json.dump(value, handle, indent=indent)
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass


def safe_url(url):
    """A URL's scheme and host, without credentials, path or query secrets."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        host = parts.hostname
        if not parts.scheme or not host:
            return "<url>"
        host = "[%s]" % host if ":" in host else host
        if parts.port:
            host += ":%d" % parts.port
        return "%s://%s" % (parts.scheme, host)
    except (TypeError, ValueError):
        return "<url>"


def safe_error(error):
    """Remove credential-bearing URLs before an error reaches logs or status."""
    text = str(error)
    return re.sub(r"https?://[^\s'\"<>]+", lambda match: safe_url(match.group()), text)[:300]


def safe_stream_key(value):
    return safe_url(value) if isinstance(value, str) and value.startswith(("http://", "https://")) else value


def rex_endpoint(base, path):
    """Append a feed route without discarding a configured rex path or query."""
    parts = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + path,
                                    parts.query, ""))


def addon_base(url):
    """A manifest's url as Stremio shows it (`stremio://`, `.../manifest.json`) -> the addon's base."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    return url.removesuffix("/manifest.json").rstrip("/")


def addon_manifest(addon):
    value = addon.get("manifest") if isinstance(addon, dict) else None
    return value if isinstance(value, dict) else {}


def serves(manifest, resource, kind, sid):
    """Whether an addon answers `resource` for `kind` and this id, by its manifest."""
    if not isinstance(manifest, dict) or not isinstance(sid, str):
        return False
    resources = manifest.get("resources")
    if not isinstance(resources, list):
        return False
    for entry in resources:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != resource:
            continue
        types = entry.get("types") or manifest.get("types") or []
        prefixes = entry.get("idPrefixes") or manifest.get("idPrefixes")
        if isinstance(types, str):
            types = [types]
        elif not isinstance(types, list):
            types = []
        types = [value for value in types if isinstance(value, str)]
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        elif not isinstance(prefixes, list):
            prefixes = []
        prefixes = [value for value in prefixes if isinstance(value, str)]
        if isinstance(kind, str) and kind in types and (not prefixes or any(sid.startswith(prefix) for prefix in prefixes)):
            return True
    return False


def catalogs_of(manifest):
    """The catalogs of films, series and live channels an addon lists without being asked something first (a
    search catalog needs a query)."""
    if not isinstance(manifest, dict):
        return []
    raw_catalogs = manifest.get("catalogs")
    if not isinstance(raw_catalogs, list):
        return []
    out = []
    for catalog in raw_catalogs:
        if not isinstance(catalog, dict):
            continue
        raw_extra = catalog.get("extra")
        if raw_extra is not None and not isinstance(raw_extra, list):
            continue
        extra = raw_extra if isinstance(raw_extra, list) else []
        required = [entry for entry in extra
                    if isinstance(entry, dict) and entry.get("isRequired")]
        extra_required = catalog.get("extraRequired")
        if extra_required is not None and not isinstance(extra_required, list):
            continue
        if isinstance(extra_required, list):
            required += [name for name in extra_required if isinstance(name, str)]
        if (catalog.get("type") in LIBRARIES and isinstance(catalog.get("id"), str)
                and catalog["id"] and not required):
            out.append({"type": catalog["type"], "id": catalog["id"], "name": catalog.get("name") or catalog["id"]})
    return out


def quality(stream):
    """(resolution, size in bytes) as the stream's labels give them; 0 or None where they do not."""
    if not isinstance(stream, dict):
        return 0, None
    hints = stream.get("behaviorHints") or {}
    if not isinstance(hints, dict):
        hints = {}
    text = " ".join(str(stream.get(key) or "") for key in ("name", "title", "description")) + " " + str(hints.get("filename") or "")
    resolution = next((value for pattern, value in RESOLUTIONS if re.search(pattern, text, re.I)), 0)
    size = hints.get("videoSize")
    try:
        size = float(size) if size else None
    except (TypeError, ValueError, OverflowError):
        size = None
    if size is not None and (not math.isfinite(size) or size <= 0):
        size = None
    if not size:
        found = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", text, re.I)
        if found:
            try:
                size = float(found[1]) * (1 << (30 if found[2].upper() == "GB" else 20))
            except (TypeError, ValueError, OverflowError):
                size = None
    if size is not None and (not math.isfinite(size) or size <= 0):
        size = None
    return resolution, int(size) if size else None


def stream_key(stream):
    """What names one stream across asks: its file, else its group, else its link."""
    if not isinstance(stream, dict):
        return None
    hints = stream.get("behaviorHints") or {}
    if not isinstance(hints, dict):
        hints = {}
    return hints.get("filename") or hints.get("bingeGroup") or stream.get("url")


def ranked(streams, config, pinned=None):
    """The streams rex can read, best first: an http(s) `url` (a torrent's `infoHash`, a YouTube
    `ytId` and an `externalUrl` are not files), within the configured limits, highest
    resolution first, the addons' own order breaking ties. The stream an item was probed from
    (`pinned`) goes first, so what plays is what rex was told about."""
    try:
        cap = int(config["max_resolution"]) if config.get("max_resolution") is not None else None
    except (TypeError, ValueError):
        cap = None
    try:
        biggest = config.get("max_size_gb") and float(config["max_size_gb"]) * (1 << 30)
    except (TypeError, ValueError, OverflowError):
        biggest = None
    if biggest is not None and not math.isfinite(biggest):
        biggest = None
    if not isinstance(streams, list):
        return []
    kept = []
    for at, stream in enumerate(streams):
        if not isinstance(stream, dict):
            continue
        if not isinstance(stream.get("url"), str) or not stream["url"].startswith(("http://", "https://")):
            continue
        resolution, size = quality(stream)
        if (cap and resolution > cap) or (biggest and size and size > biggest):
            continue
        kept.append((stream_key(stream) != pinned if pinned else False, -resolution, at, stream))
    return [stream for *_, stream in sorted(kept, key=lambda row: row[:3])]


def request_headers(stream):
    """The headers a stream's link must be asked with (`behaviorHints.proxyHeaders.request`, which
    Stremio's streaming server sends for it). Eclipsia, Hyper, Hydra and BanglaPlex links answer
    403 or 429 without their Referer and 206 with it (2026-10-24). Encoding is left to us: a
    playlist is read and rewritten here."""
    hints = stream.get("behaviorHints") if isinstance(stream, dict) else None
    proxy = hints.get("proxyHeaders") if isinstance(hints, dict) else None
    headers = proxy.get("request") if isinstance(proxy, dict) else None
    if not isinstance(headers, dict):
        return {}
    return {str(name): str(value) for name, value in headers.items()
            if isinstance(value, (str, int)) and str(name).lower() not in ("accept-encoding", "range", "host", "connection", "content-length")}


def opens(url, headers=None):
    """The link a stream ends at, when it opens; None when it does not (an expired or refused link,
    or a web page: addons list their configure page, a donation or status page as a "stream", and
    Comet's unconfigured answer was nothing else, 2026-10-24)."""
    request = urllib.request.Request(url, headers=dict({"User-Agent": "rex-stremio"}, **(headers or {}), Range="bytes=0-0"))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            kind = response.headers.get_content_type()
            if kind == "text/html":
                LOG.info("%s: a web page, not a video", safe_url(url))
                return None
            # A file that answers a Range with all of itself cannot be seeked, and rex reads every
            # file by Range: Google's video-downloads links send the whole 9.6 GB MKV from byte 0
            # (PlavioHD, Flix-Streams, 2026-10-24) and a player reading its end got its start.
            playlist = "mpegurl" in kind or urllib.parse.urlsplit(response.geturl()).path.endswith(".m3u8")
            if response.status == 200 and not playlist and int(response.headers.get("Content-Length") or 0) > 1:
                LOG.info("%s: answers no Range, so it cannot be seeked", safe_url(url))
                return None
            # A "file" of a few bytes is an error body: one of Hydra's links was 150 bytes at play.
            total = re.search(r"/(\d+)$", response.headers.get("Content-Range") or "")
            if total and int(total[1]) < (1 << 20) and not playlist:
                LOG.info("%s: %s bytes, not a video", safe_url(url), total[1])
                return None
            return response.geturl() if response.status in (200, 206) else None
    except Exception as error:
        LOG.info("%s: %s", safe_url(url), safe_error(error))
        return None


def load_config(path):
    with open(path) as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    config = dict(DEFAULTS, **raw)
    if not isinstance(config.get("catalogs"), list) or not isinstance(config.get("addons"), list):
        raise ValueError("addons and catalogs must be lists")
    if not isinstance(config.get("source"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", config["source"]):
        raise ValueError("source must be a simple name")
    for key in ("rex_url", "relay_url"):
        value = config.get(key)
        if value is None:
            if key == "rex_url":
                raise ValueError("rex_url must be an http(s) URL")
            continue
        if not isinstance(value, str):
            raise ValueError("%s must be an http(s) URL" % key)
        try:
            parts = urllib.parse.urlsplit(value)
            parts.port
            hostname = parts.hostname
        except (TypeError, ValueError):
            raise ValueError("%s must be an http(s) URL" % key)
        if parts.scheme not in ("http", "https") or not hostname:
            raise ValueError("%s must be an http(s) URL" % key)
    if not isinstance(config.get("cache"), str) or not config["cache"]:
        raise ValueError("cache must be a path")
    for key in ("listen", "mount", "ffprobe"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError("%s must be a string" % key)
    if not re.fullmatch(r"(?:\[[^\]]+\]|[^:]+):\d{1,5}", config["listen"]):
        raise ValueError("listen must be host:port")
    if int(config["listen"].rsplit(":", 1)[1]) > 65535:
        raise ValueError("listen port is out of range")
    try:
        interval = int(config.get("interval_minutes"))
    except (TypeError, ValueError):
        raise ValueError("interval_minutes must be positive")
    if interval <= 0:
        raise ValueError("interval_minutes must be positive")
    config["interval_minutes"] = interval
    if config.get("max_resolution") is not None:
        try:
            config["max_resolution"] = int(config["max_resolution"])
        except (TypeError, ValueError):
            raise ValueError("max_resolution must be a number")
        if config["max_resolution"] <= 0:
            raise ValueError("max_resolution must be positive")
    if config.get("max_size_gb") is not None:
        try:
            config["max_size_gb"] = float(config["max_size_gb"])
        except (TypeError, ValueError):
            raise ValueError("max_size_gb must be a number")
        if not math.isfinite(config["max_size_gb"]) or config["max_size_gb"] <= 0:
            raise ValueError("max_size_gb must be positive")
    if not isinstance(config.get("collections"), bool):
        raise ValueError("collections must be true or false")
    if config["cache"] and not os.path.isabs(config["cache"]):
        config["cache"] = os.path.join(os.path.dirname(os.path.abspath(path)), config["cache"])
    if not config["relay_url"]:
        config["relay_url"] = "http://" + config["listen"]
    catalogs = []
    explicit_slugs = set()
    records = []
    for raw_catalog in config["catalogs"]:
        if not isinstance(raw_catalog, dict):
            raise ValueError("catalog must be an object")
        catalog = dict(raw_catalog)
        url = catalog.get("url", "")
        if not isinstance(url, str):
            raise ValueError("catalog URL must be a string")
        found = re.fullmatch(r"(https?://.+?)/catalog/(movie|series|tv)/([^/]+?)\.json", url)
        if not found:
            raise ValueError("not a Stremio catalog URL (<addon>/catalog/<movie|series|tv>/<id>.json)")
        catalog = dict({"library": LIBRARIES[found[2]], "limit": 50}, **catalog)
        if not isinstance(catalog.get("library"), str) or not catalog["library"].strip():
            raise ValueError("catalog library must be a name")
        try:
            catalog["limit"] = int(catalog["limit"])
        except (TypeError, ValueError):
            raise ValueError("catalog limit must be positive")
        if catalog["limit"] <= 0:
            raise ValueError("catalog limit must be positive")
        has_slug = "slug" in raw_catalog and raw_catalog.get("slug") not in (None, "")
        if has_slug:
            if not isinstance(catalog.get("slug"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", catalog["slug"]):
                raise ValueError("catalog slug must be a simple name")
            if catalog["slug"] in explicit_slugs:
                raise ValueError("catalog slugs must be unique")
            explicit_slugs.add(catalog["slug"])
        else:
            catalog.pop("slug", None)
        has_directory = "directory" in raw_catalog and raw_catalog.get("directory") not in (None, "")
        if has_directory and (not isinstance(catalog.get("directory"), str)
                              or not re.fullmatch(r"[A-Za-z0-9_-]+", catalog["directory"])):
            raise ValueError("catalog directory must be a simple name")
        if not has_directory:
            catalog.pop("directory", None)
        catalog["type"] = found[2]
        records.append((catalog, found[3], has_slug, has_directory))
    allocated_slugs = set(explicit_slugs)
    for catalog, catalog_id, has_slug, has_directory in records:
        if not has_slug:
            base = slug(str(catalog.get("title") or catalog_id))
            candidate = base
            while candidate in allocated_slugs:
                # Keep the historical sequence (base, base-2, base-2-2, ...). Existing
                # configs without persisted identity must not move their feed ids on upgrade.
                candidate += "-2"
            catalog["slug"] = candidate
        allocated_slugs.add(catalog["slug"])
        if not has_directory:
            catalog["directory"] = slug(catalog["library"])
        if not isinstance(catalog["directory"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", catalog["directory"]):
            raise ValueError("catalog directory must be a simple name")
        catalogs.append(catalog)
    libraries = {}
    for catalog in catalogs:
        key = (catalog["directory"], REX_KINDS[catalog["type"]])
        previous = libraries.setdefault(key, catalog["library"])
        if previous != catalog["library"]:
            raise ValueError("catalog directory has conflicting libraries")
    config["catalogs"] = catalogs
    return config


def save_config(path, values):
    """The settings as written, mode 600: addon URLs can hold debrid keys.
    Catalog identity is retained so item ids do not move when rows are reordered."""
    raw = dict(values, catalogs=[{key: value for key, value in catalog.items() if key != "type"}
                                 for catalog in values.get("catalogs") or []])
    if raw.get("relay_url") == "http://%s" % raw.get("listen"):
        del raw["relay_url"]
    write_json(path, raw, indent=2)


def validate_config(path, values):
    """Parse a candidate beside the real file before replacing the saved config."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    descriptor, candidate = tempfile.mkstemp(prefix=".rex-stremio-config-", dir=directory)
    os.close(descriptor)
    os.chmod(candidate, 0o600)
    try:
        save_config(candidate, values)
        return load_config(candidate)
    finally:
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass


@functools.lru_cache(maxsize=None)
def lenient_hls(ffprobe):
    """`-extension_picky 0` where the ffprobe has it. FFmpeg 7.1+ refuses a whole HLS one of
    whose segments' format does not match its URL's extension, as PenguPlay's channels' WebVTT
    rendition does (ffprobe 8.1, 2026-10-24); an older one refuses the unknown option."""
    try:
        out = subprocess.run([ffprobe, "-hide_banner", "-h", "demuxer=hls"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    return ("-extension_picky", "0") if b"-extension_picky" in out else ()


def placeholder(probe):
    """A film or an episode a minute long is an addon's message, not the title: Freedom Media's
    links all played one 10 s clip (2026-10-24), PenguPlay's sign-in prompt is another."""
    try:
        return 0 < float((probe.get("format") or {}).get("duration")) < 60
    except (TypeError, ValueError):
        return False


def expiry(url):
    """A googlevideo manifest names its own expiry (`/expire/<unix>/`); two hours if it does not."""
    match = re.search(r"/expire/(\d+)/", url)
    return int(match[1]) if match else time.time() + 7200


def stremio_item(config, catalog, vid, imdb, meta, video, probe, extension):
    """One feed item (`docs/feed-api.md`): placed by its IMDb id, dressed by the catalog. A live
    channel has no IMDb id (`imdb` is the addon's own id): it goes unplaced, named by `display`."""
    path = "%s/%s/%s.%s" % (catalog["directory"], catalog["slug"], vid, extension)
    held, file = "st-" + vid, path
    if config.get("collections") and catalog["type"] != "tv" and imdb:
        # One id per library, whichever catalog holds the title: when the one holding it stops
        # listing it and the next takes over, rex keeps its row (and watch state) on that id. Its
        # file is its own too: rex refuses a new id for a file another id has.
        rest = vid[len(catalog["slug"]) + 1:]
        held, file = "stc-%s-%s" % (catalog["directory"], rest), "%s/%s.%s" % (catalog["directory"], rest, extension)
    pushed = {
        "id": held,
        "path": path,
        "file": "%s/%s" % (config["mount"].rstrip("/"), file),
        "name": meta.get("name") or imdb or str(meta.get("id")),
        "release": "stremio:%s" % (imdb or meta.get("id")),
        "media_info": probe,
    }
    if catalog["type"] != "tv" and imdb:
        pushed["ids"] = {"imdb": imdb}
    size = (probe.get("format") or {}).get("size")
    try:
        size = int(size) if size else None
    except (TypeError, ValueError):
        size = None
    if size and extension != "m3u8":
        pushed["size"] = size
    released = lambda value: str(value or "")[:10] if re.match(r"\d{4}-\d{2}-\d{2}", str(value or "")) else None
    year = re.match(r"\d{4}", str(meta.get("year") or meta.get("releaseInfo") or ""))
    if catalog["type"] == "tv":
        display = {"title": meta.get("name"), "summary": meta.get("description"),
                   "thumb": meta.get("poster") or meta.get("logo"), "art": meta.get("background")}
    elif catalog["type"] == "movie":
        pushed["kind"] = "movie"
        display = {"title": meta.get("name"), "summary": meta.get("description"), "thumb": meta.get("poster"),
                   "art": meta.get("background"), "date": released(meta.get("released")) or (year and year[0])}
    else:
        pushed.update(kind="episode", season=video["season"], episode=video["episode"])
        display = {"title": video.get("name") or video.get("title"), "summary": video.get("overview") or video.get("description"),
                   "thumb": video.get("thumbnail"), "date": released(video.get("released") or video.get("firstAired")),
                   "show": {"title": meta.get("name"), "summary": meta.get("description"), "poster": meta.get("poster"),
                            "art": meta.get("background")}}
    pushed["display"] = prune(display)
    return pushed


def prune(value):
    if isinstance(value, dict):
        kept = {key: prune(inner) for key, inner in value.items() if inner not in (None, "", {})}
        return {key: inner for key, inner in kept.items() if inner not in (None, "", {})}
    return value


class Sync:
    def __init__(self, config):
        self.config = config
        self.wake = threading.Event()
        self.status = "not synced yet"
        self.cache, self.locks, self.guard = {}, {}, threading.Lock()
        self.cache_locks = {}
        # The headers each item's link is asked with, and the links its playlists name, which the
        # relay fetches with them (`relayed`): no other URL is fetched for anyone.
        self.via, self.relayed = {}, {}
        self.config_guard = threading.RLock()
        # Feed publication and settings commits share this lock. Readers never take it, so a
        # stalled rex request cannot stop the relay or settings page from serving.
        self.publication_lock = threading.RLock()
        self.config_version = 0
        # Set by a save on the settings page: the next complete snapshot may remove more than half
        # of what rex holds (catalogs unticked, catalogs turned into collections), which rex
        # refuses (409) unless forced. A start never forces, so rex's guard still holds then.
        self.force_next = False
        self.catalog_incomplete = False
        # A sync's titles held by a catalog, `(directory, type, imdb)`, and the IMDb ids the last
        # catalog listed: with `collections` on, one title per library (`catalog_items`).
        self.taken, self.catalog_listed = set(), set()

    def path(self, kind, vid, config=None, version=None):
        config = self.config if config is None else config
        if version is None:
            os.makedirs(os.path.join(config["cache"], kind), exist_ok=True)
        else:
            with self.config_guard:
                current = self.config_version == version
            if current:
                os.makedirs(os.path.join(config["cache"], kind), exist_ok=True)
        return os.path.join(config["cache"], kind, vid + ".json")

    def cached(self, kind, vid, make, config=None, version=None):
        path = self.path(kind, vid, config, version)
        lock = self.record_lock(path)
        with lock:
            try:
                with open(path) as handle:
                    return json.load(handle)
            except FileNotFoundError:
                pass
        value = make()
        with lock:
            if not os.path.exists(path):
                if version is None:
                    write_json(path, value)
                else:
                    with self.config_guard:
                        if self.config_version == version:
                            write_json(path, value)
                return value
            with open(path) as handle:
                return json.load(handle)

    def record_lock(self, path):
        with self.guard:
            return self.cache_locks.setdefault(path, threading.RLock())

    def cache_write(self, path, value, version=None):
        """Write one cache record under its own lock; never hold it during a probe or relay ask."""
        lock = self.record_lock(path)
        with lock:
            if version is not None:
                with self.config_guard:
                    if self.config_version != version:
                        return False
                    write_json(path, value)
            else:
                write_json(path, value)
        return True

    def cache_update(self, path, fields, version=None):
        """Merge a small info-record update while holding its file lock."""
        lock = self.record_lock(path)
        with lock:
            if version is not None:
                with self.config_guard:
                    if self.config_version != version:
                        return False
                    return self._cache_update_locked(path, fields)
            return self._cache_update_locked(path, fields)

    @staticmethod
    def _cache_update_locked(path, fields):
        try:
            with open(path) as handle:
                current = json.load(handle)
        except (FileNotFoundError, ValueError, TypeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(fields)
        write_json(path, current)
        return True

    def cache_remove(self, path, version=None):
        lock = self.record_lock(path)
        with lock:
            if version is not None:
                with self.config_guard:
                    if self.config_version != version:
                        return False
                    return self._cache_remove_locked(path)
            return self._cache_remove_locked(path)

    @staticmethod
    def _cache_remove_locked(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return True

    def store_cache(self, version, vid, value, headers=None):
        with self.config_guard:
            if self.config_version != version:
                return
            with self.guard:
                self.cache[vid] = value
                self.via[vid] = headers or {}

    def imdb_of(self, kind, sid, config, version):
        """The IMDb id of a catalog item known by another id, as a meta addon names it: an added
        addon that serves meta for it, else the TMDB addon for a `tmdb:` id. Kept once found."""
        def ask():
            bases = [addon_base(addon["url"]) for addon in config["addons"]
                     if isinstance(addon, dict) and isinstance(addon.get("url"), str) and serves(addon_manifest(addon), "meta", kind, sid)]
            if sid.startswith("tmdb:"):
                bases.append(TMDB_META)
            for base in bases:
                try:
                    found = get_json("%s/meta/%s/%s.json" % (base, kind, urllib.parse.quote(sid)), 30).get("meta") or {}
                except Exception as error:
                    LOG.info("%s: %s: %s", sid, safe_url(base), safe_error(error))
                    continue
                imdb = str(found.get("imdb_id") or "") if isinstance(found, dict) else ""
                if re.fullmatch(r"tt\d+", imdb):
                    return {"imdb": imdb}
            raise LookupError("no meta addon names an IMDb id")
        try:
            return self.cached("ids", re.sub(r"[^A-Za-z0-9_-]", "-", "%s-%s" % (kind, sid)), ask, config, version)["imdb"]
        except Exception:
            return ""

    def own_meta(self, kind, sid, config):
        """A series only its addon knows, with its episodes (`videos`), from an addon that serves
        meta for its id."""
        for addon in config["addons"]:
            if not (isinstance(addon, dict) and isinstance(addon.get("url"), str) and serves(addon_manifest(addon), "meta", kind, sid)):
                continue
            try:
                found = get_json("%s/meta/%s/%s.json" % (addon_base(addon["url"]), kind, urllib.parse.quote(sid)), 60).get("meta")
            except Exception as error:
                LOG.warning("%s: %s", sid, safe_error(error))
                continue
            if isinstance(found, dict) and isinstance(found.get("videos"), list) and found["videos"]:
                return found
        return None

    def refuse(self, vid, config, version):
        """Set aside for a day the stream an item is pinned to, and what was learned from it.
        False when nothing is pinned (no stream opened at all)."""
        path = self.path("info", vid, config, version)
        try:
            with open(path) as handle:
                info = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(info, dict) or not info.get("stream"):
            return False
        now = time.time()
        refused = [entry for entry in info.get("refused") or [] if isinstance(entry, dict) and now - entry.get("at", 0) < 86400]
        refused = (refused + [{"key": info["stream"], "at": now}])[-20:]
        LOG.warning("%s: %s set aside, trying the next stream", vid, safe_stream_key(info["stream"]))
        self.cache_update(path, {"stream": None, "refused": refused}, version)
        self.cache_remove(self.path(PROBES, vid, config, version), version)
        with self.guard:
            self.cache.pop(vid, None)
        return True

    def remember(self, vid, url):
        """A link an item's playlist names, as the relay path it is fetched through."""
        token = hashlib.sha256(url.encode()).hexdigest()[:20]
        extension = re.search(r"\.([A-Za-z0-9]{1,5})$", urllib.parse.urlsplit(url).path)
        with self.guard:
            links = self.relayed.setdefault(vid, collections.OrderedDict())
            links[token] = url
            links.move_to_end(token)
            while len(links) > 4096:  # a live playlist names new segments for ever
                links.popitem(last=False)
        return "/_relay/%s/%s.%s" % (vid, token, extension[1] if extension else "bin")

    def relayed_link(self, vid, token):
        with self.guard:
            return (self.relayed.get(vid) or {}).get(token), self.via.get(vid) or {}

    def stale(self, version):
        with self.config_guard:
            return version is not None and self.config_version != version

    def publish(self, version, method, path, body, config):
        """Make one feed request only while its generation is still current.

        The publication lock serializes this request with a settings commit, but the catalog
        scan and probes happen before this method is entered. No config lock is held over I/O.
        """
        with self.publication_lock:
            with self.config_guard:
                if self.config_version != version:
                    return None
            return self.rex(method, path, body, config)

    def rex(self, method, path, body=None, config=None):
        config = self.config if config is None else config
        request = urllib.request.Request(rex_endpoint(config["rex_url"], path), method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=600) as response:
            answer = response_json(response)
        if not isinstance(answer, dict):
            raise ValueError("rex response must be an object")
        return answer

    def register(self, config=None):
        config = self.config if config is None else config
        directories = {}
        for catalog in config["catalogs"]:
            kind = REX_KINDS[catalog["type"]]
            directories.setdefault(catalog["directory"], {"name": catalog["directory"]}).setdefault(kind, catalog["library"])
        answer = self.rex("PUT", "/rex/sources/" + config["source"],
                          {"url": config["relay_url"], "mount": config["mount"], "directories": list(directories.values())}, config)
        for ignored in answer.get("ignored") or []:
            if not isinstance(ignored, dict):
                continue
            LOG.warning("rex ignored %s: %s", safe_error(ignored.get("entry")), safe_error(ignored.get("reason")))

    def streams(self, kind, sid, config=None, also=()):
        """Every stream the addons that serve this item give, in the addons' order. Each addon is
        asked by the id it serves: its catalog's own id (`also`) first, as Stremio asks by the id
        a catalog gave, else the item's IMDb id."""
        config = self.config if config is None else config
        addons = []
        for addon in config["addons"]:
            if isinstance(addon, dict) and isinstance(addon.get("url"), str):
                named = next((one for one in [*also, sid] if serves(addon_manifest(addon), "stream", kind, one)), None)
                if named:
                    addons.append((addon, named))

        def ask(pair):
            addon, sid = pair
            manifest = addon_manifest(addon)
            try:
                answer = get_json("%s/stream/%s/%s.json" % (addon_base(addon["url"]), kind, urllib.parse.quote(sid)), 30)
                streams = answer.get("streams") if isinstance(answer, dict) else []
                return [stream for stream in streams or [] if isinstance(stream, dict)] if isinstance(streams, list) else []
            except Exception as error:
                LOG.warning("%s: %s: %s", sid, manifest.get("name") or "addon", safe_error(error))
                return []
        with ThreadPoolExecutor(max(len(addons), 1)) as pool:
            return [stream for answer in pool.map(ask, addons) for stream in answer]

    def resolve(self, vid):
        with self.config_guard:
            config, version = copy.deepcopy(self.config), self.config_version
        return self._resolve(vid, config, version)

    def _resolve(self, vid, config, version):
        """The link an item plays from now: the best stream that opens. Cached an hour."""
        with self.guard:
            lock = self.locks.setdefault(vid, threading.Lock())
        with lock:  # one ask per item; other items resolve meanwhile
            with self.guard:
                url, good_until = self.cache.get(vid, (None, 0))
            if time.time() < good_until:
                with self.guard:
                    headers = self.via.get(vid) or {}
                checked = opens(url, headers)
                if checked:
                    self.store_cache(version, vid, (checked, good_until), headers)
                    return checked
                with self.guard:
                    self.cache.pop(vid, None)
            with open(self.path("info", vid, config, version)) as handle:
                info = json.load(handle)
            refused = {entry.get("key") for entry in info.get("refused") or []
                       if isinstance(entry, dict) and time.time() - entry.get("at", 0) < 86400}
            for stream in ranked(self.streams(info["kind"], info["sid"], config, info.get("also") or ()), config, info.get("stream")):
                if stream_key(stream) in refused:
                    continue
                url = opens(stream["url"], request_headers(stream))
                if not url:
                    continue
                if not info.get("stream"):  # the first one that opens is what the probe describes
                    self.cache_update(self.path("info", vid, config, version), {"stream": stream_key(stream)}, version)
                elif stream_key(stream) != info["stream"]:
                    # The stream rex was told about is gone, and this one can differ (codec,
                    # container, a live channel's other mirror and its stream layout), so this
                    # play may fail. It is pinned, probed and pushed again on a sync started now.
                    LOG.warning("%s: probed stream gone, playing %s", vid, safe_stream_key(stream_key(stream)))
                    self.cache_update(self.path("info", vid, config, version), {"stream": stream_key(stream)}, version)
                    self.cache_remove(self.path(PROBES, vid, config, version), version)
                    self.wake.set()
                self.store_cache(version, vid, (url, max(time.time(), min(expiry(stream["url"]), expiry(url)) - 3600)),
                                 request_headers(stream))
                return url
            raise RuntimeError("no stream addon has a stream that opens")

    def route_vid(self, path):
        """Return an item id only for a configured directory, catalog and extension."""
        match = re.fullmatch(r"/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)\.([A-Za-z0-9]+)", path)
        if not match:
            return None
        directory, catalog_slug, vid, extension = match.groups()
        with self.config_guard:
            catalogs, cache = copy.deepcopy(self.config["catalogs"]), self.config["cache"]
        catalog = next((catalog for catalog in catalogs
                        if catalog["directory"] == directory and catalog["slug"] == catalog_slug), None)
        if not catalog:
            return None
        # An item id is its catalog's slug and the item's (an IMDb id, or an addon's own): the
        # longest slug it starts with is its catalog's (`cat-2-tt1` is cat-2's, not cat's).
        owner = max((other["slug"] for other in catalogs if vid.startswith(other["slug"] + "-")), key=len, default=None)
        if owner != catalog_slug or len(vid) <= len(catalog_slug) + 1:
            return None
        info_path = os.path.join(cache, "info", vid + ".json")
        if not os.path.isfile(info_path):
            return None
        if extension == "stream":
            return vid
        try:
            with open(info_path) as handle:
                info = json.load(handle)
            if not isinstance(info, dict) or info.get("kind") != catalog["type"]:
                return None
            expected = info.get("extension")
            if not expected:
                probe_path = os.path.join(cache, PROBES, vid + ".json")
                with open(probe_path) as handle:
                    probe = json.load(handle)
                expected = CONTAINERS.get((probe.get("format") or {}).get("format_name"))
            return vid if expected == extension else None
        except (AttributeError, OSError, ValueError, TypeError):
            return None

    def probe(self, vid, catalog, config=None):
        config = self.config if config is None else config
        url = rex_endpoint(config["relay_url"], "/%s/%s/%s.stream" % (catalog["directory"], catalog["slug"], vid))
        out = subprocess.run([config["ffprobe"], "-v", "error", *lenient_hls(config["ffprobe"]), "-print_format", "json",
                              "-show_format", "-show_streams", "-show_chapters", url], capture_output=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode(errors="replace").strip()[-300:] or "ffprobe failed")
        return json.loads(out.stdout)

    def catalog_items(self, catalog, config=None, version=None):
        """A catalog's films, or its series' episodes (Cinemeta's list of them), pushed by their
        IMDb ids so Plex names them; the catalog's own names and pictures are `display`, shown
        only until Plex names them. With `collections` on, a title an earlier catalog of the same
        library took (`self.taken`) is left to it; `self.catalog_listed` keeps the IMDb ids listed."""
        config = self.config if config is None else config
        self.catalog_listed = set()
        kind, limit = catalog["type"], catalog["limit"]
        complete = True
        answer = get_json(catalog["url"])
        if not isinstance(answer, dict) or not isinstance(answer.get("metas"), list):
            raise ValueError("catalog response has no metas list")
        if not answer["metas"]:
            # An empty answer can be a provider outage. Keep the last feed rows until a
            # non-empty response proves that this catalog is really empty.
            complete = False
        wanted, skipped = [], 0
        for meta in answer["metas"]:
            if not isinstance(meta, dict):
                complete = False
                continue
            if len(wanted) + skipped >= limit:
                continue
            if kind == "tv":
                # A channel is known only by the addon's own id (`ustv-…`, `pp-live:…`), which is
                # what its stream is asked by; the item id keeps only what a relay path can hold.
                if not isinstance(meta.get("id"), str) or not meta["id"]:
                    complete = False
                    continue
                wanted.append(("%s-%s" % (catalog["slug"], re.sub(r"[^A-Za-z0-9_-]", "-", meta["id"])), meta["id"], meta, None, (), None))
                continue
            own = meta["id"] if isinstance(meta.get("id"), str) else ""
            imdb = str(meta.get("imdb_id") or "")
            if not re.fullmatch(r"tt\d+", imdb):
                # An addon's own id can carry it (Cotonet's `cotonet:tt28942997`).
                found = re.search(r"(?<![A-Za-z0-9])tt\d{5,}(?!\d)", own)
                imdb = found[0] if found else (self.imdb_of(kind, own, config, version) if own else "")
            if not imdb and not own:
                complete = False
                continue
            # Ids carry the catalog: one film in two catalogs is two items, unless catalogs are
            # collections, when the first catalog of its library to list it holds it.
            if imdb and config.get("collections"):
                self.catalog_listed.add(imdb)
                if (catalog["directory"], kind, imdb) in self.taken:
                    # A film another catalog holds still counts as one of this catalog's.
                    skipped += kind == "movie"
                    continue
                self.taken.add((catalog["directory"], kind, imdb))
            if not imdb:
                # Only the addon knows it (One Pace, Sosac, Bamabin): it goes to rex named by the
                # catalog (`display`), and plays from the addons that serve its own id.
                label = re.sub(r"[^A-Za-z0-9_-]", "-", own)
                if kind == "movie":
                    wanted.append(("%s-%s" % (catalog["slug"], label), own, meta, None, (), None))
                    continue
                full = self.own_meta(kind, own, config)
                if not full:
                    complete = False
                    continue
            elif kind == "movie":
                wanted.append(("%s-%s" % (catalog["slug"], imdb), imdb, meta, None, (own,) if own and own != imdb else (), imdb))
                continue
            else:
                label = imdb
                try:
                    response = get_json("%s/meta/series/%s.json" % (CINEMETA, imdb))
                    full = response.get("meta") if isinstance(response, dict) else None
                    if not isinstance(full, dict):
                        raise ValueError("series response has no meta")
                except Exception as error:
                    complete = False
                    LOG.warning("%s: %s", imdb, safe_error(error))
                    continue
            raw_videos = full.get("videos")
            if not isinstance(raw_videos, list) or not raw_videos:
                complete = False
                continue
            episodes = []
            for raw_video in raw_videos:
                if not isinstance(raw_video, dict):
                    complete = False
                    continue
                try:
                    season, episode = int(raw_video.get("season")), int(raw_video.get("episode"))
                except (TypeError, ValueError):
                    complete = False
                    continue
                if season <= 0 or episode <= 0:
                    complete = False
                    continue
                episodes.append(dict(raw_video, season=season, episode=episode))
            episodes.sort(key=lambda video: (video["season"], video["episode"]))
            for video in episodes[:limit - len(wanted)]:
                vid = "%s-%s-%d-%d" % (catalog["slug"], label, video["season"], video["episode"])
                if imdb:
                    also = ("%s:%d:%d" % (own, video["season"], video["episode"]),) if own and own != imdb else ()
                    wanted.append((vid, "%s:%d:%d" % (imdb, video["season"], video["episode"]), dict(full, **meta), video, also, imdb))
                elif isinstance(video.get("id"), str) and video["id"]:
                    wanted.append((vid, video["id"], dict(full, **meta), video, (), None))
        items = []
        for vid, sid, meta, video, also, imdb in wanted:
            if self.stale(version):
                # A save replaced this pass's catalogs: the relay no longer routes their paths, so
                # every probe left would 404 (5,147 of them on one VPS, 2026-10-26).
                complete = False
                break
            info_path = self.path("info", vid, config, version)
            expected_info = dict({"kind": kind, "sid": sid}, **({"also": list(also)} if also else {}))
            try:
                info = self.cached("info", vid, lambda: expected_info, config, version)
            except Exception as error:
                complete = False
                LOG.warning("%s: %s", vid, safe_error(error))
                # A truncated JSON file is just as stale as a valid record for another item.
                # Repair it before the probe so the next relay request sees a usable identity.
                info = expected_info
                self.cache_write(info_path, info, version)
                self.cache_remove(self.path(PROBES, vid, config, version), version)
            info_valid = isinstance(info, dict) and info.get("kind") == kind and info.get("sid") == sid
            if info_valid and (info.get("also") or []) != list(also):
                self.cache_update(info_path, {"also": list(also)}, version)
            if not info_valid:
                # Repair identity before probing. A stale identity means the derived probe belongs
                # to a different source item and must not be reused for this one.
                complete = False
                info = expected_info
                self.cache_write(info_path, info, version)
                self.cache_remove(self.path(PROBES, vid, config, version), version)

            # A corrupt or shape-invalid derived probe is repairable. Remove just that record
            # and ask ffprobe once more; a real probe failure is still recorded as a partial
            # catalog without repeatedly hammering the source.
            probe_path = self.path(PROBES, vid, config, version)
            extension = None
            # A stream ffprobe cannot read or rex cannot take gives way to the next one: an
            # addon's best-labelled link was a .zip of another show (Eclipsia, 2026-10-24), and
            # pinned, it kept the title out of rex on every sync.
            for _ in range(3):
                probe = None
                for attempt in range(2):
                    had_cache = os.path.isfile(probe_path)
                    try:
                        probe = self.cached(PROBES, vid, lambda: self.probe(vid, catalog, config), config, version)
                    except Exception as error:  # no stream that opens, or one ffprobe could not read
                        complete = False
                        if attempt == 0 and had_cache:
                            self.cache_remove(probe_path, version)
                            continue
                        LOG.warning("%s: %s", vid, safe_error(error))
                        break
                    if isinstance(probe, dict) and isinstance(probe.get("format"), dict):
                        break
                    complete = False
                    if attempt == 0:
                        self.cache_remove(probe_path, version)
                        continue
                    LOG.warning("%s: ffprobe returned no format", vid)
                    probe = None
                if isinstance(probe, dict) and isinstance(probe.get("format"), dict):
                    extension = CONTAINERS.get(probe["format"].get("format_name"))
                    if extension and kind != "tv" and placeholder(probe):
                        LOG.warning("%s: a %ss clip, not the title", vid, probe["format"].get("duration"))
                        extension = None
                    if extension and not any(isinstance(stream, dict) and stream.get("codec_type") == "video"
                                             for stream in probe.get("streams") or []):
                        # Pluto's MST3K probed as a subtitle track alone (2026-10-24).
                        LOG.warning("%s: no picture in it", vid)
                        extension = None
                    if extension:
                        break
                    complete = False
                    LOG.warning("%s: a %s stream rex cannot take", vid, probe["format"].get("format_name"))
                if not self.refuse(vid, config, version):
                    break
            if not extension:
                continue
            if info.get("extension") != extension:
                if not self.cache_update(info_path, {"extension": extension}, version):
                    complete = False
                    continue
            try:
                items.append(stremio_item(config, catalog, vid, sid if kind == "tv" else imdb, meta, video, probe, extension))
            except Exception as error:
                complete = False
                LOG.warning("%s: %s", vid, safe_error(error))
        LOG.info("%s: %d of %d ready", safe_url(catalog["url"]), len(items), len(wanted))
        self.catalog_incomplete = not complete
        return items

    def once(self):
        with self.config_guard:
            config, version = copy.deepcopy(self.config), self.config_version
        return self._once(config, version)

    def _once(self, config, version):
        # Register is one serialized feed request. The catalog scan and ffprobe calls below take
        # neither publication_lock nor config_guard, so settings remain responsive while a source
        # is slow. Each later request rechecks the generation immediately before publishing.
        with self.publication_lock:
            with self.config_guard:
                if self.config_version != version:
                    return
            self.register(config)
        items = []
        incomplete = False
        self.taken, listed = set(), []
        for catalog in config["catalogs"]:
            before = len(items)
            try:
                items += self.catalog_items(catalog, config, version)
                listed.append((catalog, self.catalog_listed))
                incomplete |= self.catalog_incomplete
            except Exception as error:
                incomplete = True
                self.status = "failed at %s: %s" % (safe_error(catalog.get("title") or catalog["slug"]), safe_error(error))
                LOG.warning("%s: %s", safe_url(catalog["url"]), safe_error(error))
                continue
            if self.stale(version):
                self.status = "settings changed during sync; retrying"
                return
            # Each catalog shows in rex once it is checked, not hours later when the last one is;
            # the snapshot at the end still removes what is gone.
            for at in range(0, len(items) - before, 200):
                answer = self.publish(version, "POST", "/rex/feed/%s/items" % config["source"],
                                      {"items": items[before + at:before + at + 200]}, config)
                if answer is None:
                    self.status = "settings changed during sync; retrying"
                    return
                for rejected in answer.get("rejected") or []:
                    if isinstance(rejected, dict):
                        LOG.warning("rex rejected %s: %s", rejected.get("id"), safe_error(rejected.get("reason")))
        if config.get("collections"):
            # Every file of a title names all its collections: rex keeps the last list it was sent.
            for item in items:
                imdb = (item.get("ids") or {}).get("imdb")
                if imdb:
                    directory = item["path"].split("/", 1)[0]
                    kind = "movie" if item.get("kind") == "movie" else "series"
                    item["collections"] = [catalog.get("title") or catalog["slug"] for catalog, ids in listed
                                           if catalog["directory"] == directory and catalog["type"] == kind and imdb in ids]
        if incomplete:
            # Every item went up with its catalog; only the collections, known now, are left to send.
            chunks = [items[at:at + 200] for at in range(0, len(items), 200)] if config.get("collections") else []
            for chunk in chunks:
                answer = self.publish(version, "POST", "/rex/feed/%s/items" % config["source"], {"items": chunk}, config)
                if answer is None:
                    self.status = "settings changed during sync; retrying"
                    return
            self.status = "%s: partial sync, %d items upserted" % (time.strftime("%Y-%m-%d %H:%M"), len(items))
            LOG.warning("upserted %d items without removing existing rows", len(items))
            return
        session = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        chunks = [items[at:at + 200] for at in range(0, len(items), 200)] or [[]]
        for part, chunk in enumerate(chunks):
            body = {"session": session, "part": part, "complete": part == len(chunks) - 1, "items": chunk}
            if body["complete"] and self.force_next:
                body["force"] = True
            answer = self.publish(version, "PUT", "/rex/feed/%s/snapshot" % config["source"], body, config)
            if answer is None:
                self.status = "settings changed during sync; retrying"
                return
            for rejected in answer.get("rejected") or []:
                if not isinstance(rejected, dict):
                    continue
                LOG.warning("rex rejected %s: %s", rejected.get("id"), safe_error(rejected.get("reason")))
        with self.config_guard:
            if self.config_version == version:
                self.force_next = False
        self.status = "%s: %d items in rex" % (time.strftime("%Y-%m-%d %H:%M"), len(items))
        LOG.info("pushed %d items: %s", len(items), {key: answer.get(key) for key in ("created", "updated", "removed")})

    def forever(self):
        while True:
            self.status = "syncing since %s" % time.strftime("%H:%M")
            try:
                self.once()
            except Exception as error:
                self.status = "failed: %s" % safe_error(error)
                LOG.warning("sync: %s", safe_error(error))
            with self.config_guard:
                interval = self.config["interval_minutes"]
            self.wake.wait(interval * 60)
            self.wake.clear()


def fetch_text(url, agent):
    """A playlist's text and the link it was read from, after redirects."""
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(MAX_JSON_BYTES).decode("utf-8", "replace"), response.geturl()


def one_variant(text, base, cap=None):
    """An HLS master as its best variant alone (the highest within `cap`, then the most bandwidth),
    with the renditions it names and every link absolute; None for a media playlist or a master
    of one. rex takes a master's first video stream, the lowest or an unreadable variant (PlavioHD's
    height 0, PenguPlay's SD), and FFmpeg opened all of PenguPlay's four before a picture (20-30 s)."""
    lines = text.splitlines()
    variants = []
    for at, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            uri = next((other.strip() for other in lines[at + 1:] if other.strip() and not other.startswith("#")), None)
            height = re.search(r"RESOLUTION=\d+x(\d+)", line)
            bandwidth = re.search(r"(?<![-A-Z])BANDWIDTH=(\d+)", line)
            if uri:
                variants.append((line, uri, int(height[1]) if height else 0, int(bandwidth[1]) if bandwidth else 0))
    if len(variants) < 2:
        return None
    fitting = [variant for variant in variants if not cap or variant[2] <= int(cap)] or variants
    line, uri, _, _ = max(fitting, key=lambda variant: variant[2:])
    # One audio rendition (the default) and no subtitle playlists: rex maps tracks by the probe's
    # indexes, and Pluto TV's masters list a different number of renditions per session, so a
    # channel probed with four tracks before its picture played with fewer (2026-10-24).
    line = re.sub(r',?SUBTITLES="[^"]*"', "", line)
    groups = set(re.findall(r'(?:AUDIO|VIDEO|CLOSED-CAPTIONS)="([^"]+)"', line))
    kept = ["#EXTM3U"] + [other for other in lines if other.startswith(("#EXT-X-VERSION", "#EXT-X-INDEPENDENT-SEGMENTS"))]
    renditions = [other for other in lines if other.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" not in other
                  and (re.search(r'GROUP-ID="([^"]+)"', other) or [None, None])[1] in groups]
    audio = [other for other in renditions if "TYPE=AUDIO" in other]
    chosen = next((other for other in audio if "DEFAULT=YES" in other), audio[0] if audio else None)
    kept += [other for other in renditions if "TYPE=AUDIO" not in other or other is chosen]
    return rewrite_playlist("\n".join(kept + [line, uri]), base, lambda link: link)


def rewrite_playlist(text, base, relayed):
    """An HLS playlist with every link it names (segments, variants, `URI="..."` keys, maps and
    renditions) replaced by `relayed(absolute link)`."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(relayed(urllib.parse.urljoin(base, stripped)))
        elif stripped.startswith("#") and 'URI="' in stripped:
            out.append(re.sub(r'URI="([^"]+)"', lambda match: 'URI="%s"' % relayed(urllib.parse.urljoin(base, match[1])), line))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def own_network(handler):
    """Whether a settings request comes from this machine, the home network or Tailscale, to a host
    named by its address (a name could be another site rebinding to us), and not from another
    page's form."""
    try:
        client = ipaddress.ip_address(handler.client_address[0])
        host_header = handler.headers.get("Host") or ""
        parsed_host = urllib.parse.urlsplit("//" + host_header)
        if parsed_host.username or parsed_host.password or parsed_host.path or parsed_host.query or parsed_host.fragment:
            return False
        parsed_host.port  # reject an invalid port in a forged Host header
        host = parsed_host.hostname or ""
        if host != "localhost":
            ipaddress.ip_address(host)
    except ValueError:
        return False
    origin = handler.headers.get("Origin")
    if handler.command == "POST" and origin and origin != "http://" + handler.headers.get("Host"):
        return False
    return client.is_loopback or client.is_private or client in TAILSCALE


TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>rex-stremio</title>
<style>
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#fafafa;--muted:#6e6e73;--line:#d2d2d7;--accent:#b4500f;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root{--fg:#f5f5f7;--bg:#161617;--muted:#a1a1a6;--line:#3a3a3c;--accent:#ff9a62;--on-accent:#1a0d05}}
body{font:15px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:860px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p.muted,small{color:var(--muted)}
table{width:100%%;border-collapse:collapse}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
input[type=text],input[type=url],input[type=number],select{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit;max-width:100%%}
button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);cursor:pointer}
button.plain{background:transparent;color:var(--accent)}form.inline{display:inline}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.scroll{overflow-x:auto}input[type=checkbox]{accent-color:var(--accent)}
</style></head><body>
<h1>rex-stremio</h1><p class="muted">Stremio catalogs as rex libraries. Sync: %(status)s</p>
<h2>Addons</h2>
<p class="muted">Paste an addon's manifest link, as you would in Stremio. Catalogs come from any addon; streams from every addon that has them.</p>
<div class="scroll"><table><tr><th>Addon</th><th>Gives</th><th></th></tr>%(addons)s</table></div>
<form method="post" action="/addons" class="row" style="margin-top:8px">
<label for="add">Manifest link</label><input id="add" type="url" name="url" required placeholder="https://…/manifest.json" style="flex:1;min-width:220px">
<button>Add</button></form>
<form method="post" action="/settings">
<h2>Catalogs</h2>
<p class="muted">Each chosen catalog's films, or episodes, go into the library you name. Same name, same library.</p>
<div class="scroll"><table><tr><th>Take</th><th>Catalog</th><th>Library</th><th>How many</th></tr>%(catalogs)s</table></div>
<div class="row" style="margin-top:8px"><input type="checkbox" id="collections" name="collections" value="1"%(collections)s>
<label for="collections">Catalogs as collections: each catalog is a collection in its library, and a title in several is there once</label></div>
<h2>Streams</h2>
<p class="muted">The highest resolution within these limits plays; ties go by the addons' own order. If a link no longer opens, the next one plays.</p>
<div class="row"><label for="res">Highest resolution</label><select id="res" name="max_resolution">%(resolutions)s</select>
<label for="size">Largest file (GB)</label><input id="size" type="number" min="0" step="0.1" name="max_size_gb" value="%(max_size_gb)s" placeholder="any"></div>
<h2>rex</h2>
<div class="row"><label for="rex">rex's address</label><input id="rex" type="url" name="rex_url" value="%(rex_url)s" required style="flex:1;min-width:220px"></div>
<p style="margin-top:20px"><button>Save and sync</button></p>
</form></body></html>"""


def shown(url):
    """An addon's link without what may be a key in its path or query."""
    parts = urllib.parse.urlsplit(addon_base(url))
    return parts.hostname or "addon"


def page(config, status):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for at, addon in enumerate(config["addons"]):
        if not isinstance(addon, dict) or not isinstance(addon.get("url"), str):
            continue
        manifest = addon_manifest(addon)
        resources = manifest.get("resources") if isinstance(manifest.get("resources"), list) else []
        gives = [name for name in ("catalog", "stream") if any((entry if isinstance(entry, str) else entry.get("name")) == name
                                                                  for entry in resources if isinstance(entry, (str, dict)))]
        rows.append('<tr><td>%s<br><small>%s</small></td><td>%s</td><td><form class="inline" method="post" action="/addons/remove">'
                    '<input type="hidden" name="at" value="%d"><button class="plain">Remove</button></form></td></tr>'
                    % (esc(manifest.get("name") or "addon"), esc(shown(addon["url"])), esc(", ".join(gives) or "nothing rex uses"), at))
    taken = {catalog["url"]: catalog for catalog in config["catalogs"]}
    catalogs, n = [], 0
    for addon in config["addons"]:
        if not isinstance(addon, dict) or not isinstance(addon.get("url"), str):
            continue
        manifest = addon_manifest(addon)
        for catalog in catalogs_of(manifest):
            url = "%s/catalog/%s/%s.json" % (addon_base(addon["url"]), catalog["type"], catalog["id"])
            chosen = taken.get(url) or {}
            library = chosen.get("library") or LIBRARIES[catalog["type"]]
            addon_name = manifest.get("name") or "addon"
            name = "%s: %s (%s)" % (addon_name, catalog["name"],
                                    {"movie": "films", "series": "episodes", "tv": "channels"}[catalog["type"]])
            catalogs.append('<tr><td><input type="checkbox" id="c%d" name="take" value="%d"%s><input type="hidden" name="url%d" value="%s">'
                            '<input type="hidden" name="title%d" value="%s"><input type="hidden" name="slug%d" value="%s">'
                            '<input type="hidden" name="directory%d" value="%s"></td><td><label for="c%d">%s</label></td>'
                            '<td><input type="text" name="library%d" value="%s" aria-label="Library"></td>'
                            '<td><input type="number" min="1" name="limit%d" value="%s" aria-label="How many" style="width:5em"></td></tr>'
                            % (n, n, " checked" if chosen else "", n, esc(url), n, esc(addon_name + " " + catalog["name"]),
                               n, esc(chosen.get("slug") or ""), n, esc(chosen.get("directory") or ""), n, esc(name),
                               n, esc(library), n, esc(chosen.get("limit") or 50)))
            n += 1
    resolutions = "".join('<option value="%s"%s>%s</option>' % (value, " selected" if str(config.get("max_resolution") or "") == value else "", label)
                          for value, label in (("", "any"), ("2160", "2160p (4K)"), ("1080", "1080p"), ("720", "720p")))
    return PAGE % {"status": esc(safe_error(status)), "addons": "".join(rows) or '<tr><td colspan="3"><small>None yet.</small></td></tr>',
                   "catalogs": "".join(catalogs) or '<tr><td colspan="4"><small>Add an addon with catalogs, such as Cinemeta.</small></td></tr>',
                   "resolutions": resolutions, "collections": " checked" if config.get("collections") else "", "max_size_gb": esc("%g" % config["max_size_gb"] if config.get("max_size_gb") else ""),
                   # The settings field is an authorized private input and must round-trip exactly;
                   # safe_url is reserved for logs/status and addon display.
                   "rex_url": esc(config["rex_url"])}


class App:
    """The relay and the settings page, on one listener."""

    def __init__(self, config_path, config, sync):
        self.config_path, self.config, self.sync = config_path, config, sync
        self.saving = threading.RLock()

    def settings(self, form):
        """The settings form -> the config, saved, then a sync."""
        with self.saving:
            values = dict(self.config)
            catalogs = []
            for at in form.get("take", []):
                url = form.get("url" + at, [""])[0]
                limit = form.get("limit" + at, ["50"])[0]
                catalog = {"url": url, "title": form.get("title" + at, [""])[0] or None,
                           "library": form.get("library" + at, [""])[0].strip() or None,
                           "limit": int(limit) if limit.isdigit() and int(limit) > 0 else 50}
                for key in ("slug", "directory"):
                    value = form.get(key + at, [""])[0]
                    if value:
                        catalog[key] = value
                previous = next((saved for saved in self.config["catalogs"] if saved["url"] == url), None)
                if (previous and catalog.get("directory") == previous.get("directory")
                        and catalog.get("library") != previous.get("library")
                        and previous.get("directory") == slug(previous.get("library", ""))):
                    # The hidden directory is a derived value from the prior library. Re-derive it
                    # when that label changes; a different prior directory is a deliberate override.
                    catalog["directory"] = slug(catalog.get("library") or "")
                catalogs.append(catalog)
            values["catalogs"] = [{key: value for key, value in catalog.items() if value} for catalog in catalogs]
            values["collections"] = form.get("collections", [""])[0] == "1"
            values["max_resolution"] = int(form["max_resolution"][0]) if form.get("max_resolution", [""])[0].isdigit() else None
            size = form.get("max_size_gb", [""])[0]
            values["max_size_gb"] = float(size) if re.fullmatch(r"\d+(\.\d+)?", size) and float(size) > 0 else None
            rex_url = form.get("rex_url", [""])[0].strip()
            if not rex_url.startswith(("http://", "https://")):
                raise ValueError("rex_url must be an http(s) URL")
            values["rex_url"] = rex_url
            self.save(values)

    def add_addon(self, url):
        base = addon_base(url)
        if not base.startswith(("http://", "https://")):
            raise ValueError("not an addon link")
        manifest = get_json(base + "/manifest.json", 30)
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str)
                or not manifest["id"] or not isinstance(manifest.get("resources"), list)):
            raise ValueError("no Stremio manifest at that link")
        with self.saving:
            addons = [addon for addon in self.config["addons"]
                      if isinstance(addon, dict) and addon_base(addon.get("url")) != base]
            self.save(dict(self.config, addons=addons + [{"url": base + "/manifest.json", "manifest": manifest}]), sync=False)

    def remove_addon(self, at):
        with self.saving:
            if not isinstance(at, int) or at < 0 or at >= len(self.config["addons"]):
                raise ValueError("addon index is out of range")
            addons = list(self.config["addons"])
            gone = addons.pop(at)
            prefix = addon_base(gone["url"]) + "/catalog/"
            self.save(dict(self.config, addons=addons,
                           catalogs=[c for c in self.config["catalogs"] if not c["url"].startswith(prefix)]))

    def save(self, values, sync=True):
        with self.saving:
            fresh = validate_config(self.config_path, values)
            with self.sync.publication_lock:
                with self.sync.config_guard:
                    # Persist the same normalized identity that is installed in memory. This makes
                    # the first save durable across restart and keeps later reorder/remove stable.
                    save_config(self.config_path, fresh)
                    self.config.clear()
                    self.config.update(fresh)
                    self.sync.config_version += 1
                    self.sync.force_next = True
                    with self.sync.guard:
                        self.sync.cache.clear()
        if sync:
            self.sync.wake.set()

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/":
                    if not own_network(self):
                        self.send_error(403)
                        return
                    with app.sync.config_guard:
                        body = page(app.config, app.sync.status).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                relayed = re.fullmatch(r"/_relay/([A-Za-z0-9_-]+)/([0-9a-f]{20})\.[A-Za-z0-9]+", path)
                if relayed:
                    url, headers = app.sync.relayed_link(*relayed.groups())
                    if not url:
                        self.send_error(404)
                        return
                    self.relay(relayed[1], url, headers)
                    return
                vid = app.sync.route_vid(path)
                if not vid:
                    self.send_error(404)
                    return
                try:
                    url = app.sync.resolve(vid)
                except Exception as error:
                    LOG.warning("resolve %s: %s", vid, safe_error(error))
                    self.send_error(502, "No stream addon has a stream that opens")
                    return
                with app.sync.guard:
                    headers = app.sync.via.get(vid) or {}
                if headers:
                    self.relay(vid, url, headers)
                    return
                master = self.trimmed(url, self.headers.get("User-Agent")) if urllib.parse.urlsplit(url).path.endswith(".m3u8") else None
                if master:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(master)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(master)
                    return
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_HEAD = do_GET

            def trimmed(self, url, agent):
                """An HLS master's best variant alone (`one_variant`), or None to send rex to it.
                Asked as the player asks: ABC's Akamai links name a variant only the User-Agent
                that fetched the master may read (403 for any other, 2026-10-24)."""
                try:
                    text, final = fetch_text(url, agent or "rex-stremio")
                except Exception as error:
                    LOG.info("%s: %s", safe_url(url), safe_error(error))
                    return None
                master = one_variant(text, final, app.config.get("max_resolution")) if text.lstrip().startswith("#EXTM3U") else None
                return master.encode() if master else None

            def relay(self, vid, url, headers):
                """A link that needs its headers, fetched here: bytes as they come (the client's
                Range passed on), a playlist with every link it names sent back through here."""
                ask = dict({"User-Agent": "rex-stremio"}, **headers)
                if self.headers.get("Range"):
                    ask["Range"] = self.headers["Range"]
                try:
                    upstream = urllib.request.urlopen(urllib.request.Request(url, headers=ask), timeout=30)
                except urllib.error.HTTPError as error:
                    self.send_error(error.code if 400 <= error.code < 600 else 502)
                    return
                except Exception as error:
                    LOG.warning("relay %s: %s", vid, safe_error(error))
                    self.send_error(502)
                    return
                with upstream:
                    kind = upstream.headers.get("Content-Type") or ""
                    if "mpegurl" in kind.lower() or urllib.parse.urlsplit(upstream.geturl()).path.endswith(".m3u8"):
                        text = upstream.read(MAX_JSON_BYTES).decode("utf-8", "replace")
                        if text.lstrip().startswith("#EXTM3U"):
                            text = one_variant(text, upstream.geturl(), app.config.get("max_resolution")) or text
                            body = rewrite_playlist(text, upstream.geturl(), lambda link: app.sync.remember(vid, link)).encode()
                            self.send_response(200)
                            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            if self.command != "HEAD":
                                self.wfile.write(body)
                            return
                        raw = text.encode()
                        self.send_response(upstream.status)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(raw)
                        return
                    self.send_response(upstream.status)
                    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                        if upstream.headers.get(name):
                            self.send_header(name, upstream.headers[name])
                    self.end_headers()
                    if self.command == "HEAD":
                        return
                    try:
                        while True:
                            chunk = upstream.read(1 << 16)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def do_POST(self):
                if not own_network(self):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self.send_error(400, "invalid request")
                    return
                if length < 0:
                    self.send_error(400, "invalid request")
                    return
                if length > MAX_FORM_BYTES:
                    self.send_error(413, "request body too large")
                    return
                try:
                    form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"),
                                                 max_num_fields=MAX_FORM_FIELDS)
                except ValueError:
                    self.send_error(400, "invalid request")
                    return
                path = urllib.parse.urlsplit(self.path).path
                try:
                    if path == "/addons":
                        app.add_addon(form.get("url", [""])[0])
                    elif path == "/addons/remove":
                        value = form.get("at", [""])[0]
                        if not re.fullmatch(r"\d+", value):
                            raise ValueError("addon index is out of range")
                        app.remove_addon(int(value))
                    elif path == "/settings":
                        app.settings(form)
                    else:
                        self.send_error(404)
                        return
                except Exception as error:
                    LOG.warning("settings: %s", safe_error(error))
                    self.send_error(400, "invalid request")
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true", help="sync once while the relay runs, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not os.path.exists(args.config):
        save_config(args.config, {"listen": os.environ.get("REX_STREMIO_LISTEN") or DEFAULTS["listen"]})
    config = load_config(args.config)
    sync = Sync(config)
    host, port = config["listen"].rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), App(args.config, config, sync).handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("relay and settings on http://%s/, %d catalog(s), rex at %s", config["listen"], len(config["catalogs"]), safe_url(config["rex_url"]))
    if args.once:
        sync.once()
        return 0
    sync.forever()


if __name__ == "__main__":
    sys.exit(main())
