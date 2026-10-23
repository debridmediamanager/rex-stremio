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
import html
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("rex-stremio")

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

RESOLUTIONS = [(r"2160p|\b4k\b|\buhd\b", 2160), (r"1440p", 1440), (r"1080p", 1080), (r"720p", 720), (r"576p|480p|\bsd\b", 480)]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "catalog"


def get_json(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": "rex-stremio", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def addon_base(url):
    """A manifest's url as Stremio shows it (`stremio://`, `.../manifest.json`) -> the addon's base."""
    url = url.strip()
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    return url.removesuffix("/manifest.json").rstrip("/")


def serves(manifest, resource, kind, sid):
    """Whether an addon answers `resource` for `kind` and this id, by its manifest."""
    for entry in manifest.get("resources") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if entry.get("name") != resource:
            continue
        types = entry.get("types") or manifest.get("types") or []
        prefixes = entry.get("idPrefixes") or manifest.get("idPrefixes")
        if kind in types and (not prefixes or any(sid.startswith(prefix) for prefix in prefixes)):
            return True
    return False


def catalogs_of(manifest):
    """The catalogs of films and series an addon lists without being asked something first (a
    search catalog needs a query)."""
    out = []
    for catalog in manifest.get("catalogs") or []:
        required = [extra for extra in catalog.get("extra") or [] if extra.get("isRequired")]
        required += [name for name in catalog.get("extraRequired") or []]
        if catalog.get("type") in ("movie", "series") and catalog.get("id") and not required:
            out.append({"type": catalog["type"], "id": catalog["id"], "name": catalog.get("name") or catalog["id"]})
    return out


def quality(stream):
    """(resolution, size in bytes) as the stream's labels give them; 0 or None where they do not."""
    hints = stream.get("behaviorHints") or {}
    text = " ".join(str(stream.get(key) or "") for key in ("name", "title", "description")) + " " + str(hints.get("filename") or "")
    resolution = next((value for pattern, value in RESOLUTIONS if re.search(pattern, text, re.I)), 0)
    size = hints.get("videoSize")
    if not size:
        found = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", text, re.I)
        size = found and float(found[1]) * (1 << (30 if found[2].upper() == "GB" else 20))
    return resolution, int(size) if size else None


def stream_key(stream):
    """What names one stream across asks: its file, else its group, else its link."""
    hints = stream.get("behaviorHints") or {}
    return hints.get("filename") or hints.get("bingeGroup") or stream.get("url")


def ranked(streams, config, pinned=None):
    """The streams rex can read, best first: an http(s) `url` (a torrent's `infoHash`, a YouTube
    `ytId` and an `externalUrl` are not files), within the configured limits, highest
    resolution first, the addons' own order breaking ties. The stream an item was probed from
    (`pinned`) goes first, so what plays is what rex was told about."""
    cap = config.get("max_resolution")
    biggest = config.get("max_size_gb") and float(config["max_size_gb"]) * (1 << 30)
    kept = []
    for at, stream in enumerate(streams):
        if not str(stream.get("url", "")).startswith(("http://", "https://")):
            continue
        resolution, size = quality(stream)
        if (cap and resolution > int(cap)) or (biggest and size and size > biggest):
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
        LOG.info("%s: %s", url.split("?")[0][:80], error)
        return None


def load_config(path):
    with open(path) as handle:
        config = dict(DEFAULTS, **json.load(handle))
    if config["cache"] and not os.path.isabs(config["cache"]):
        config["cache"] = os.path.join(os.path.dirname(os.path.abspath(path)), config["cache"])
    if not config["relay_url"]:
        config["relay_url"] = "http://" + config["listen"]
    catalogs = []
    for catalog in config["catalogs"]:
        found = re.fullmatch(r"(https?://.+?)/catalog/(movie|series)/([^/]+?)\.json", catalog.get("url", ""))
        if not found:
            raise ValueError("catalog %s: not a Stremio catalog url (<addon>/catalog/<movie|series>/<id>.json)" % catalog.get("url"))
        catalog = dict({"library": "Stremio Films" if found[2] == "movie" else "Stremio Shows", "limit": 50}, **catalog)
        catalog.setdefault("slug", slug(catalog.get("title") or found[3]))
        while any(other["slug"] == catalog["slug"] for other in catalogs):  # item ids start with it
            catalog["slug"] += "-2"
        catalog.setdefault("directory", slug(catalog["library"]))
        catalog["type"] = found[2]
        catalogs.append(catalog)
    config["catalogs"] = catalogs
    return config


def save_config(path, values):
    """The settings as written, mode 600: addons' urls can hold debrid keys. What `load_config`
    derives (a catalog's type, slug and directory; the default relay url) is left out."""
    raw = dict(values, catalogs=[{key: value for key, value in catalog.items() if key not in ("type", "slug", "directory")}
                                 for catalog in values.get("catalogs") or []])
    if raw.get("relay_url") == "http://%s" % raw.get("listen"):
        del raw["relay_url"]
    descriptor = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(raw, handle, indent=2)
    os.replace(path + ".tmp", path)


def expiry(url):
    """A googlevideo manifest names its own expiry (`/expire/<unix>/`); two hours if it does not."""
    match = re.search(r"/expire/(\d+)/", url)
    return int(match[1]) if match else time.time() + 7200


def stremio_item(config, catalog, vid, imdb, meta, video, probe, extension):
    """One feed item (`docs/feed-api.md`): placed by its IMDb id, dressed by the catalog."""
    path = "%s/%s/%s.%s" % (catalog["directory"], catalog["slug"], vid, extension)
    pushed = {
        "id": "st-" + vid,
        "path": path,
        "file": "%s/%s" % (config["mount"].rstrip("/"), path),
        "name": meta.get("name") or imdb,
        "release": "stremio:" + imdb,
        "ids": {"imdb": imdb},
        "media_info": probe,
    }
    size = (probe.get("format") or {}).get("size")
    if size and extension != "m3u8":
        pushed["size"] = int(size)
    released = lambda value: str(value or "")[:10] if re.match(r"\d{4}-\d{2}-\d{2}", str(value or "")) else None
    year = re.match(r"\d{4}", str(meta.get("year") or meta.get("releaseInfo") or ""))
    if catalog["type"] == "movie":
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

    def path(self, kind, vid):
        os.makedirs(os.path.join(self.config["cache"], kind), exist_ok=True)
        return os.path.join(self.config["cache"], kind, vid + ".json")

    def cached(self, kind, vid, make):
        path = self.path(kind, vid)
        if not os.path.exists(path):
            value = make()
            with open(path + ".tmp", "w") as handle:
                json.dump(value, handle)
            os.replace(path + ".tmp", path)
        with open(path) as handle:
            return json.load(handle)

    def rex(self, method, path, body=None):
        request = urllib.request.Request(self.config["rex_url"].rstrip("/") + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read() or b"{}")

    def register(self):
        directories = {}
        for catalog in self.config["catalogs"]:
            kind = "movies" if catalog["type"] == "movie" else "shows"
            directories.setdefault(catalog["directory"], {"name": catalog["directory"]}).setdefault(kind, catalog["library"])
        answer = self.rex("PUT", "/rex/sources/" + self.config["source"],
                          {"url": self.config["relay_url"], "mount": self.config["mount"], "directories": list(directories.values())})
        for ignored in answer.get("ignored") or []:
            LOG.warning("rex ignored %s: %s", ignored.get("entry"), ignored.get("reason"))

    def streams(self, kind, sid):
        """Every stream the addons that serve this item give, in the addons' order."""
        addons = [addon for addon in self.config["addons"] if serves(addon.get("manifest") or {}, "stream", kind, sid)]

        def ask(addon):
            try:
                return get_json("%s/stream/%s/%s.json" % (addon_base(addon["url"]), kind, urllib.parse.quote(sid)), 30).get("streams") or []
            except Exception as error:
                LOG.warning("%s: %s: %s", sid, (addon.get("manifest") or {}).get("name") or "addon", error)
                return []
        with ThreadPoolExecutor(max(len(addons), 1)) as pool:
            return [stream for answer in pool.map(ask, addons) for stream in answer]

    def resolve(self, vid):
        """The link an item plays from now: the best stream that opens. Cached an hour."""
        with self.guard:
            lock = self.locks.setdefault(vid, threading.Lock())
        with lock:  # one ask per item; other items resolve meanwhile
            url, good_until = self.cache.get(vid, (None, 0))
            if time.time() < good_until:
                return url
            with open(self.path("info", vid)) as handle:
                info = json.load(handle)
            for stream in ranked(self.streams(info["kind"], info["sid"]), self.config, info.get("stream")):
                url = opens(stream["url"])
                if not url:
                    continue
                if not info.get("stream"):  # the first one that opens is what the probe describes
                    with open(self.path("info", vid), "w") as handle:
                        json.dump(dict(info, stream=stream_key(stream)), handle)
                elif stream_key(stream) != info["stream"]:
                    # ponytail: a fallback stream can differ from the probe rex holds (codec,
                    # container); delete cache/probes/<id>.json to re-probe if one will not play.
                    LOG.warning("%s: probed stream gone, playing %s", vid, stream_key(stream))
                self.cache[vid] = (url, expiry(url) - 3600)
                return url
            raise RuntimeError("no stream addon has a stream that opens")

    def probe(self, vid, catalog):
        url = "%s/%s/%s/%s.stream" % (self.config["relay_url"].rstrip("/"), catalog["directory"], catalog["slug"], vid)
        out = subprocess.run([self.config["ffprobe"], "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                              "-show_chapters", url], capture_output=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode(errors="replace").strip()[-300:] or "ffprobe failed")
        return json.loads(out.stdout)

    def catalog_items(self, catalog):
        """A catalog's films, or its series' episodes (Cinemeta's list of them), pushed by their
        IMDb ids so Plex names them; the catalog's own names and pictures are `display`, shown
        only until Plex names them."""
        kind, limit = catalog["type"], catalog["limit"]
        wanted = []
        for meta in get_json(catalog["url"]).get("metas") or []:
            imdb = str(meta.get("imdb_id") or meta.get("id") or "").split(":")[0]
            if not re.fullmatch(r"tt\d+", imdb) or len(wanted) >= limit:
                continue  # rex identifies by IMDb id; a catalog's own ids name nothing to it
            # Ids carry the catalog: one film in two catalogs is two items.
            if kind == "movie":
                wanted.append(("%s-%s" % (catalog["slug"], imdb), imdb, meta, None))
                continue
            try:
                full = get_json("%s/meta/series/%s.json" % (CINEMETA, imdb)).get("meta") or {}
            except Exception as error:
                LOG.warning("%s: %s", imdb, error)
                continue
            episodes = sorted((video for video in full.get("videos") or [] if video.get("season") and video.get("episode")),
                              key=lambda video: (video["season"], video["episode"]))
            for video in episodes[:limit - len(wanted)]:
                wanted.append(("%s-%s-%d-%d" % (catalog["slug"], imdb, video["season"], video["episode"]),
                               "%s:%d:%d" % (imdb, video["season"], video["episode"]), dict(full, **meta), video))
        items = []
        for vid, sid, meta, video in wanted:
            self.cached("info", vid, lambda: {"kind": kind, "sid": sid})
            try:
                probe = self.cached("probes", vid, lambda: self.probe(vid, catalog))
            except Exception as error:  # no stream that opens, or one ffprobe could not read
                LOG.warning("%s: %s", vid, error)
                continue
            extension = CONTAINERS.get((probe.get("format") or {}).get("format_name"))
            if not extension:
                LOG.warning("%s: a %s stream rex cannot take", vid, (probe.get("format") or {}).get("format_name"))
                continue
            items.append(stremio_item(self.config, catalog, vid, sid.split(":")[0], meta, video, probe, extension))
        LOG.info("%s: %d of %d ready", catalog["url"].split("/catalog/")[-1], len(items), len(wanted))
        return items

    def once(self):
        self.register()
        items = []
        for catalog in self.config["catalogs"]:
            try:
                items += self.catalog_items(catalog)
            except Exception as error:
                self.status = "failed at %s: %s" % (catalog.get("title") or catalog["slug"], error)
                LOG.warning("%s: %s", catalog["url"].split("/catalog/")[-1], error)
                return  # a partial snapshot would remove the catalog that failed
        session = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        chunks = [items[at:at + 200] for at in range(0, len(items), 200)] or [[]]
        for part, chunk in enumerate(chunks):
            answer = self.rex("PUT", "/rex/feed/%s/snapshot" % self.config["source"],
                              {"session": session, "part": part, "complete": part == len(chunks) - 1, "items": chunk})
            for rejected in answer.get("rejected") or []:
                LOG.warning("rex rejected %s: %s", rejected.get("id"), rejected.get("reason"))
        self.status = "%s: %d items in rex" % (time.strftime("%Y-%m-%d %H:%M"), len(items))
        LOG.info("pushed %d items: %s", len(items), {key: answer.get(key) for key in ("created", "updated", "removed")})

    def forever(self):
        while True:
            self.status = "syncing since %s" % time.strftime("%H:%M")
            try:
                self.once()
            except Exception as error:
                self.status = "failed: %s" % error
                LOG.warning("sync: %s", error)
            self.wake.wait(self.config["interval_minutes"] * 60)
            self.wake.clear()


def own_network(handler):
    """Whether a settings request comes from this machine, the home network or Tailscale, to a host
    named by its address (a name could be another site rebinding to us), and not from another
    page's form."""
    try:
        client = ipaddress.ip_address(handler.client_address[0])
        host = urllib.parse.urlsplit("//" + (handler.headers.get("Host") or "")).hostname or ""
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
    """An addon's link without what may be a key in it: its host and its last path part."""
    parts = urllib.parse.urlsplit(addon_base(url))
    return "%s/…/%s" % (parts.hostname, parts.path.rsplit("/", 1)[-1]) if parts.path.count("/") > 1 else parts.hostname


def page(config, status):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for at, addon in enumerate(config["addons"]):
        manifest = addon.get("manifest") or {}
        gives = [name for name in ("catalog", "stream") if any((entry if isinstance(entry, str) else entry.get("name")) == name
                                                              for entry in manifest.get("resources") or [])]
        rows.append('<tr><td>%s<br><small>%s</small></td><td>%s</td><td><form class="inline" method="post" action="/addons/remove">'
                    '<input type="hidden" name="at" value="%d"><button class="plain">Remove</button></form></td></tr>'
                    % (esc(manifest.get("name") or "addon"), esc(shown(addon["url"])), esc(", ".join(gives) or "nothing rex uses"), at))
    taken = {catalog["url"]: catalog for catalog in config["catalogs"]}
    catalogs, n = [], 0
    for addon in config["addons"]:
        for catalog in catalogs_of(addon.get("manifest") or {}):
            url = "%s/catalog/%s/%s.json" % (addon_base(addon["url"]), catalog["type"], catalog["id"])
            chosen = taken.get(url) or {}
            library = chosen.get("library") or ("Stremio Films" if catalog["type"] == "movie" else "Stremio Shows")
            addon_name = (addon.get("manifest") or {}).get("name") or "addon"
            name = "%s: %s (%s)" % (addon_name, catalog["name"],
                                    "films" if catalog["type"] == "movie" else "episodes")
            catalogs.append('<tr><td><input type="checkbox" id="c%d" name="take" value="%d"%s><input type="hidden" name="url%d" value="%s">'
                            '<input type="hidden" name="title%d" value="%s"></td><td><label for="c%d">%s</label></td>'
                            '<td><input type="text" name="library%d" value="%s" aria-label="Library"></td>'
                            '<td><input type="number" min="1" name="limit%d" value="%s" aria-label="How many" style="width:5em"></td></tr>'
                            % (n, n, " checked" if chosen else "", n, esc(url), n, esc(addon_name + " " + catalog["name"]), n, esc(name), n, esc(library),
                               n, esc(chosen.get("limit") or 50)))
            n += 1
    resolutions = "".join('<option value="%s"%s>%s</option>' % (value, " selected" if str(config.get("max_resolution") or "") == value else "", label)
                          for value, label in (("", "any"), ("2160", "2160p (4K)"), ("1080", "1080p"), ("720", "720p")))
    return PAGE % {"status": esc(status), "addons": "".join(rows) or '<tr><td colspan="3"><small>None yet.</small></td></tr>',
                   "catalogs": "".join(catalogs) or '<tr><td colspan="4"><small>Add an addon with catalogs, such as Cinemeta.</small></td></tr>',
                   "resolutions": resolutions, "max_size_gb": esc(config.get("max_size_gb") or ""), "rex_url": esc(config["rex_url"])}


class App:
    """The relay and the settings page, on one listener."""

    def __init__(self, config_path, config, sync):
        self.config_path, self.config, self.sync = config_path, config, sync
        self.saving = threading.Lock()

    def settings(self, form):
        """The settings form -> the config, saved, then a sync."""
        values = dict(self.config)
        catalogs = []
        for at in form.get("take", []):
            url = form.get("url" + at, [""])[0]
            limit = form.get("limit" + at, ["50"])[0]
            catalogs.append({"url": url, "title": form.get("title" + at, [""])[0] or None,
                             "library": form.get("library" + at, [""])[0].strip() or None,
                             "limit": int(limit) if limit.isdigit() and int(limit) > 0 else 50})
        values["catalogs"] = [{key: value for key, value in catalog.items() if value} for catalog in catalogs]
        values["max_resolution"] = int(form["max_resolution"][0]) if form.get("max_resolution", [""])[0].isdigit() else None
        size = form.get("max_size_gb", [""])[0]
        values["max_size_gb"] = float(size) if re.fullmatch(r"\d+(\.\d+)?", size) and float(size) > 0 else None
        if form.get("rex_url", [""])[0].startswith(("http://", "https://")):
            values["rex_url"] = form["rex_url"][0].rstrip("/")
        self.save(values)

    def add_addon(self, url):
        base = addon_base(url)
        if not base.startswith(("http://", "https://")):
            raise ValueError("not an addon link")
        manifest = get_json(base + "/manifest.json", 30)
        if not manifest.get("id") or not manifest.get("resources"):
            raise ValueError("no Stremio manifest at that link")
        addons = [addon for addon in self.config["addons"] if addon_base(addon["url"]) != base]
        self.save(dict(self.config, addons=addons + [{"url": base + "/manifest.json", "manifest": manifest}]), sync=False)

    def remove_addon(self, at):
        addons = list(self.config["addons"])
        gone = addons.pop(at)
        prefix = addon_base(gone["url"]) + "/catalog/"
        self.save(dict(self.config, addons=addons, catalogs=[c for c in self.config["catalogs"] if not c["url"].startswith(prefix)]))

    def save(self, values, sync=True):
        with self.saving:
            save_config(self.config_path, values)
            fresh = load_config(self.config_path)
            self.config.clear()
            self.config.update(fresh)
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
                    body = page(app.config, app.sync.status).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                match = re.fullmatch(r"/[\w-]+/[\w-]+/([\w-]+)\.\w+", path)
                if not match or not os.path.exists(app.sync.path("info", match[1])):
                    self.send_error(404)
                    return
                try:
                    url = app.sync.resolve(match[1])
                except Exception as error:
                    LOG.warning("resolve %s: %s", match[1], error)
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
                length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
                path = urllib.parse.urlsplit(self.path).path
                try:
                    if path == "/addons":
                        app.add_addon(form.get("url", [""])[0])
                    elif path == "/addons/remove":
                        app.remove_addon(int(form.get("at", ["-1"])[0]))
                    elif path == "/settings":
                        app.settings(form)
                    else:
                        self.send_error(404)
                        return
                except Exception as error:
                    LOG.warning("settings: %s", error)
                    self.send_error(400, str(error)[:200])
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
    LOG.info("relay and settings on http://%s/, %d catalog(s), rex at %s", config["listen"], len(config["catalogs"]), config["rex_url"])
    if args.once:
        sync.once()
        return 0
    sync.forever()


if __name__ == "__main__":
    sys.exit(main())
