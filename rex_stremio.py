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
import copy
import functools
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
    "max_size_gb": None,
    "addons": [],
    "catalogs": [],
}

# Series' seasons and episodes come from Cinemeta, whose ids are IMDb's.
CINEMETA = "https://v3-cinemeta.strem.io"

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


def opens(url):
    """The link a stream ends at, when it opens; None when it does not (an expired or refused link)."""
    request = urllib.request.Request(url, headers={"User-Agent": "rex-stremio", "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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


def expiry(url):
    """A googlevideo manifest names its own expiry (`/expire/<unix>/`); two hours if it does not."""
    match = re.search(r"/expire/(\d+)/", url)
    return int(match[1]) if match else time.time() + 7200


def stremio_item(config, catalog, vid, imdb, meta, video, probe, extension):
    """One feed item (`docs/feed-api.md`): placed by its IMDb id, dressed by the catalog. A live
    channel has no IMDb id (`imdb` is the addon's own id): it goes unplaced, named by `display`."""
    path = "%s/%s/%s.%s" % (catalog["directory"], catalog["slug"], vid, extension)
    pushed = {
        "id": "st-" + vid,
        "path": path,
        "file": "%s/%s" % (config["mount"].rstrip("/"), path),
        "name": meta.get("name") or imdb,
        "release": "stremio:" + imdb,
        "media_info": probe,
    }
    if catalog["type"] != "tv":
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
        self.config_guard = threading.RLock()
        # Feed publication and settings commits share this lock. Readers never take it, so a
        # stalled rex request cannot stop the relay or settings page from serving.
        self.publication_lock = threading.RLock()
        self.config_version = 0
        self.catalog_incomplete = False

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

    def store_cache(self, version, vid, value):
        with self.config_guard:
            if self.config_version != version:
                return
            with self.guard:
                self.cache[vid] = value

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

    def streams(self, kind, sid, config=None):
        """Every stream the addons that serve this item give, in the addons' order."""
        config = self.config if config is None else config
        addons = [addon for addon in config["addons"]
                  if isinstance(addon, dict) and isinstance(addon.get("url"), str)
                  and serves(addon_manifest(addon), "stream", kind, sid)]

        def ask(addon):
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
                checked = opens(url)
                if checked:
                    self.store_cache(version, vid, (checked, good_until))
                    return checked
                with self.guard:
                    self.cache.pop(vid, None)
            with open(self.path("info", vid, config, version)) as handle:
                info = json.load(handle)
            for stream in ranked(self.streams(info["kind"], info["sid"], config), config, info.get("stream")):
                url = opens(stream["url"])
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
                    self.cache_remove(self.path("probes", vid, config, version), version)
                    self.wake.set()
                self.store_cache(version, vid, (url, max(time.time(), min(expiry(stream["url"]), expiry(url)) - 3600)))
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
        suffix = vid[len(catalog_slug) + 1:] if vid.startswith(catalog_slug + "-") else ""
        pattern = {"movie": r"tt\d+", "series": r"tt\d+-\d+-\d+", "tv": r"[A-Za-z0-9_-]+"}[catalog["type"]]
        if not re.fullmatch(pattern, suffix):
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
                probe_path = os.path.join(cache, "probes", vid + ".json")
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
        only until Plex names them."""
        config = self.config if config is None else config
        kind, limit = catalog["type"], catalog["limit"]
        complete = True
        answer = get_json(catalog["url"])
        if not isinstance(answer, dict) or not isinstance(answer.get("metas"), list):
            raise ValueError("catalog response has no metas list")
        if not answer["metas"]:
            # An empty answer can be a provider outage. Keep the last feed rows until a
            # non-empty response proves that this catalog is really empty.
            complete = False
        wanted = []
        for meta in answer["metas"]:
            if not isinstance(meta, dict):
                complete = False
                continue
            if len(wanted) >= limit:
                continue
            if kind == "tv":
                # A channel is known only by the addon's own id (`ustv-…`, `pp-live:…`), which is
                # what its stream is asked by; the item id keeps only what a relay path can hold.
                if not isinstance(meta.get("id"), str) or not meta["id"]:
                    complete = False
                    continue
                wanted.append(("%s-%s" % (catalog["slug"], re.sub(r"[^A-Za-z0-9_-]", "-", meta["id"])), meta["id"], meta, None))
                continue
            imdb = str(meta.get("imdb_id") or meta.get("id") or "").split(":")[0]
            if not re.fullmatch(r"tt\d+", imdb):
                # A non-IMDb row cannot be represented in rex. Keep the old row until a
                # later complete catalog response tells us it is really gone.
                complete = False
                continue  # rex identifies by IMDb id; a catalog's own ids name nothing to it
            # Ids carry the catalog: one film in two catalogs is two items.
            if kind == "movie":
                wanted.append(("%s-%s" % (catalog["slug"], imdb), imdb, meta, None))
                continue
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
                wanted.append(("%s-%s-%d-%d" % (catalog["slug"], imdb, video["season"], video["episode"]),
                               "%s:%d:%d" % (imdb, video["season"], video["episode"]), dict(full, **meta), video))
        items = []
        for vid, sid, meta, video in wanted:
            info_path = self.path("info", vid, config, version)
            expected_info = {"kind": kind, "sid": sid}
            try:
                info = self.cached("info", vid, lambda: expected_info, config, version)
            except Exception as error:
                complete = False
                LOG.warning("%s: %s", vid, safe_error(error))
                # A truncated JSON file is just as stale as a valid record for another item.
                # Repair it before the probe so the next relay request sees a usable identity.
                info = expected_info
                self.cache_write(info_path, info, version)
                self.cache_remove(self.path("probes", vid, config, version), version)
            info_valid = isinstance(info, dict) and info.get("kind") == kind and info.get("sid") == sid
            if not info_valid:
                # Repair identity before probing. A stale identity means the derived probe belongs
                # to a different source item and must not be reused for this one.
                complete = False
                info = expected_info
                self.cache_write(info_path, info, version)
                self.cache_remove(self.path("probes", vid, config, version), version)

            # A corrupt or shape-invalid derived probe is repairable. Remove just that record
            # and ask ffprobe once more; a real probe failure is still recorded as a partial
            # catalog without repeatedly hammering the source.
            probe_path = self.path("probes", vid, config, version)
            probe = None
            for attempt in range(2):
                had_cache = os.path.isfile(probe_path)
                try:
                    probe = self.cached("probes", vid, lambda: self.probe(vid, catalog, config), config, version)
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
            if not isinstance(probe, dict) or not isinstance(probe.get("format"), dict):
                continue
            extension = CONTAINERS.get(probe["format"].get("format_name"))
            if not extension:
                complete = False
                LOG.warning("%s: a %s stream rex cannot take", vid, (probe.get("format") or {}).get("format_name"))
                continue
            if info.get("extension") != extension:
                if not self.cache_update(info_path, {"extension": extension}, version):
                    complete = False
                    continue
            try:
                items.append(stremio_item(config, catalog, vid, sid if kind == "tv" else sid.split(":")[0], meta, video, probe, extension))
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
        for catalog in config["catalogs"]:
            try:
                items += self.catalog_items(catalog, config, version)
                incomplete |= self.catalog_incomplete
            except Exception as error:
                incomplete = True
                self.status = "failed at %s: %s" % (safe_error(catalog.get("title") or catalog["slug"]), safe_error(error))
                LOG.warning("%s: %s", safe_url(catalog["url"]), safe_error(error))
        if incomplete:
            chunks = [items[at:at + 200] for at in range(0, len(items), 200)] or [[]]
            for chunk in chunks:
                answer = self.publish(version, "POST", "/rex/feed/%s/items" % config["source"], {"items": chunk}, config)
                if answer is None:
                    self.status = "settings changed during sync; retrying"
                    return
                for rejected in answer.get("rejected") or []:
                    if not isinstance(rejected, dict):
                        continue
                    LOG.warning("rex rejected %s: %s", rejected.get("id"), safe_error(rejected.get("reason")))
            self.status = "%s: partial sync, %d items upserted" % (time.strftime("%Y-%m-%d %H:%M"), len(items))
            LOG.warning("upserted %d items without removing existing rows", len(items))
            return
        session = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        chunks = [items[at:at + 200] for at in range(0, len(items), 200)] or [[]]
        for part, chunk in enumerate(chunks):
            answer = self.publish(version, "PUT", "/rex/feed/%s/snapshot" % config["source"],
                                  {"session": session, "part": part, "complete": part == len(chunks) - 1, "items": chunk}, config)
            if answer is None:
                self.status = "settings changed during sync; retrying"
                return
            for rejected in answer.get("rejected") or []:
                if not isinstance(rejected, dict):
                    continue
                LOG.warning("rex rejected %s: %s", rejected.get("id"), safe_error(rejected.get("reason")))
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
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#fafafa;--muted:#6e6e73;--line:#d2d2d7;--accent:#0a66c2}
@media (prefers-color-scheme:dark){:root{--fg:#f5f5f7;--bg:#161617;--muted:#a1a1a6;--line:#3a3a3c;--accent:#4c9ee8}}
body{font:15px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:860px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p.muted,small{color:var(--muted)}
table{width:100%%;border-collapse:collapse}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
input[type=text],input[type=url],input[type=number],select{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit;max-width:100%%}
button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
button.plain{background:transparent;color:var(--accent)}form.inline{display:inline}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.scroll{overflow-x:auto}
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
                   "resolutions": resolutions, "max_size_gb": esc(config.get("max_size_gb") or ""),
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
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_HEAD = do_GET

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
