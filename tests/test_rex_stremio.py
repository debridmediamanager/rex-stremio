#!/usr/bin/env python3
"""rex-stremio's own logic, its settings page, and one sync against a real rex.

The fixtures are captured: `stremio.json` is Stremio addons' real answers (DMM Cast for the RD
test account, Cinemeta, httpx, Torrentio over RD, Public Domain Movies, live TV's USA TV Next and PenguPlay; secrets redacted) and
`manifests.json` their manifests, all served by a fake addon host; the probes of their streams
were captured through a relay. `fake_ffprobe.py` answers from them. The end-to-end case needs a
rex binary: REX_BIN.

    REX_BIN=/path/to/rex python3 -m unittest discover -s tests
"""
import json
import math
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import rex_stremio  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
with open(os.path.join(FIXTURES, "stremio.json")) as _handle:
    STREMIO = json.load(_handle)["routes"]
with open(os.path.join(FIXTURES, "manifests.json")) as _handle:
    MANIFESTS = json.load(_handle)["routes"]
DMM = STREMIO["/dmm/stream/movie/tt30144839.json"]["streams"]
TORRENTIO = STREMIO["/torrentio/stream/series/tt11540908:1:2.json"]["streams"]
ABC = "ustv-1a0b178a-23c5-4c06-9217-ceabe2897343"
USATV = STREMIO["/usatv/stream/tv/%s.json" % ABC]["streams"]


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def stop_server(server):
    server.shutdown()
    server.server_close()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# The headers each /hdr/ link of the captured answers was served with: the fake host answers 429
# without them, as Hyper's and Eclipsia's hosts did (2026-10-24).
NEEDS = {}
for _route, _answer in STREMIO.items():
    for _stream in (_answer.get("streams") or []) if isinstance(_answer, dict) else []:
        if "/hdr/" in str(_stream.get("url")):
            NEEDS[_stream["url"].split("/hdr/", 1)[1]] = {
                name: value for name, value in (((_stream.get("behaviorHints") or {}).get("proxyHeaders") or {}).get("request") or {}).items()
                if name.lower() != "accept-encoding"}
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4097  # over 1 MB, as any video is


def hdr_body(name):
    """A /hdr/ link's bytes: an MP4, or an HLS master -> media playlist -> segments, every one of
    them named relative to its playlist."""
    if name.endswith(".mp4"):
        return MP4 + name.encode(), "video/mp4"
    if name.endswith("/v/index.m3u8"):
        return (b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
                b"#EXTINF:2,\nseg0.ts\n#EXTINF:2,\nseg1.ts\n#EXT-X-ENDLIST\n"), "application/vnd.apple.mpegurl"
    if name.endswith(".m3u8"):
        return b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n" + name.rsplit("/", 1)[-1][:-5].encode() + b"/v/index.m3u8\n", "application/x-mpegURL"
    return b"G" * 376, "video/mp2t"


def addon_host():
    """The captured addons on one local host, `/<addon>/...` as each answered."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urllib.parse.unquote(self.path)
            if path.startswith("/hdr/"):
                name = path[len("/hdr/"):]
                link = next((link for link in NEEDS if name == link or name.startswith(link[:link.rfind(".")] + "/")), None)
                need = NEEDS.get(link) or {}
                if link is None or any(self.headers.get(header) != value for header, value in need.items()):
                    self.send_error(429 if link else 404)
                    return
                raw, kind = hdr_body(name)
                first, last = 0, len(raw) - 1
                found = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range") or "")
                if found:
                    first, last = int(found[1]), min(int(found[2] or last), last)
                self.send_response(206 if found else 200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(last - first + 1))
                if found:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, len(raw)))
                self.end_headers()
                self.wfile.write(raw[first:last + 1])
                return
            if path.startswith("/tiny/"):  # an error body served as a file
                self.send_response(206)
                self.send_header("Content-Type", "video/x-matroska")
                self.send_header("Content-Range", "bytes 0-0/150")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"x")
                return
            if path.startswith("/whole/"):  # Google's video-downloads: no Range, all of the file
                self.send_response(200)
                self.send_header("Content-Type", "video/mkv")
                self.send_header("Content-Length", str(len(MP4)))
                self.end_headers()
                self.wfile.write(MP4)
                return
            if path.startswith("/akamai/"):  # a master whose variants answer only its own User-Agent
                agent = urllib.parse.quote(self.headers.get("User-Agent") or "", safe="")
                if path.endswith("master.m3u8"):
                    raw = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nlow.m3u8?ua=%s\n"
                           "#EXT-X-STREAM-INF:BANDWIDTH=3600000,RESOLUTION=1280x720\nhigh.m3u8?ua=%s\n" % (agent, agent)).encode()
                elif urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("ua", [""])[0] == urllib.parse.unquote(agent):
                    raw = b"#EXTM3U\n#EXTINF:4,\nseg0.ts\n#EXT-X-ENDLIST\n"
                else:
                    self.send_error(403)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-mpegURL")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path.startswith("/video/") and path.endswith(".m3u8"):  # usatv's ABC's master
                with open(os.path.join(FIXTURES, "hls-master-usatv-abc.m3u8"), "rb") as handle:
                    raw = handle.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-mpegURL")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path.startswith(("/page/", "/video/")):  # a web page, and a video file
                raw = b"<!doctype html><title>Comet</title>" if path.startswith("/page/") else b"\x1a\x45\xdf\xa3" + bytes(1 << 20)
                ranged = path.startswith("/video/") and self.headers.get("Range") == "bytes=0-0"
                self.send_response(206 if ranged else 200)
                self.send_header("Content-Type", "text/html; charset=utf-8" if path.startswith("/page/") else "video/x-matroska")
                if ranged:
                    raw, _ = raw[:1], self.send_header("Content-Range", "bytes 0-0/%d" % len(raw))
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            body = STREMIO.get(path) or MANIFESTS.get(path)
            if body is None:
                self.send_error(404)
                return
            raw = json.dumps(body).replace("{STREAMS}", base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = "http://127.0.0.1:%d" % server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, base


def addon(base, name):
    return {"url": "%s/%s/manifest.json" % (base, name), "manifest": MANIFESTS["/%s/manifest.json" % name]}


def config_file(test, values):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(values, handle)
    handle.close()
    test.addCleanup(os.unlink, handle.name)
    return handle.name


class FakeRequest:
    def __init__(self, client, host, command="GET", origin=None):
        self.client_address = (client, 50000)
        self.headers = {"Host": host, **({"Origin": origin} if origin else {})}
        self.command = command


class Logic(unittest.TestCase):
    def test_addon_base(self):
        self.assertEqual(rex_stremio.addon_base("stremio://torrentio.strem.fun/realdebrid=K/manifest.json"),
                         "https://torrentio.strem.fun/realdebrid=K")
        self.assertEqual(rex_stremio.addon_base(" https://v3-cinemeta.strem.io/manifest.json "), "https://v3-cinemeta.strem.io")

    def test_sensitive_urls_are_redacted(self):
        self.assertEqual(rex_stremio.safe_url("https://user:secret@example.test/private/key?token=secret"),
                         "https://example.test")
        self.assertNotIn("secret", rex_stremio.safe_error(ValueError("failed https://user:secret@example.test/key?token=secret")))
        rendered = rex_stremio.page({**rex_stremio.DEFAULTS,
                                     "rex_url": "https://user:secret@example.test/rex/path?token=secret"},
                                    "failed https://user:secret@example.test/error")
        self.assertIn("https://user:secret@example.test/rex/path?token=secret", rendered)
        self.assertNotIn("error", rendered)

    def test_rex_endpoint_keeps_path_and_query(self):
        self.assertEqual(rex_stremio.rex_endpoint("https://rex.example/rex/path?token=secret", "/rex/sources/stremio"),
                         "https://rex.example/rex/path/rex/sources/stremio?token=secret")

    def test_serves_by_manifest(self):
        cinemeta, torrentio, dmm, httpx = (MANIFESTS["/%s/manifest.json" % name] for name in ("cinemeta", "torrentio", "dmm", "httpx"))
        self.assertTrue(rex_stremio.serves(torrentio, "stream", "series", "tt11540908:1:2"))
        self.assertTrue(rex_stremio.serves(dmm, "stream", "movie", "tt30144839"))
        self.assertTrue(rex_stremio.serves(httpx, "stream", "movie", "tt6262770"))  # types and prefixes at the top
        self.assertFalse(rex_stremio.serves(cinemeta, "stream", "movie", "tt6262770"))
        self.assertFalse(rex_stremio.serves(dmm, "stream", "other", "dmm:1"))
        self.assertFalse(rex_stremio.serves({"resources": 3, "types": "movie"}, "stream", "movie", "tt1"))
        self.assertFalse(rex_stremio.serves({"resources": [{"name": "stream", "types": 3, "idPrefixes": 4}]},
                                             "stream", "movie", "tt1"))
        self.assertEqual(rex_stremio.page({**rex_stremio.DEFAULTS, "addons": [{"url": "https://invalid/manifest.json", "manifest": []}]}, "ok").count("invalid"), 1)

    def test_catalogs_listed_without_a_question(self):
        cinemeta = rex_stremio.catalogs_of(MANIFESTS["/cinemeta/manifest.json"])
        # "New" needs a genre, "Last videos" and "Calendar videos" a list of ids.
        self.assertEqual([(c["type"], c["id"]) for c in cinemeta],
                         [("movie", "top"), ("series", "top"), ("movie", "imdbRating"), ("series", "imdbRating")])
        dmm = rex_stremio.catalogs_of(MANIFESTS["/dmm/manifest.json"])
        self.assertEqual([(c["type"], c["id"]) for c in dmm], [("movie", "casted-movies"), ("series", "casted-shows")])
        self.assertEqual(rex_stremio.catalogs_of(MANIFESTS["/torrentio/manifest.json"]), [])
        # A live TV addon's channels (its genre is optional).
        self.assertEqual(rex_stremio.catalogs_of(MANIFESTS["/usatv/manifest.json"]), [{"type": "tv", "id": "all", "name": "USA TV Next"}])
        self.assertEqual(rex_stremio.catalogs_of({"catalogs": 4}), [])
        self.assertEqual(rex_stremio.catalogs_of({"catalogs": [{"type": "movie", "id": 7, "extra": 3}, 4]}), [])

    def test_quality_from_labels(self):
        self.assertEqual(rex_stremio.quality(TORRENTIO[0]), (2160, int(5.49 * (1 << 30))))
        self.assertEqual(rex_stremio.quality(DMM[2]), (2160, int(83.52 * (1 << 30))))
        self.assertEqual(rex_stremio.quality(DMM[1]), (0, int(83.77 * (1 << 30))))
        pdm = STREMIO["/pdm/stream/movie/tt0012349.json"]["streams"][0]
        self.assertEqual(rex_stremio.quality(pdm), (1080, int(859.37 * (1 << 20))))
        self.assertEqual(rex_stremio.quality({"name": "9" * 400 + " GB", "behaviorHints": {}}), (0, None))
        # Live channels name their streams HD and SD; HD ranks above SD.
        self.assertEqual([rex_stremio.quality(stream)[0] for stream in USATV], [720, 480, 0])
        self.assertEqual(rex_stremio.ranked(USATV, {})[0], USATV[0])

    def test_ranked(self):
        # A link out to DMM's site is no file; the labelled 4K streams beat the unlabelled one.
        best = rex_stremio.ranked(DMM, {})
        self.assertEqual([s["url"][-5:] for s in best], [s["url"][-5:] for s in (DMM[2], DMM[4], DMM[1], DMM[3], DMM[5])])
        # 1080p at most: only what names no resolution is left (it may be anything; it is not excluded).
        self.assertEqual(rex_stremio.ranked(DMM, {"max_resolution": 1080}), [DMM[1], DMM[3], DMM[5]])
        self.assertEqual(rex_stremio.ranked(DMM, {"max_size_gb": 80})[0], DMM[4])
        # The stream an item was probed from plays first, whatever ranks above it.
        self.assertEqual(rex_stremio.ranked(TORRENTIO, {}, rex_stremio.stream_key(TORRENTIO[2]))[0], TORRENTIO[2])
        self.assertEqual(rex_stremio.ranked(STREMIO["/pdm/stream/movie/tt0012349.json"]["streams"], {}), [])

    def test_load_config(self):
        config = rex_stremio.load_config(config_file(self, {"catalogs": [
            {"url": "https://a/catalog/movie/top.json", "title": "Popular"},
            {"url": "https://b/catalog/movie/top.json", "title": "Popular", "library": "Films"},
            {"url": "https://a/catalog/series/top.json"}]}))
        films, others, shows = config["catalogs"]
        self.assertEqual((films["library"], films["directory"], films["slug"], films["limit"]), ("Stremio Films", "stremio-films", "popular", 50))
        self.assertEqual((others["slug"], others["directory"]), ("popular-2", "films"))
        self.assertEqual((shows["type"], shows["library"], shows["slug"]), ("series", "Stremio Shows", "top"))
        self.assertEqual(config["relay_url"], "http://127.0.0.1:9982")
        with self.assertRaises(ValueError):
            rex_stremio.load_config(config_file(self, {"catalogs": [{"url": "https://a/meta/movie/tt1.json"}]}))
        with self.assertRaises(ValueError):
            rex_stremio.load_config(config_file(self, {"listen": "not-a-host-port"}))
        with self.assertRaises(ValueError):
            rex_stremio.load_config(config_file(self, {"rex_url": "http://127.0.0.1:not-a-port"}))
        with self.assertRaises(ValueError):
            rex_stremio.load_config(config_file(self, {"rex_url": None}))

    def test_unassigned_duplicate_identity_keeps_historical_suffix_sequence(self):
        path = config_file(self, {"catalogs": [
            {"url": "https://a/catalog/movie/one.json", "title": "Popular", "library": "Films"},
            {"url": "https://a/catalog/movie/two.json", "title": "Popular", "library": "Films"},
            {"url": "https://a/catalog/movie/three.json", "title": "Popular", "library": "Films"}]})
        before = rex_stremio.load_config(path)
        expected = ["popular", "popular-2", "popular-2-2"]
        self.assertEqual([catalog["slug"] for catalog in before["catalogs"]], expected)
        rex_stremio.save_config(path, before)
        after = rex_stremio.load_config(path)
        self.assertEqual([catalog["slug"] for catalog in after["catalogs"]], expected)

    def test_write_json_replace_failure_does_not_close_reused_descriptor(self):
        work = tempfile.mkdtemp(prefix="rex-stremio-write-")
        target = os.path.join(work, "target.json")
        other = os.path.join(work, "other")
        handles = {}
        original_fdopen = rex_stremio.os.fdopen

        def tracked_fdopen(descriptor, *args, **kwargs):
            handles["descriptor"] = descriptor
            return original_fdopen(descriptor, *args, **kwargs)

        def failed_replace(source, destination):
            descriptor = os.open(other, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            handles["reused"] = descriptor
            self.assertEqual(descriptor, handles["descriptor"])
            raise OSError("fixture replace failure")

        with unittest.mock.patch.object(rex_stremio.os, "fdopen", side_effect=tracked_fdopen), \
             unittest.mock.patch.object(rex_stremio.os, "replace", side_effect=failed_replace):
            with self.assertRaises(OSError):
                rex_stremio.write_json(target, {"written": True})
        os.write(handles["reused"], b"still open")
        os.close(handles["reused"])
        self.assertFalse(os.path.exists(target))

    def test_write_json_works_without_fchmod_as_on_windows(self):
        target = os.path.join(tempfile.mkdtemp(prefix="rex-stremio-write-"), "config.json")
        with unittest.mock.patch.object(rex_stremio.os, "fchmod", create=True) as fchmod:
            del rex_stremio.os.fchmod
            rex_stremio.write_json(target, {"written": True})
        self.assertFalse(fchmod.called)
        self.assertEqual(read_json(target), {"written": True})

    def test_explicit_slug_is_reserved_and_conflicts_are_rejected(self):
        config = rex_stremio.load_config(config_file(self, {"catalogs": [
            {"url": "https://a/catalog/movie/top.json", "title": "Popular"},
            {"url": "https://b/catalog/movie/other.json", "slug": "popular"}]}))
        self.assertEqual([catalog["slug"] for catalog in config["catalogs"]], ["popular-2", "popular"])
        with self.assertRaises(ValueError):
            rex_stremio.load_config(config_file(self, {"catalogs": [
                {"url": "https://a/catalog/movie/one.json", "slug": "same"},
                {"url": "https://b/catalog/movie/two.json", "slug": "same"}]}))
        with self.assertRaises(ValueError):
            rex_stremio.load_config(config_file(self, {"catalogs": [
                {"url": "https://a/catalog/movie/one.json", "directory": "films", "library": "One"},
                {"url": "https://b/catalog/movie/two.json", "directory": "films", "library": "Two"}]}))

    def test_save_config_private_and_with_stable_catalog_identity(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/top.json", "library": "Films"}]})
        rex_stremio.save_config(path, rex_stremio.load_config(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        saved = read_json(path)
        self.assertEqual(saved["catalogs"], [{"url": "https://a/catalog/movie/top.json", "library": "Films", "limit": 50,
                                               "slug": "top", "directory": "films"}])
        self.assertNotIn("relay_url", saved)

    def test_explicit_catalog_identity_survives_save(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/top.json", "title": "Popular",
                                                "slug": "stable-popular", "directory": "stable-films"}]})
        config = rex_stremio.load_config(path)
        rex_stremio.save_config(path, config)
        loaded = rex_stremio.load_config(path)["catalogs"][0]
        self.assertEqual((loaded["slug"], loaded["directory"]), ("stable-popular", "stable-films"))

    def test_first_save_persists_identity_across_reorder_remove_and_add(self):
        path = config_file(self, {"catalogs": []})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        values = dict(config, catalogs=[
            {"url": "https://a/catalog/movie/one.json", "title": "Popular", "library": "Films"},
            {"url": "https://a/catalog/movie/two.json", "title": "Popular", "library": "Films"},
            {"url": "https://a/catalog/movie/three.json", "title": "Popular", "library": "Films"}])
        app.save(values, sync=False)
        first = {catalog["url"]: (catalog["slug"], catalog["directory"]) for catalog in rex_stremio.load_config(path)["catalogs"]}
        self.assertEqual(sorted(slug for slug, _ in first.values()), ["popular", "popular-2", "popular-2-2"])
        restarted = rex_stremio.load_config(path)
        app.save(dict(restarted, catalogs=list(reversed(restarted["catalogs"]))), sync=False)
        reordered = {catalog["url"]: catalog["slug"] for catalog in rex_stremio.load_config(path)["catalogs"]}
        self.assertEqual(reordered, {url: slug for url, (slug, _) in first.items()})
        retained = [catalog for catalog in rex_stremio.load_config(path)["catalogs"] if not catalog["url"].endswith("two.json")]
        retained.append({"url": "https://a/catalog/movie/four.json", "title": "Popular", "library": "Films"})
        app.save(dict(restarted, catalogs=retained), sync=False)
        final = {catalog["url"]: catalog["slug"] for catalog in rex_stremio.load_config(path)["catalogs"]}
        self.assertEqual(final["https://a/catalog/movie/one.json"], first["https://a/catalog/movie/one.json"][0])
        self.assertEqual(final["https://a/catalog/movie/three.json"], first["https://a/catalog/movie/three.json"][0])
        self.assertEqual(final["https://a/catalog/movie/four.json"], "popular-2")

    def test_cache_info_updates_do_not_drop_each_other(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-cache-")})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        info_path = sync.path("info", "item")
        sync.cache_write(info_path, {"kind": "movie", "sid": "tt1"})
        barrier = threading.Barrier(2)

        def update(fields):
            barrier.wait()
            sync.cache_update(info_path, fields)

        first = threading.Thread(target=update, args=({"stream": "file.mkv"},))
        second = threading.Thread(target=update, args=({"extension": "mkv"},))
        first.start()
        second.start()
        first.join(5)
        second.join(5)
        self.assertEqual(read_json(info_path), {"kind": "movie", "sid": "tt1", "stream": "file.mkv", "extension": "mkv"})

    def test_ranked_ignores_malformed_stream_rows(self):
        self.assertEqual(rex_stremio.ranked([None, {"url": None, "behaviorHints": []}, DMM[2]], {}), [DMM[2]])
        self.assertEqual(rex_stremio.ranked(3, {}), [])
        self.assertEqual(rex_stremio.quality({"url": DMM[2]["url"], "behaviorHints": {"videoSize": "nan"}}), (0, None))

    def test_own_network(self):
        own = rex_stremio.own_network
        self.assertTrue(own(FakeRequest("127.0.0.1", "127.0.0.1:9982")))
        self.assertTrue(own(FakeRequest("192.168.1.30", "192.168.1.20:9982")))
        self.assertTrue(own(FakeRequest("100.101.1.2", "localhost:9982")))  # Tailscale
        self.assertFalse(own(FakeRequest("8.8.8.8", "192.168.1.20:9982")))
        self.assertFalse(own(FakeRequest("127.0.0.1", "evil.example:9982")))  # a name rebound to us
        self.assertFalse(own(FakeRequest("127.0.0.1", "trusted@127.0.0.1:9982")))  # Host userinfo is not an IP host
        self.assertFalse(own(FakeRequest("127.0.0.1", "127.0.0.1:not-a-port")))
        self.assertFalse(own(FakeRequest("127.0.0.1", "127.0.0.1:9982", "POST", "https://evil.example")))
        self.assertTrue(own(FakeRequest("127.0.0.1", "127.0.0.1:9982", "POST", "http://127.0.0.1:9982")))

    def test_live_channel_item(self):
        config = rex_stremio.load_config(config_file(self, {"catalogs": [{"url": "https://h/catalog/tv/all.json", "title": "USA TV"}]}))
        channels = config["catalogs"][0]
        self.assertEqual((channels["type"], channels["library"], channels["directory"]), ("tv", "Live TV", "live-tv"))
        meta = STREMIO["/usatv/catalog/tv/all.json"]["metas"][0]
        probe = read_json(os.path.join(FIXTURES, "probe-usa-tv-%s.json" % ABC))
        pushed = rex_stremio.stremio_item(config, channels, "usa-tv-" + ABC, ABC, meta, None, probe, "m3u8")
        # Nothing names a channel but the addon: no kind, no ids, its name and logo as display.
        self.assertNotIn("kind", pushed)
        self.assertNotIn("ids", pushed)
        self.assertNotIn("size", pushed)
        self.assertEqual(pushed["path"], "live-tv/usa-tv/usa-tv-%s.m3u8" % ABC)
        self.assertEqual(pushed["display"], {"title": "ABC", "thumb": meta["poster"]})

    def test_channels_keep_the_addons_own_ids(self):
        self.assertEqual(len(rex_stremio.catalogs_of(MANIFESTS["/penguplay/manifest.json"])), 16)
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        config = rex_stremio.load_config(config_file(self, {"cache": work, "addons": [addon(base, "penguplay")], "catalogs": [
            {"url": base + "/penguplay/catalog/tv/pp-live-channels.json", "title": "Channels"}]}))
        sync = rex_stremio.Sync(config)
        probe = read_json(os.path.join(FIXTURES, "probe-usa-tv-%s.json" % ABC))
        with unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda *args: probe):
            items = sync.catalog_items(config["catalogs"][0])
        # PenguPlay's ids hold ':' and '~': the path takes them as '-', the stream is asked by the id.
        metas = STREMIO["/penguplay/catalog/tv/pp-live-channels.json"]["metas"]
        vid = "channels-pp-live-sktech-" + metas[0]["id"].split("~")[1]
        self.assertEqual([item["path"] for item in items][0], "live-tv/channels/%s.m3u8" % vid)
        self.assertEqual(read_json(sync.path("info", vid))["sid"], metas[0]["id"])
        self.assertEqual(sync.route_vid("/live-tv/channels/%s.m3u8" % vid), vid)
        self.assertEqual([item["display"]["title"] for item in items], [meta["name"] for meta in metas])

    def test_probe_reads_hls_leniently_where_ffprobe_can(self):
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        seen = os.path.join(work, "argv")
        fake = os.path.join(work, "ffprobe")
        for ffprobe, help_text, lenient in ((fake, "-extension_picky <boolean>", True), (fake + "-old", "-allowed_extensions", False)):
            with open(ffprobe, "w") as handle:
                handle.write("#!/bin/sh\nif [ \"$2\" = -h ]; then echo '%s'; exit 0; fi\necho \"$@\" > %s\necho '{\"format\": {}}'\n" % (help_text, seen))
            os.chmod(ffprobe, 0o755)
            config = rex_stremio.load_config(config_file(self, {"cache": work, "ffprobe": ffprobe, "catalogs": [
                {"url": "https://h/catalog/tv/all.json", "title": "USA TV"}]}))
            rex_stremio.Sync(config).probe("usa-tv-" + ABC, config["catalogs"][0])
            with open(seen) as handle:
                self.assertEqual("-extension_picky 0" in handle.read(), lenient)

    def test_a_web_page_is_no_stream(self):
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        comet = json.loads(json.dumps(STREMIO["/comet/stream/movie/tt0111161.json"]).replace("{STREAMS}", base))["streams"]
        # Ranked as a link, it opens with 200, but what it opens is Comet's web page.
        self.assertEqual(rex_stremio.ranked(comet, {}), comet)
        self.assertIsNone(rex_stremio.opens(comet[0]["url"]))
        self.assertEqual(rex_stremio.opens(base + "/video/a.mkv"), base + "/video/a.mkv")
        # A file that answers a Range with all of itself (Google's video-downloads) cannot be seeked.
        self.assertIsNone(rex_stremio.opens(base + "/whole/breaking-bad-s01e01.mkv"))
        self.assertIsNone(rex_stremio.opens(base + "/tiny/shawshank.mkv"))  # 150 bytes, as Hydra's was

    def test_tmdb_catalog_items_are_named_by_their_imdb_ids(self):
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        tmdb = rex_stremio.TMDB_META
        rex_stremio.TMDB_META = base + "/tmdb"
        self.addCleanup(setattr, rex_stremio, "TMDB_META", tmdb)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        config = rex_stremio.load_config(config_file(self, {"cache": work, "addons": [addon(base, "cartoon")], "catalogs": [
            {"url": base + "/cartoon/catalog/movie/cartoon_network_movie.json", "title": "Cartoon Network"}]}))
        sync = rex_stremio.Sync(config)
        probe = read_json(os.path.join(FIXTURES, "probe-top-films-tt6262770.json"))
        with unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda *args: probe):
            items = sync.catalog_items(config["catalogs"][0])
        # The addon's own meta names no IMDb id; the TMDB addon's does.
        self.assertEqual([(item["id"], item["ids"]) for item in items],
                         [("st-cartoon-network-tt10515852", {"imdb": "tt10515852"}), ("st-cartoon-network-tt0289408", {"imdb": "tt0289408"})])
        self.assertEqual(read_json(sync.path("info", "cartoon-network-tt10515852"))["sid"], "tt10515852")
        self.assertEqual(read_json(sync.path("ids", "movie-tmdb-537061")), {"imdb": "tt10515852"})

    def test_catalogs_of_an_addons_own_ids(self):
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        config = rex_stremio.load_config(config_file(self, {"cache": work, "addons": [addon(base, "onepace"), addon(base, "cotonet")], "catalogs": [
            {"url": base + "/cotonet/catalog/movie/cotonet_recentes.json", "title": "Cotonet", "limit": 1},
            {"url": base + "/onepace/catalog/series/one-pace-complete.json", "title": "One Pace", "limit": 2}]}))
        sync = rex_stremio.Sync(config)
        probe = read_json(os.path.join(FIXTURES, "probe-top-films-tt6262770.json"))
        with unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda *args: probe):
            films = sync.catalog_items(config["catalogs"][0])
            episodes = sync.catalog_items(config["catalogs"][1])
        # Cotonet's id carries an IMDb id: Plex names the film, Cotonet is asked by its own id.
        self.assertEqual([(item["id"], item["ids"]) for item in films], [("st-cotonet-tt28942997", {"imdb": "tt28942997"})])
        self.assertEqual(read_json(sync.path("info", "cotonet-tt28942997"))["also"], ["cotonet:tt28942997"])
        with unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url):
            self.assertEqual(sync.resolve("cotonet-tt28942997"), base + "/video/cotonet-0.mkv")
        # One Pace is known to its addon alone: its episodes, numbered by its meta, named by it.
        self.assertEqual([(item["kind"], item["season"], item["episode"], item.get("ids"), item["release"]) for item in episodes],
                         [("episode", 1, 1, None, "stremio:onepace_complete_series"), ("episode", 1, 2, None, "stremio:onepace_complete_series")])
        self.assertEqual(episodes[0]["display"]["show"]["title"], "One Pace")
        self.assertEqual(episodes[0]["display"]["title"], "Romance Dawn - Episodio 1")
        self.assertEqual(sync.route_vid("/stremio-shows/one-pace/one-pace-onepace_complete_series-1-1.m3u8"), "one-pace-onepace_complete_series-1-1")
        with unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url):
            self.assertEqual(sync.resolve("one-pace-onepace_complete_series-1-1"), base + "/video/onepace-0.mkv")

    def test_an_hls_master_is_its_best_variant(self):
        with open(os.path.join(FIXTURES, "hls-master-usatv-abc.m3u8")) as handle:
            master = handle.read()  # usatv's ABC: five variants, 360p to two of 720p
        base = "https://content.uplynk.com/channel/39919d3f7a074eefa8bf579214e952f9.m3u8"
        one = rex_stremio.one_variant(master, base)
        variants = [line for line in one.splitlines() if line.startswith("#EXT-X-STREAM-INF")]
        self.assertEqual(len(variants), 1)
        self.assertIn("RESOLUTION=1280x720,BANDWIDTH=6617500", variants[0])  # the better of the two 720p
        self.assertTrue(one.rstrip().endswith("/e.m3u8?pbs=PBS"))
        # The renditions it names stay; the I-frame playlist goes.
        self.assertIn('GROUP-ID="aac"', one)
        self.assertIn('GROUP-ID="ccs"', one)
        self.assertNotIn("I-FRAME", one)
        self.assertIn("RESOLUTION=854x480", rex_stremio.one_variant(master, base, 480))
        self.assertIsNone(rex_stremio.one_variant("#EXTM3U\n#EXTINF:4,\nseg0.ts\n", base))
        # Pluto TV's: five variants named by bandwidth alone, two audio renditions and subtitles.
        with open(os.path.join(FIXTURES, "hls-master-pluto.m3u8")) as handle:
            pluto = rex_stremio.one_variant(handle.read(), "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv/v2/stitch/hls/channel/x/master.m3u8")
        self.assertEqual([line for line in pluto.splitlines() if line.startswith("#EXT-X-STREAM-INF")],
                         ['#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3321280,AUDIO="audio"'])
        self.assertEqual(len([line for line in pluto.splitlines() if "TYPE=AUDIO" in line and "DEFAULT=YES" in line]), 1)
        self.assertNotIn("TYPE=AUDIO,GROUP-ID=\"audio\",LANGUAGE=\"en\",NAME=\"English\",", pluto)
        self.assertNotIn("SUBTITLES", pluto)

    def test_stremio_item(self):
        config = rex_stremio.load_config(config_file(self, {"catalogs": [
            {"url": "https://h/catalog/series/top.json", "title": "Top shows"},
            {"url": "https://h/catalog/movie/casted-movies.json", "title": "DMM Cast"}]}))
        shows, films = config["catalogs"]
        meta = STREMIO["/cinemeta/meta/series/tt11540908.json"]["meta"]
        video = [v for v in meta["videos"] if (v.get("season"), v.get("episode")) == (1, 2)][0]
        probe = read_json(os.path.join(FIXTURES, "probe-top-shows-tt11540908-1-2.json"))
        pushed = rex_stremio.stremio_item(config, shows, "top-shows-tt11540908-1-2", "tt11540908", meta, video, probe, "mkv")
        self.assertEqual((pushed["kind"], pushed["season"], pushed["episode"], pushed["ids"]), ("episode", 1, 2, {"imdb": "tt11540908"}))
        self.assertEqual(pushed["path"], "stremio-shows/top-shows/top-shows-tt11540908-1-2.mkv")
        self.assertEqual(pushed["size"], int(probe["format"]["size"]))
        self.assertEqual(pushed["display"]["show"]["title"], meta["name"])
        catalog = STREMIO["/dmm/catalog/movie/casted-movies.json"]["metas"][0]
        pushed = rex_stremio.stremio_item(config, films, "dmm-cast-tt30144839", "tt30144839", catalog, None,
                                          read_json(os.path.join(FIXTURES, "probe-dmm-cast-tt30144839.json")), "ts")
        self.assertEqual((pushed["kind"], pushed["ids"], pushed["path"][-3:]), ("movie", {"imdb": "tt30144839"}, ".ts"))
        self.assertEqual(pushed["display"]["title"], "One Battle After Another")


class Resolve(unittest.TestCase):
    """The relay's choice at play: ranked, pinned to what was probed, the next when a link is dead."""

    def setUp(self):
        self.host, self.base = addon_host()
        self.addCleanup(stop_server, self.host)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        self.config = rex_stremio.load_config(config_file(self, {"cache": work, "addons": [addon(self.base, "torrentio")]}))
        self.sync = rex_stremio.Sync(self.config)
        with open(self.sync.path("info", "carrie-1-2"), "w") as handle:
            json.dump({"kind": "series", "sid": "tt11540908:1:2"}, handle)
        self.opened = []

    def opens(self, dead):
        def opens(url, headers=None):
            self.opened.append(url)
            return None if url in dead else url + "#final"
        return opens

    def test_first_that_opens_is_pinned_then_kept(self):
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({TORRENTIO[0]["url"]})):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[1]["url"] + "#final")
        info = read_json(self.sync.path("info", "carrie-1-2"))
        self.assertEqual(info["stream"], rex_stremio.stream_key(TORRENTIO[1]))
        # An hour later the best link opens again, but the pinned one still plays first.
        self.sync.cache.clear()
        self.opened.clear()
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens(set())):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[1]["url"] + "#final")
        self.assertEqual(self.opened, [TORRENTIO[1]["url"]])

    def test_pinned_gone_falls_back(self):
        with open(self.sync.path(rex_stremio.PROBES, "carrie-1-2"), "w") as handle:
            json.dump({"format": {}}, handle)
        with open(self.sync.path("info", "carrie-1-2"), "w") as handle:
            json.dump({"kind": "series", "sid": "tt11540908:1:2", "stream": rex_stremio.stream_key(TORRENTIO[2])}, handle)
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({TORRENTIO[2]["url"]})):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[0]["url"] + "#final")
        self.assertEqual(self.opened, [TORRENTIO[2]["url"], TORRENTIO[0]["url"]])
        # What plays now is pinned and probed again on a sync started at once, so rex learns it.
        self.assertEqual(read_json(self.sync.path("info", "carrie-1-2"))["stream"], rex_stremio.stream_key(TORRENTIO[0]))
        self.assertFalse(os.path.exists(self.sync.path(rex_stremio.PROBES, "carrie-1-2")))
        self.assertTrue(self.sync.wake.is_set())

    def test_nothing_opens(self):
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({s["url"] for s in TORRENTIO})):
            with self.assertRaises(RuntimeError):
                self.sync.resolve("carrie-1-2")

    def test_cached_link_is_rechecked_before_reuse(self):
        self.sync.cache["carrie-1-2"] = ("http://cached.invalid/file", time.time() + 3600)
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({"http://cached.invalid/file"})):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[0]["url"] + "#final")
        self.assertEqual(self.opened[0], "http://cached.invalid/file")

    def test_relay_requires_configured_directory_and_catalog(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-route-"),
                                  "addons": [addon(self.base, "torrentio")],
                                  "catalogs": [{"url": "%s/catalog/series/top.json" % self.base, "title": "Carrie"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        with open(sync.path("info", "carrie-tt11540908-1-2"), "w") as handle:
            json.dump({"kind": "series", "sid": "tt11540908:1:2", "extension": "mkv"}, handle)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_stremio.App(path, config, sync).handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        with unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url):
            with self.assertRaises(urllib.error.HTTPError) as answer:
                urllib.request.urlopen("http://127.0.0.1:%d/wrong/carrie/carrie-1-2.mkv" % server.server_address[1])
        self.assertEqual(answer.exception.code, 404)
        opener = urllib.request.build_opener(NoRedirect)
        with unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url):
            with self.assertRaises(urllib.error.HTTPError) as answer:
                opener.open("http://127.0.0.1:%d/stremio-shows/carrie/carrie-tt11540908-1-2.mkv" % server.server_address[1])
        self.assertEqual(answer.exception.code, 302)

    def test_relay_does_not_confuse_overlapping_catalog_slugs(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-route-"),
                                  "catalogs": [
                                      {"url": "%s/catalog/movie/one.json" % self.base, "slug": "cat", "directory": "films"},
                                      {"url": "%s/catalog/movie/two.json" % self.base, "slug": "cat-2", "directory": "films"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        with open(sync.path("info", "cat-2-tt1234567"), "w") as handle:
            json.dump({"kind": "movie", "sid": "tt1234567", "extension": "mkv"}, handle)
        self.assertIsNone(sync.route_vid("/films/cat/cat-2-tt1234567.mkv"))
        self.assertEqual(sync.route_vid("/films/cat-2/cat-2-tt1234567.mkv"), "cat-2-tt1234567")


class Settings(unittest.TestCase):
    """The page, through HTTP: add addons, choose catalogs, rank streams."""

    def setUp(self):
        self.host, self.base = addon_host()
        self.addCleanup(stop_server, self.host)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        self.path = os.path.join(work, "config.json")
        rex_stremio.save_config(self.path, {"listen": "127.0.0.1:0", "cache": work})
        self.config = rex_stremio.load_config(self.path)
        self.sync = rex_stremio.Sync(self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_stremio.App(self.path, self.config, self.sync).handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        self.url = "http://127.0.0.1:%d" % server.server_address[1]

    def post(self, path, form, origin=None):
        data = urllib.parse.urlencode(form, doseq=True).encode()
        request = urllib.request.Request(self.url + path, data=data, headers={"Origin": origin or self.url})
        with urllib.request.urlopen(request, timeout=30) as response:  # follows the 303 to the page
            return response.read().decode()

    def test_add_choose_and_save(self):
        self.post("/addons", {"url": "%s/cinemeta/manifest.json" % self.base})
        page = self.post("/addons", {"url": "%s/torrentio" % self.base})  # with or without /manifest.json
        self.assertIn("Cinemeta: Popular (films)", page)
        self.assertIn("Torrentio", page)
        self.assertNotIn("Torrentio: ", page)  # no catalogs of its own
        saved = read_json(self.path)
        self.assertEqual([a["manifest"]["name"] for a in saved["addons"]], ["Cinemeta", "Torrentio"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # Rows 0 and 1 are Cinemeta's Popular films and series.
        self.post("/settings", {"take": ["0", "1"], "url0": "%s/cinemeta/catalog/movie/top.json" % self.base, "title0": "Cinemeta Popular",
                                "library0": "Films", "limit0": "10", "url1": "%s/cinemeta/catalog/series/top.json" % self.base,
                                "title1": "Cinemeta Popular", "library1": "", "limit1": "x", "max_resolution": "1080", "max_size_gb": "20",
                                "rex_url": "http://192.168.1.20:32400/"})
        saved = read_json(self.path)
        self.assertEqual(saved["catalogs"], [
            {"url": "%s/cinemeta/catalog/movie/top.json" % self.base, "title": "Cinemeta Popular", "library": "Films", "limit": 10,
             "slug": "cinemeta-popular", "directory": "films"},
            {"url": "%s/cinemeta/catalog/series/top.json" % self.base, "title": "Cinemeta Popular", "limit": 50,
             "library": "Stremio Shows", "slug": "cinemeta-popular-2", "directory": "stremio-shows"}])
        self.assertEqual((saved["max_resolution"], saved["max_size_gb"], saved["rex_url"]), (1080, 20.0, "http://192.168.1.20:32400/"))
        self.assertEqual([c["slug"] for c in self.config["catalogs"]], ["cinemeta-popular", "cinemeta-popular-2"])
        self.assertTrue(self.sync.wake.is_set())
        page = self.post("/addons/remove", {"at": "0"})
        self.assertNotIn("Cinemeta: Popular", page)
        self.assertEqual(read_json(self.path)["catalogs"], [])  # its catalogs leave with it

    def test_rex_url_round_trips_path_and_query(self):
        configured = "http://rex.example/rex/path/?token=private&mode=lan"
        rendered = self.post("/settings", {"rex_url": configured})
        self.assertEqual(read_json(self.path)["rex_url"], configured)
        self.assertIn('name="rex_url" value="http://rex.example/rex/path/?token=private&amp;mode=lan"', rendered)

    def test_library_edit_rederives_directory_but_preserves_manual_override(self):
        url = "%s/cinemeta/catalog/movie/top.json" % self.base
        self.post("/addons", {"url": "%s/cinemeta/manifest.json" % self.base})
        self.post("/settings", {"take": ["0"], "url0": url, "title0": "Popular", "library0": "Films", "limit0": "10",
                                 "rex_url": "http://127.0.0.1:32400"})
        chosen = self.config["catalogs"][0]
        self.post("/settings", {"take": ["0"], "url0": url, "title0": "Popular", "slug0": chosen["slug"],
                                 "directory0": chosen["directory"], "library0": "Renamed Films", "limit0": "10",
                                 "rex_url": "http://127.0.0.1:32400"})
        edited = read_json(self.path)["catalogs"][0]
        self.assertEqual((edited["slug"], edited["directory"], edited["library"]), (chosen["slug"], "renamed-films", "Renamed Films"))
        self.post("/settings", {"take": ["0"], "url0": url, "title0": "Popular", "slug0": edited["slug"],
                                 "directory0": "keep-me", "library0": "Manual Films", "limit0": "10",
                                 "rex_url": "http://127.0.0.1:32400"})
        self.post("/settings", {"take": ["0"], "url0": url, "title0": "Popular", "slug0": edited["slug"],
                                 "directory0": "keep-me", "library0": "Manual Renamed", "limit0": "10",
                                 "rex_url": "http://127.0.0.1:32400"})
        manual = read_json(self.path)["catalogs"][0]
        self.assertEqual((manual["directory"], manual["library"]), ("keep-me", "Manual Renamed"))

    def test_refuses_other_pages_and_non_addons(self):
        with self.assertRaises(urllib.error.HTTPError) as answer:
            self.post("/addons", {"url": "%s/cinemeta/manifest.json" % self.base}, origin="https://evil.example")
        self.assertEqual(answer.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as answer:
            self.post("/addons", {"url": "%s/nothing/manifest.json" % self.base})
        self.assertEqual(answer.exception.code, 400)
        self.assertEqual(read_json(self.path).get("addons", []), [])

    def test_dmm_key_is_saved_never_shown_kept_when_left_empty_and_forgotten(self):
        form = {"rex_url": "http://127.0.0.1:32400", "dmm_api_key": " secret-sponsor-key "}
        page = self.post("/settings", form)
        self.assertEqual(read_json(self.path)["dmm_api_key"], "secret-sponsor-key")
        self.assertNotIn("secret-sponsor-key", page)
        self.assertIn("saved; type a new one to replace it", page)
        # Styled as the page's other fields, not the browser's white box on the dark page.
        self.assertRegex(page, r"input\[type=password\][^{]*\{[^}]*border-radius")
        self.post("/settings", dict(form, dmm_api_key=""))
        self.assertEqual(read_json(self.path)["dmm_api_key"], "secret-sponsor-key")
        self.post("/settings", dict(form, dmm_api_key="", dmm_forget="1"))
        self.assertIsNone(read_json(self.path)["dmm_api_key"])

    def test_invalid_settings_do_not_change_disk_or_running_config(self):
        before = read_json(self.path)
        running = dict(self.config)
        with self.assertRaises(urllib.error.HTTPError) as answer:
            self.post("/settings", {"take": ["0"], "url0": "not-a-catalog", "title0": "bad", "limit0": "1"})
        self.assertEqual(answer.exception.code, 400)
        self.assertEqual(read_json(self.path), before)
        self.assertEqual(self.config, running)

    def test_remove_requires_an_existing_index(self):
        self.post("/addons", {"url": "%s/cinemeta/manifest.json" % self.base})
        with self.assertRaises(urllib.error.HTTPError) as answer:
            self.post("/addons/remove", {})
        self.assertEqual(answer.exception.code, 400)
        self.assertEqual(len(read_json(self.path)["addons"]), 1)

    def test_save_invalidates_resolve_cache(self):
        self.sync.cache["item"] = ("https://example.test/file", time.time() + 3600)
        self.config["max_resolution"] = 1080
        rex_stremio.App(self.path, self.config, self.sync).save(dict(self.config), sync=False)
        self.assertEqual(self.sync.cache, {})

    def test_rejects_oversized_form_body(self):
        host, port = self.url.removeprefix("http://").split(":")
        with socket.create_connection((host, int(port)), timeout=30) as client:
            client.sendall(("POST /settings HTTP/1.1\r\nHost: %s:%s\r\nOrigin: %s\r\nContent-Length: %d\r\n\r\n"
                            % (host, port, self.url, rex_stremio.MAX_FORM_BYTES + 1)).encode())
            self.assertIn(b" 413 ", client.recv(1024))


class PartialSync(unittest.TestCase):
    def test_malformed_cached_info_does_not_drop_healthy_item(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-cache-"),
                                  "catalogs": [{"url": "https://a/catalog/movie/one.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        catalog = config["catalogs"][0]
        vid = "%s-tt1234567" % catalog["slug"]
        with open(sync.path("info", vid), "w") as handle:
            json.dump(["corrupt"], handle)
        probe = {"format": {"format_name": "matroska,webm", "size": "1"}, "streams": [{"codec_type": "video"}]}
        def probe_after_repair(*args):
            self.assertEqual(read_json(sync.path("info", vid))["sid"], "tt1234567")
            return probe
        with unittest.mock.patch.object(rex_stremio, "get_json", return_value={"metas": [{"id": "tt1234567", "name": "Healthy"}]}), \
             unittest.mock.patch.object(sync, "probe", side_effect=probe_after_repair):
            items = sync.catalog_items(catalog)
        self.assertEqual(len(items), 1)
        self.assertTrue(sync.catalog_incomplete)
        with open(sync.path("info", vid)) as handle:
            self.assertEqual(json.load(handle)["sid"], "tt1234567")

    def test_empty_catalog_is_partial(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/one.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        with unittest.mock.patch.object(rex_stremio, "get_json", return_value={"metas": []}):
            self.assertEqual(sync.catalog_items(config["catalogs"][0]), [])
        self.assertTrue(sync.catalog_incomplete)

    def test_corrupt_info_is_repaired_before_real_probe_and_handler(self):
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        work = tempfile.mkdtemp(prefix="rex-stremio-probe-")
        path = config_file(self, {"cache": work, "relay_url": "http://127.0.0.1:0", "ffprobe": os.path.join(HERE, "fake_ffprobe.py"),
                                  "catalogs": [{"url": "%s/cinemeta/catalog/movie/top.json" % base, "title": "Top films", "limit": 1}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_stremio.App(path, config, sync).handler())
        server_url = "http://127.0.0.1:%d" % server.server_address[1]
        config["relay_url"] = server_url
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        vid = "top-films-tt34384900"
        with open(sync.path("info", vid), "w") as handle:
            handle.write("{truncated")
        sync.resolve = lambda item: "http://media.example/file.mkv"
        with unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url):
            items = sync.catalog_items(config["catalogs"][0])
        self.assertEqual([item["id"] for item in items], ["st-" + vid])
        self.assertEqual(read_json(sync.path("info", vid))["sid"], "tt34384900")
        self.assertEqual(read_json(sync.path(rex_stremio.PROBES, vid))["format"]["format_name"], "hls")

    def test_corrupt_probe_is_repaired_by_real_handler_and_ffprobe(self):
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        work = tempfile.mkdtemp(prefix="rex-stremio-probe-")
        path = config_file(self, {"cache": work, "relay_url": "http://127.0.0.1:0", "ffprobe": os.path.join(HERE, "fake_ffprobe.py"),
                                  "catalogs": [{"url": "%s/cinemeta/catalog/movie/top.json" % base, "title": "Top films", "limit": 1}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_stremio.App(path, config, sync).handler())
        config["relay_url"] = "http://127.0.0.1:%d" % server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        vid = "top-films-tt34384900"
        with open(sync.path("info", vid), "w") as handle:
            json.dump({"kind": "movie", "sid": "tt34384900"}, handle)
        with open(sync.path(rex_stremio.PROBES, vid), "w") as handle:
            handle.write("{truncated")
        sync.resolve = lambda item: "http://media.example/file.m3u8"
        with unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url):
            items = sync.catalog_items(config["catalogs"][0])
        self.assertEqual([item["id"] for item in items], ["st-" + vid])
        self.assertEqual(read_json(sync.path(rex_stremio.PROBES, vid))["format"]["format_name"], "hls")

    def test_incomplete_catalog_upserts_healthy_items_without_snapshot(self):
        path = config_file(self, {"catalogs": [
            {"url": "https://a/catalog/movie/one.json"},
            {"url": "https://a/catalog/movie/two.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        healthy = {"id": "healthy", "path": "films/healthy.mkv"}
        calls = []
        sync.register = lambda config=None: None
        sync.catalog_items = unittest.mock.Mock(side_effect=[[healthy], RuntimeError("fixture failure")])

        def rex(method, request_path, body=None, config=None):
            calls.append((method, request_path, body))
            return {"created": 1}

        sync.rex = rex
        sync.once()
        self.assertEqual(calls, [("POST", "/rex/feed/stremio/items", {"items": [healthy]})])

    def test_settings_change_aborts_obsolete_snapshot(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/one.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        healthy = {"id": "healthy", "path": "films/healthy.mkv"}
        calls = []
        sync.register = lambda config=None: None

        def catalog_items(catalog, config=None, version=None, ready=None):
            app.save(dict(sync.config), sync=False)
            return [healthy]

        sync.catalog_items = catalog_items
        sync.rex = lambda method, request_path, body=None, config=None: calls.append((method, request_path, body)) or {}
        sync.once()
        self.assertEqual(calls, [])

    def test_malformed_catalog_rows_are_partial_upserts(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/one.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        calls = []
        sync.register = lambda config=None: None
        with unittest.mock.patch.object(rex_stremio, "get_json", return_value={"metas": [{"id": "source-only-id"}]}), \
             unittest.mock.patch.object(sync, "cached", side_effect=AssertionError("invalid rows must not be probed")):
            sync.rex = lambda method, request_path, body=None, config=None: calls.append((method, request_path, body)) or {}
            sync.once()
        self.assertEqual(calls, [])

    def test_each_catalog_reaches_rex_before_the_next_is_checked(self):
        # A first sync of 61 catalogs took hours, and rex showed nothing until the last was done.
        path = config_file(self, {"catalogs": [
            {"url": "https://a/catalog/movie/one.json"},
            {"url": "https://a/catalog/movie/two.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        first, second = {"id": "first", "path": "films/first.mkv"}, {"id": "second", "path": "films/second.mkv"}
        calls = []
        sync.register = lambda config=None: None

        def catalog_items(catalog, config=None, version=None, ready=None):
            if catalog["url"].endswith("two.json"):
                self.assertIn(("POST", "/rex/feed/stremio/items", {"items": [first]}), calls)
                return [second]
            return [first]

        sync.catalog_items = catalog_items
        sync.rex = lambda method, request_path, body=None, config=None: calls.append((method, request_path, body)) or {}
        sync.once()
        self.assertEqual([call[:2] for call in calls], [("POST", "/rex/feed/stremio/items")] * 2
                         + [("PUT", "/rex/feed/stremio/snapshot")])
        self.assertEqual(calls[-1][2]["items"], [first, second])


class Concurrency(unittest.TestCase):
    def test_save_stops_the_obsolete_pass_from_probing(self):
        # After a save, the old pass's relay paths 404: on one VPS (2026-10-26) it went on to fail
        # 5,147 probes over its remaining catalogs before the new settings were synced at all.
        # One worker: with N, up to N probes are already in flight when the save lands.
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-cache-"), "probe_workers": 1, "catalogs": [
            {"url": "https://a/catalog/movie/one.json"},
            {"url": "https://a/catalog/movie/two.json"}]})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        sync.register = lambda config=None: None
        calls, probed = [], []
        sync.rex = lambda method, request_path, body=None, config=None: calls.append((method, request_path)) or {}

        def probe(vid, catalog, config=None):
            probed.append(vid)
            app.save(dict(sync.config), sync=False)
            raise RuntimeError("http://relay Server returned 404 Not Found")

        metas = {"metas": [{"id": "tt%07d" % n, "name": "Film %d" % n} for n in range(1, 4)]}
        with unittest.mock.patch.object(rex_stremio, "get_json", return_value=metas), \
             unittest.mock.patch.object(sync, "probe", side_effect=probe):
            sync.once()
        self.assertEqual(len(probed), 1)
        self.assertEqual(calls, [])
        self.assertEqual(sync.status, "settings changed during sync; retrying")

    def test_blocked_catalog_scan_allows_save_and_aborts_old_feed(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/top.json"}],
                                  "cache": tempfile.mkdtemp(prefix="rex-stremio-lock-")})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        entered, release, saved = threading.Event(), threading.Event(), threading.Event()
        errors = []
        calls = []

        sync.register = lambda config=None: None

        def blocked_catalog(*args, **kwargs):
            entered.set()
            release.wait(10)
            return [{"id": "stale", "path": "films/stale.mkv"}]

        sync.catalog_items = blocked_catalog
        sync.rex = lambda method, request_path, body=None, config=None: calls.append((method, request_path, body)) or {}
        worker = threading.Thread(target=sync.once)
        worker.start()
        self.assertTrue(entered.wait(5))

        def save():
            try:
                app.save(dict(sync.config), sync=False)
            except Exception as error:  # surface worker failures after cleanup
                errors.append(error)
            finally:
                saved.set()

        saver = threading.Thread(target=save)
        saver.start()
        try:
            self.assertTrue(saved.wait(2), "settings save waited for the catalog scan")
        finally:
            release.set()
            worker.join(10)
            saver.join(10)
        self.assertFalse(errors)
        self.assertEqual(calls, [])

    def test_old_generation_cache_update_cannot_land_after_save(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-cache-")})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        info_path = sync.path("info", "item")
        record = sync.record_lock(info_path)
        record.acquire()
        started = threading.Event()

        def old_writer():
            started.set()
            sync.cache_update(info_path, {"stream": "stale"}, version=0)

        writer = threading.Thread(target=old_writer)
        writer.start()
        self.assertTrue(started.wait(5))
        app.save(dict(sync.config), sync=False)
        record.release()
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertFalse(os.path.exists(info_path))

    def test_old_generation_cached_initialization_cannot_land_after_save(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-cache-")})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        entered, release = threading.Event(), threading.Event()

        def make_probe():
            entered.set()
            release.wait(10)
            return {"format": {"format_name": "hls"}}

        result = []
        worker = threading.Thread(target=lambda: result.append(sync.cached(rex_stremio.PROBES, "item", make_probe,
                                                                             config, version=0)))
        worker.start()
        self.assertTrue(entered.wait(5))
        app.save(dict(sync.config), sync=False)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [{"format": {"format_name": "hls"}}])
        self.assertFalse(os.path.exists(sync.path(rex_stremio.PROBES, "item")))

    def test_old_generation_remove_cannot_delete_after_save(self):
        path = config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-cache-")})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        info_path = sync.path("info", "item")
        sync.cache_write(info_path, {"kind": "movie", "sid": "tt1"})
        record = sync.record_lock(info_path)
        record.acquire()
        started = threading.Event()

        def old_remove():
            started.set()
            sync.cache_remove(info_path, version=0)

        worker = threading.Thread(target=old_remove)
        worker.start()
        self.assertTrue(started.wait(5))
        app.save(dict(sync.config), sync=False)
        record.release()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(os.path.exists(info_path))

    def test_blocked_rex_request_does_not_block_relay_or_settings_get(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/top.json"}],
                                  "cache": tempfile.mkdtemp(prefix="rex-stremio-lock-")})
        config = rex_stremio.load_config(path)
        sync = rex_stremio.Sync(config)
        app = rex_stremio.App(path, config, sync)
        vid = "top-tt1234567"
        with open(sync.path("info", vid), "w") as handle:
            json.dump({"kind": "movie", "sid": "tt1234567", "extension": "mkv"}, handle)
        sync.resolve = lambda item: "http://media.example/file.mkv"
        entered, release = threading.Event(), threading.Event()

        def blocked_rex(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return {}

        sync.rex = blocked_rex
        sync.catalog_items = lambda *args, **kwargs: []
        worker = threading.Thread(target=sync.once)
        worker.start()
        self.assertTrue(entered.wait(10))
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        started = time.monotonic()
        self.assertIn("rex-stremio", urllib.request.urlopen(base + "/", timeout=2).read().decode())
        self.assertLess(time.monotonic() - started, 2)
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as answer:
            opener.open(base + "/stremio-films/top/top-tt1234567.mkv", timeout=2)
        self.assertEqual(answer.exception.code, 302)
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())


class Relay(unittest.TestCase):
    """Links that need their addon's headers (`proxyHeaders`), and links that are no video."""

    def setUp(self):
        self.host, self.base = addon_host()
        self.addCleanup(stop_server, self.host)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        path = config_file(self, {"cache": work, "addons": [addon(self.base, "hyper"), addon(self.base, "eclipsia")],
                                  "catalogs": [{"url": self.base + "/cinemeta/catalog/movie/top.json", "title": "Films"}]})
        self.config = rex_stremio.load_config(path)
        self.sync = rex_stremio.Sync(self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_stremio.App(path, self.config, self.sync).handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        self.relay = "http://127.0.0.1:%d" % server.server_address[1]
        self.info = self.sync.path("info", "films-tt0111161")

    def pin(self, answer, at):
        stream = json.loads(json.dumps(STREMIO[answer]["streams"][at]).replace("{STREAMS}", self.base))
        with open(self.info, "w") as handle:
            json.dump({"kind": "movie", "sid": "tt0111161", "stream": rex_stremio.stream_key(stream)}, handle)
        return stream

    def get(self, path, **headers):
        request = urllib.request.Request(self.relay + path, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()

    def test_a_link_that_needs_headers_is_relayed_with_them(self):
        stream = self.pin("/hyper/stream/movie/tt0111161.json", 2)
        with self.assertRaises(urllib.error.HTTPError) as refused:  # the link alone, as rex read it before
            urllib.request.urlopen(stream["url"], timeout=30)
        self.assertEqual(refused.exception.code, 429)
        # Its own bytes, not another addon's link that needs nothing.
        self.assertEqual(self.get("/stremio-films/films/films-tt0111161.stream"), (200, MP4 + b"hyper-2.mp4"))
        self.assertEqual(self.get("/stremio-films/films/films-tt0111161.stream", Range="bytes=4-11"), (206, MP4[4:12]))

    def test_a_playlist_that_needs_headers_names_only_relayed_links(self):
        self.pin("/eclipsia/stream/movie/tt0111161.json", 5)  # Castle's HLS
        status, master = self.get("/stremio-films/films/films-tt0111161.stream")
        variant = [line for line in master.decode().splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(variant), 1)
        self.assertRegex(variant[0], r"^/_relay/films-tt0111161/[0-9a-f]{20}\.m3u8$")
        media = self.get(variant[0])[1].decode()
        self.assertRegex(media, r'URI="/_relay/films-tt0111161/[0-9a-f]{20}\.bin"')
        segments = [line for line in media.splitlines() if line and not line.startswith("#")]
        self.assertEqual([self.get(segment) for segment in segments], [(200, b"G" * 376)] * 2)
        with self.assertRaises(urllib.error.HTTPError) as unknown:  # nothing a playlist did not name
            self.get("/_relay/films-tt0111161/%s.ts" % ("0" * 20))
        self.assertEqual(unknown.exception.code, 404)

    def test_an_hls_master_is_relayed_as_its_best_variant(self):
        stream = dict(USATV[0], url=self.base + "/video/abc-master.m3u8")
        with open(self.info, "w") as handle:
            json.dump({"kind": "tv", "sid": "ustv-abc", "stream": rex_stremio.stream_key(stream)}, handle)
        with unittest.mock.patch.object(rex_stremio.Sync, "streams", lambda *args, **kwargs: [stream]):
            status, body = self.get("/stremio-films/films/films-tt0111161.stream")
        variants = [line for line in body.decode().splitlines() if line.startswith("#EXT-X-STREAM-INF")]
        self.assertEqual((status, len(variants)), (200, 1))
        self.assertIn("RESOLUTION=1280x720,BANDWIDTH=6617500", variants[0])
        self.assertTrue(body.decode().rstrip().endswith("https://content-aeuc1-up-1.uplynk.com/channel/39919d3f7a074eefa8bf579214e952f9/e.m3u8?pbs=PBS"))

    def test_a_trimmed_master_is_asked_for_as_the_player_asks(self):
        stream = dict(USATV[0], url=self.base + "/akamai/master.m3u8")
        with open(self.info, "w") as handle:
            json.dump({"kind": "tv", "sid": "ustv-abc", "stream": rex_stremio.stream_key(stream)}, handle)
        with unittest.mock.patch.object(rex_stremio.Sync, "streams", lambda *args, **kwargs: [stream]):
            body = self.get("/stremio-films/films/films-tt0111161.stream", **{"User-Agent": "Lavf/61.7.100"})[1].decode()
        variant = body.rstrip().splitlines()[-1]
        self.assertIn("/akamai/high.m3u8", variant)
        request = urllib.request.Request(variant, headers={"User-Agent": "Lavf/61.7.100"})
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.status, 200)

    def test_a_stream_without_a_picture_gives_way_to_the_next(self):
        self.pin("/eclipsia/stream/movie/tt0111161.json", 0)
        probes = iter([read_json(os.path.join(FIXTURES, "probe-pluto-mst3k-no-video.json")),
                       read_json(os.path.join(FIXTURES, "probe-dmm-cast-tt30144839.json"))])
        metas = {"metas": [{"id": "tt0111161", "type": "movie", "name": "The Shawshank Redemption"}]}
        with unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda *args: next(probes)), \
                unittest.mock.patch.object(rex_stremio, "get_json", lambda url, timeout=60: metas):
            items = self.sync.catalog_items(self.config["catalogs"][0])
        self.assertEqual(len(items), 1)
        self.assertTrue(any(stream["codec_type"] == "video" for stream in items[0]["media_info"]["streams"]))

    def test_a_placeholder_clip_gives_way_to_the_next(self):
        self.pin("/eclipsia/stream/movie/tt0111161.json", 0)
        # Freedom Media's 10 s clip, which every one of its links played, then a film.
        probes = iter([read_json(os.path.join(FIXTURES, "probe-freedom-media-placeholder.json")),
                       read_json(os.path.join(FIXTURES, "probe-dmm-cast-tt30144839.json"))])
        metas = {"metas": [{"id": "tt0111161", "type": "movie", "name": "The Shawshank Redemption"}]}
        with unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda *args: next(probes)), \
                unittest.mock.patch.object(rex_stremio, "get_json", lambda url, timeout=60: metas):
            items = self.sync.catalog_items(self.config["catalogs"][0])
        self.assertEqual(len(items), 1)
        self.assertGreater(float(items[0]["media_info"]["format"]["duration"]), 60)
        self.assertEqual([entry["key"] for entry in read_json(self.info)["refused"]], [self.base + "/hdr/eclipsia-0.mp4"])

    def test_a_stream_rex_cannot_take_gives_way_to_the_next(self):
        self.pin("/eclipsia/stream/movie/tt0111161.json", 0)
        probes = iter([{"format": {"format_name": "eac3"}, "streams": []},
                       {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}, "streams": [{"codec_type": "video"}]}])
        metas = {"metas": [{"id": "tt0111161", "type": "movie", "name": "The Shawshank Redemption"}]}
        with unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda *args: next(probes)), \
                unittest.mock.patch.object(rex_stremio, "get_json", lambda url, timeout=60: metas):
            items = self.sync.catalog_items(self.config["catalogs"][0])
        self.assertEqual([item["path"] for item in items], ["stremio-films/films/films-tt0111161.mp4"])
        info = read_json(self.info)
        self.assertEqual([entry["key"] for entry in info["refused"]], [self.base + "/hdr/eclipsia-0.mp4"])
        # The next resolve skips what was set aside.
        self.assertEqual(self.sync.resolve("films-tt0111161"), self.base + "/hdr/eclipsia-1.m3u8")


class RateLimitedAddon(unittest.TestCase):
    """111477 behind Cloudflare's rate limit (error 1015, `stream-111477-cloudflare-1015.json`):
    a catalog's titles asked one after another tripped it after 13 of 50 films, and every title
    after that failed within seconds until the next sync, six hours later (2026-11-03)."""
    CAPTURED = read_json(os.path.join(FIXTURES, "stream-111477-cloudflare-1015.json"))
    TITLES = ["tt%07d" % number for number in range(1, 9)]

    def serve(self, allowed=3, window=1.0, retry_after=True, refuse_after=None):
        """The addon as Cloudflare fronts it: `allowed` stream answers per `window` seconds, then
        the captured 429 until that window ends, its Retry-After the seconds left (left out when
        not `retry_after`). From `refuse_after` stream requests on, it refuses every one."""
        asked, served, state, lock = [], [], {"blocked": 0.0}, threading.Lock()
        captured, titles = self.CAPTURED, self.TITLES

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, headers, raw):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urllib.parse.unquote(self.path)
                if "/catalog/" in path:
                    body = {"metas": [{"id": title, "type": "movie", "name": title} for title in titles]}
                    return self.answer(200, {"Content-Type": "application/json"}, json.dumps(body).encode())
                now = time.monotonic()
                with lock:
                    asked.append((now, path))
                    if now >= state["blocked"] and (refuse_after is None or len(asked) <= refuse_after):
                        if sum(1 for at, _ in served if now - at < window) < allowed:
                            served.append((now, path))
                            body = dict(captured["answered"], streams=[dict(stream, url=stream["url"] + "?" + path)
                                                                       for stream in captured["answered"]["streams"]])
                            return self.answer(200, {"Content-Type": "application/json"}, json.dumps(body).encode())
                        state["blocked"] = now + window
                    left = max(state["blocked"] - now, 0.001 if refuse_after is None else window)
                headers = {name: value for name, value in captured["refused"]["headers"].items()
                           if name not in ("content-length", "retry-after", "date")}
                if retry_after:
                    headers["Retry-After"] = str(math.ceil(left))
                self.answer(captured["refused"]["status"], headers, captured["refused"]["body"].encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        config = rex_stremio.load_config(config_file(self, {
            "cache": work, "stream_interval_seconds": 0.2, "retry_minutes": 1,
            "addons": [{"url": base + "/111477/manifest.json", "manifest": {
                "id": "org.111477.stremio", "name": "111477 stremio", "resources": ["stream"], "types": ["movie", "series"],
                "idPrefixes": ["tt"], "catalogs": []}}],
            "catalogs": [{"url": base + "/cinemeta/catalog/movie/top.json", "title": "Popular films"}]}))
        sync = rex_stremio.Sync(config)
        probe = read_json(os.path.join(FIXTURES, "probe-top-films-tt6262770.json"))
        sync.probe = lambda vid, catalog, config=None: sync.resolve(vid) and probe
        patch = unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url)
        patch.start()
        self.addCleanup(patch.stop)
        return sync, config, asked, served

    def sync_all(self, **limits):
        sync, config, asked, served = self.serve(**limits)
        started = time.monotonic()
        items = sync.catalog_items(config["catalogs"][0])
        elapsed = time.monotonic() - started
        refused = [path for at, path in asked if (at, path) not in served]
        # Every title plays, each asked again in this pass after its refusal.
        self.assertEqual(sorted(item["ids"]["imdb"] for item in items), self.TITLES)
        self.assertTrue(refused, "the limit never tripped")
        for path in refused:
            self.assertIn(path, [answered for _, answered in served])
        # Paced: never two stream requests to the host closer than stream_interval_seconds.
        times = [at for at, _ in asked]
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.18)
        self.assertLess(elapsed, 20)
        return sync

    def test_429_with_retry_after_is_waited_out_and_the_title_asked_again(self):
        self.sync_all(retry_after=True)

    def test_429_without_retry_after_backs_off(self):
        backoff = rex_stremio.BACKOFF
        rex_stremio.BACKOFF = 0.5
        self.addCleanup(setattr, rex_stremio, "BACKOFF", backoff)
        self.sync_all(retry_after=False)

    def test_a_host_that_keeps_refusing_is_left_for_the_pass_and_retried_soon(self):
        sync, config, asked, served = self.serve(refuse_after=2, window=1.0)
        items, waits = [], []

        class Stop(Exception):
            pass

        def wait(seconds):
            waits.append(seconds)
            raise Stop

        sync.once = lambda: items.extend(sync.catalog_items(config["catalogs"][0]))
        sync.wake.wait = wait
        started = time.monotonic()
        with self.assertRaises(Stop):
            sync.forever()
        # Two titles play; the host then refuses three times in a row and is not asked again in
        # this pass, which ends at once instead of failing each title against it, and the next
        # pass comes after retry_minutes, not the six-hour interval.
        self.assertEqual(len(items), 2)
        self.assertEqual(len(asked), 2 + rex_stremio.REFUSALS)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(waits, [60])

    def test_a_null_streams_answer_is_a_refusal(self):
        # 111477 answers {"streams": null} while its own index is throttled, [] for a title it
        # does not know (captured): null is no answer, so the title is asked again later.
        sync, config, asked, served = self.serve()
        with unittest.mock.patch.object(rex_stremio, "get_json", return_value=self.CAPTURED["throttled"]), \
                unittest.mock.patch.object(rex_stremio, "BACKOFF", 0.01):
            self.assertEqual(sync.streams("movie", "tt0111161", config), [])
        self.assertTrue(sync.retry_soon)
        sync.retry_soon = False
        sync.hosts.clear()
        with unittest.mock.patch.object(rex_stremio, "get_json", return_value=self.CAPTURED["unknown"]):
            self.assertEqual(sync.streams("movie", "tt9999999999", config), [])
        self.assertFalse(sync.retry_soon)


MATRIX = read_json(os.path.join(FIXTURES, "torrentio-rd-matrix-2026-11-06.json"))["streams"]
MATRIX_DMM = read_json(os.path.join(FIXTURES, "dmm-mediainfo-matrix-2026-11-06.json"))["releases"]
TIGOLE, IAMABLE = "12f8ca9709b357477612aaac7c6703503c410352", "03dd34fea0ff15a451c1723062a901aa3a0ad458"
SIZES = {TIGOLE: 15237850498, IAMABLE: 37675795874}  # the files the two releases' links serve


def dmm_host(test, known, key="sponsor-key", shared=None):
    """DMM's /api/sponsor/mediainfo over `known` releases: 401 for another key, each answer a
    subset of the hashes asked. Returns its URL and the hash lists it was asked, in order. Probes
    posted to /api/sponsor/probes go into `shared`, a list, as (time, probes)."""
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("x-api-key") != key:
                raw, status = b'{"error":"Invalid API key"}', 401
            elif self.path.endswith("/probes"):
                (shared if shared is not None else []).append((time.monotonic(), body["probes"]))
                raw, status = json.dumps({"saved": len(body["probes"]), "known": 0}).encode(), 200
            else:
                asked.append((time.monotonic(), body["hashes"]))
                raw, status = json.dumps({"releases": {h: known[h] for h in body["hashes"] if h in known}}).encode(), 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(stop_server, server)
    return "http://127.0.0.1:%d/api/sponsor/mediainfo" % server.server_address[1], asked


def size_host(test):
    """Debrid links as they answer a one-byte Range: the size of the release file each serves."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            size = next((size for hash_, size in SIZES.items() if hash_ in self.path), 1 << 30)
            self.send_response(206)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Range", "bytes 0-0/%d" % size)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"\x1a")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(stop_server, server)
    return "http://127.0.0.1:%d" % server.server_address[1]


class StoredProbes(unittest.TestCase):
    """DMM's stored probes stand in for ffprobe (2026-11-06: DMM held 9 of the 10 Matrix releases
    Torrentio ranks first). Captured: Torrentio's answer over RD and DMM's answer for two of them."""

    def test_info_hash_from_the_stream_or_its_link(self):
        self.assertEqual([rex_stremio.info_hash(stream) for stream in MATRIX], [IAMABLE, TIGOLE])
        # A p2p stream names it itself (Torrentio without a debrid service).
        self.assertEqual(rex_stremio.info_hash({"infoHash": "61065EA115B7CC3E8DB9FB5AB1F6F327F08BD1C9", "fileIdx": 0}),
                         "61065ea115b7cc3e8db9fb5ab1f6f327f08bd1c9")
        self.assertIsNone(rex_stremio.info_hash(DMM[0]))  # a DMM Cast link carries none
        self.assertIsNone(rex_stremio.info_hash({"url": "https://a/%s0/x.mkv" % TIGOLE}))  # 41 hex digits are no hash

    def test_the_file_of_that_size_and_name(self):
        main = rex_stremio.stored_probe(MATRIX_DMM[TIGOLE], SIZES[TIGOLE])
        self.assertEqual(len(main["streams"]), 27)
        self.assertEqual(main["format"]["format_name"], "matroska,webm")
        self.assertIsNone(rex_stremio.stored_probe(MATRIX_DMM[TIGOLE], SIZES[TIGOLE] + 1))
        named = rex_stremio.stored_probe(MATRIX_DMM[IAMABLE], SIZES[IAMABLE], "The.Matrix.1999.RERIP.2160p.UHD.BluRay.X265-IAMABLE.mkv")
        self.assertEqual(named["format"]["size"], str(SIZES[IAMABLE]))
        self.assertIsNone(rex_stremio.stored_probe(MATRIX_DMM[IAMABLE], SIZES[IAMABLE], "another.mkv"))
        # DMM lists this release's sample twice, under two paths and the same size: no answer.
        self.assertIsNone(rex_stremio.stored_probe(MATRIX_DMM[IAMABLE], 281993068))

    def test_hashes_asked_together_in_batches_and_spaced(self):
        url, asked = dmm_host(self, MATRIX_DMM)
        dmm = rex_stremio.StoredProbes("sponsor-key", url)
        hashes = [TIGOLE, IAMABLE] + ["%040x" % n for n in range(1, 24)]
        with unittest.mock.patch.object(rex_stremio, "DMM_BATCH", 10), unittest.mock.patch.object(rex_stremio, "DMM_SPACING", 0.3):
            answers = {}
            workers = [threading.Thread(target=lambda h=h: answers.__setitem__(h, dmm.release(h))) for h in hashes]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
        self.assertEqual(sorted(h for batch in asked for h in batch[1]), sorted(hashes))
        self.assertEqual([len(batch) for _, batch in asked], [10, 10, 5])
        self.assertGreaterEqual(min(b[0] - a[0] for a, b in zip(asked, asked[1:])), 0.29)
        self.assertEqual(sorted(h for h, release in answers.items() if release), sorted([TIGOLE, IAMABLE]))
        self.assertIsNone(dmm.release("%040x" % 1))  # known not to be there: not asked again
        self.assertEqual(len(asked), 3)

    def test_a_refused_key_is_not_used_again(self):
        url, asked = dmm_host(self, MATRIX_DMM)
        dmm = rex_stremio.StoredProbes("lapsed", url)
        self.assertIsNone(dmm.release(TIGOLE))
        self.assertTrue(dmm.refused)
        self.assertIsNone(dmm.release(IAMABLE))
        self.assertEqual(asked, [])

    def sync_matrix(self, streams, key="sponsor-key", known=MATRIX_DMM, fallback="probe-top-films-tt6262770.json", shared=None):
        url, asked = dmm_host(self, known, shared=shared)
        links = size_host(self)
        streams = [dict(stream, url=stream["url"].replace("https://torrentio.strem.fun", links)) for stream in streams]
        config = rex_stremio.load_config(config_file(self, {
            "cache": tempfile.mkdtemp(prefix="rex-stremio-"), "dmm_api_key": key,
            "catalogs": [{"url": "https://a/catalog/movie/top.json", "title": "Films"}]}))
        sync = rex_stremio.Sync(config)
        probed = []
        fallback = read_json(os.path.join(FIXTURES, fallback))
        metas = {"metas": [{"id": "tt0133093", "type": "movie", "name": "The Matrix"}]}
        with unittest.mock.patch.object(rex_stremio, "DMM_MEDIAINFO", url), \
                unittest.mock.patch.object(rex_stremio, "DMM_PROBES", url.replace("/mediainfo", "/probes")), \
                unittest.mock.patch.object(rex_stremio, "get_json", lambda *args, **kwargs: metas), \
                unittest.mock.patch.object(rex_stremio, "opens", lambda link, headers=None: link), \
                unittest.mock.patch.object(rex_stremio.Sync, "streams", lambda *args, **kwargs: streams), \
                unittest.mock.patch.object(rex_stremio.Sync, "probe", lambda self, vid, *rest: probed.append(vid) or fallback):
            items = sync.catalog_items(config["catalogs"][0])
        return items, probed, asked

    def test_a_title_dmm_knows_goes_to_rex_without_ffprobe(self):
        items, probed, asked = self.sync_matrix([MATRIX[1]])
        self.assertEqual(probed, [])
        self.assertEqual([batch for _, batch in asked], [[TIGOLE]])
        self.assertEqual(items[0]["media_info"], rex_stremio.stored_probe(MATRIX_DMM[TIGOLE], SIZES[TIGOLE]))
        self.assertEqual((items[0]["path"], items[0]["size"]), ("stremio-films/films/films-tt0133093.mkv", SIZES[TIGOLE]))

    def test_a_named_file_is_matched_by_its_name_too(self):
        items, probed, _ = self.sync_matrix([MATRIX[0]])
        self.assertEqual(probed, [])
        self.assertEqual(items[0]["size"], SIZES[IAMABLE])

    def test_what_dmm_does_not_know_is_probed(self):
        items, probed, asked = self.sync_matrix([MATRIX[1]], known={})
        self.assertEqual((probed, [batch for _, batch in asked]), (["films-tt0133093"], [[TIGOLE]]))
        self.assertEqual(items[0]["media_info"]["format"]["format_name"], "hls")

    def test_a_probe_ffprobe_took_goes_back_to_dmm(self):
        # A Torrentio-over-RD episode file's real probe (Matroska), for a release DMM did not know.
        shared = []
        items, probed, _ = self.sync_matrix([MATRIX[0]], known={}, fallback="probe-top-shows-tt11540908-1-1.json", shared=shared)
        self.assertEqual(probed, ["films-tt0133093"])
        [(_, probes)] = shared
        probe = read_json(os.path.join(FIXTURES, "probe-top-shows-tt11540908-1-1.json"))
        self.assertEqual(probes, [{"hash": IAMABLE, "bytes": int(probe["format"]["size"]),
                                   "name": "The.Matrix.1999.RERIP.2160p.UHD.BluRay.X265-IAMABLE.mkv",
                                   "MediaInfo": dict(probe, format={k: v for k, v in probe["format"].items() if k != "filename"})}])
        self.assertNotIn("filename", probes[0]["MediaInfo"]["format"])  # the relay address stays here
        self.assertEqual(items[0]["media_info"], probe)  # and the item went to rex as before

    def test_a_link_without_a_file_name_is_shared_by_its_size_alone(self):
        shared = []
        self.sync_matrix([MATRIX[1]], known={}, fallback="probe-top-shows-tt11540908-1-1.json", shared=shared)
        self.assertEqual([(p["hash"], "name" in p) for _, batch in shared for p in batch], [(TIGOLE, False)])

    def test_what_dmm_knew_is_not_sent_back(self):
        shared = []
        self.sync_matrix([MATRIX[1]], shared=shared)
        self.assertEqual(shared, [])

    def test_share_probes_off_keeps_probes_here(self):
        shared = []
        with unittest.mock.patch.dict(rex_stremio.DEFAULTS, {"share_probes": False}):
            self.sync_matrix([MATRIX[0]], known={}, fallback="probe-top-shows-tt11540908-1-1.json", shared=shared)
        self.assertEqual(shared, [])

    def test_a_playlist_probe_is_not_shared(self):
        # An HLS probe's format.size is the playlist's, not a file's.
        shared = []
        self.sync_matrix([MATRIX[1]], known={}, shared=shared)
        self.assertEqual(shared, [])

    def test_shared_probes_go_in_batches_spaced_and_stop_at_a_refused_key(self):
        shared = []
        url, _ = dmm_host(self, {}, shared=shared)
        dmm = rex_stremio.StoredProbes("sponsor-key", url, url.replace("/mediainfo", "/probes"))
        probe = {"hash": TIGOLE, "bytes": 1, "MediaInfo": {"format": {"format_name": "matroska,webm"}, "streams": []}}
        with unittest.mock.patch.object(rex_stremio, "SHARE_BATCH", 2), unittest.mock.patch.object(rex_stremio, "DMM_SPACING", 0.3):
            for n in range(5):
                dmm.share(dict(probe, bytes=n + 1))
            self.assertEqual(dmm.flush(), 4)  # full batches only while a sync goes on
            self.assertEqual(dmm.flush(final=True), 1)  # the rest when its catalog is done
        self.assertEqual([[p["bytes"] for p in batch] for _, batch in shared], [[1, 2], [3, 4], [5]])
        self.assertGreaterEqual(min(b[0] - a[0] for a, b in zip(shared, shared[1:])), 0.29)
        lapsed = rex_stremio.StoredProbes("lapsed", url, url.replace("/mediainfo", "/probes"))
        lapsed.share(probe)
        self.assertEqual(lapsed.flush(final=True), 0)
        self.assertTrue(lapsed.refused)
        lapsed.share(probe)
        self.assertEqual(lapsed.outbox, [])  # nothing is queued for a key DMM refused

    def test_without_a_key_nothing_is_asked(self):
        items, probed, asked = self.sync_matrix([MATRIX[1]], key=None)
        self.assertEqual((probed, asked), (["films-tt0133093"], []))


class FlakyHost(unittest.TestCase):
    """A host that answers one read with a 5xx (archive.org did, 1 of 99 ffprobe reads, 2026-11-06)
    must not cost the title a day: the stream used to be set aside as unreadable."""

    def probe_with(self, **env):
        state = tempfile.mktemp(prefix="rex-stremio-flaky-")
        self.addCleanup(lambda: os.path.exists(state) and os.unlink(state))
        config = rex_stremio.load_config(config_file(self, {"cache": tempfile.mkdtemp(prefix="rex-stremio-"),
                                                            "ffprobe": os.path.join(HERE, "flaky_ffprobe.py")}))
        sync = rex_stremio.Sync(config)
        catalog = {"directory": "films", "slug": "films"}
        with unittest.mock.patch.dict(os.environ, dict(env, FLAKY_STATE=state)), \
                unittest.mock.patch.object(rex_stremio, "PROBE_RETRY_SECONDS", 0, create=True):
            try:
                return sync.probe("films-tt6262770", catalog, config), read_json_lines(state)
            except RuntimeError as error:
                return error, read_json_lines(state)

    def test_a_5xx_is_asked_again_before_the_stream_is_given_up(self):
        probe, runs = self.probe_with()
        self.assertIsInstance(probe, dict)
        self.assertEqual(runs, 2)

    def test_a_file_ffprobe_cannot_read_fails_at_once(self):
        error, runs = self.probe_with(FLAKY_ERROR="Invalid data found when processing input")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(runs, 1)


def read_json_lines(path):
    with open(path) as handle:
        return len(handle.readlines())


class ParallelProbes(unittest.TestCase):
    def test_items_are_probed_together_and_reach_rex_as_they_are_ready(self):
        config = rex_stremio.load_config(config_file(self, {
            "cache": tempfile.mkdtemp(prefix="rex-stremio-"), "probe_workers": 3,
            "catalogs": [{"url": "https://a/catalog/movie/top.json", "title": "Films"}]}))
        sync = rex_stremio.Sync(config)
        sync.register = lambda config=None: None
        probe = read_json(os.path.join(FIXTURES, "probe-top-films-tt6262770.json"))
        running, most, lock, calls = [0], [0], threading.Lock(), []

        def slow_probe(self, vid, *rest):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.4)
            with lock:
                running[0] -= 1
            return probe

        sync.rex = lambda method, path, body=None, config=None: calls.append((time.monotonic(), method, path, body)) or {}
        metas = {"metas": [{"id": "tt%07d" % n, "type": "movie", "name": "Film %d" % n} for n in range(1, 7)]}
        started = time.monotonic()
        with unittest.mock.patch.object(rex_stremio, "READY_SECONDS", 0), \
                unittest.mock.patch.object(rex_stremio, "get_json", lambda *args, **kwargs: metas), \
                unittest.mock.patch.object(rex_stremio.Sync, "probe", slow_probe):
            sync.once()
        self.assertEqual(most[0], 3)
        self.assertLess(time.monotonic() - started, 6 * 0.4)
        posts = [call for call in calls if call[1] == "POST"]
        # The first items were in rex while the last were still being probed...
        self.assertLess(posts[0][0] - started, 0.8)
        # ...and each went up once, the snapshot then naming all six.
        sent = [item["id"] for call in posts for item in call[3]["items"]]
        self.assertEqual(sorted(sent), sorted(set(sent)))
        self.assertEqual(len(sent), 6)
        self.assertEqual(calls[-1][1:3], ("PUT", "/rex/feed/stremio/snapshot"))
        self.assertEqual(len(calls[-1][3]["items"]), 6)


@unittest.skipUnless(os.path.exists(os.environ.get("REX_BIN", "/nonexistent")), "REX_BIN names no rex binary")
class AgainstRex(unittest.TestCase):
    TOKEN = "REX-STREMIO-TEST"

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rex-stremio-")
        self.port = free_port()
        env = dict(os.environ, REX_TRANSCODER="off", REX_DIRECT_URL="off", REX_PLEXTV_URL="http://127.0.0.1:9")
        self.rex = subprocess.Popen([os.environ["REX_BIN"], "--port", str(self.port), "--catalog", os.path.join(self.work, "catalog.json"),
                                     "--token", self.TOKEN, "--no-state", "--assert-claimed"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.addCleanup(stop_process, self.rex)
        for _ in range(100):
            try:
                self.get("/identity")
                break
            except OSError:
                time.sleep(0.1)
        host, base = addon_host()
        self.addCleanup(stop_server, host)
        cinemeta = rex_stremio.CINEMETA
        rex_stremio.CINEMETA = base + "/cinemeta"
        self.addCleanup(setattr, rex_stremio, "CINEMETA", cinemeta)
        relay = free_port()
        path = os.path.join(self.work, "config.json")
        with open(path, "w") as handle:
            json.dump({"rex_url": "http://127.0.0.1:%d" % self.port, "listen": "127.0.0.1:%d" % relay, "cache": os.path.join(self.work, "cache"),
                       "ffprobe": os.path.join(HERE, "fake_ffprobe.py"),
                       "addons": [addon(base, "dmm"), addon(base, "cinemeta"), addon(base, "httpx"), addon(base, "torrentio"),
                                  addon(base, "usatv")],
                       "catalogs": [
                           {"url": base + "/dmm/catalog/movie/casted-movies.json", "title": "DMM Cast"},
                           {"url": base + "/cinemeta/catalog/movie/top.json", "title": "Top films", "limit": 2},
                           {"url": base + "/cinemeta/catalog/series/top.json", "title": "Top shows", "limit": 3},
                           {"url": base + "/pdm/catalog/movie/publicdomainmovies.json", "title": "Public domain", "limit": 3},
                           {"url": base + "/usatv/catalog/tv/all.json", "title": "USA TV"}]},
                      handle)
        self.config = rex_stremio.load_config(path)
        self.sync = rex_stremio.Sync(self.config)
        self.app = rex_stremio.App(path, self.config, self.sync)
        server = ThreadingHTTPServer(("127.0.0.1", relay), self.app.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(stop_server, server)
        self.relay = relay
        # The captured links are real hosts' (and their keys redacted): taken as opening, as they did.
        patch = unittest.mock.patch.object(rex_stremio, "opens", lambda url, headers=None: url)
        patch.start()
        self.addCleanup(patch.stop)

    def films(self, sections):
        """The films library's titles not in the trash."""
        listed = self.get("/library/sections/%s/all" % sections["Stremio Films"]["key"])["MediaContainer"]["Metadata"]
        return [film for film in listed if not all(media.get("deletedAt") for media in
                                                    self.get("/library/metadata/%s" % film["ratingKey"])["MediaContainer"]["Metadata"][0]["Media"])]

    def scrobble(self, key):
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/:/scrobble?identifier=com.plexapp.plugins.library&key=%s"
                                                      % (self.port, key), headers={"X-Plex-Token": self.TOKEN}), timeout=30)

    def get(self, path):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         headers={"Accept": "application/json", "X-Plex-Token": self.TOKEN})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def test_sync_takes_catalogs(self):
        self.sync.once()
        sections = {row["title"]: row for row in self.get("/library/sections")["MediaContainer"]["Directory"]}
        films = self.get("/library/sections/%s/all" % sections["Stremio Films"]["key"])["MediaContainer"]["Metadata"]
        catalog = [meta["name"] for meta in STREMIO["/cinemeta/catalog/movie/top.json"]["metas"]]
        # DMM Cast's one film and Cinemeta's two with httpx's streams; Public Domain Movies' film is
        # torrents only, and its addon was never added.
        self.assertEqual(sorted(film["title"] for film in films), sorted(["One Battle After Another"] + catalog))
        containers = {film["title"]: self.get("/library/metadata/%s" % film["ratingKey"])["MediaContainer"]["Metadata"][0]["Media"][0]["container"]
                      for film in films}
        self.assertEqual(containers, dict({"One Battle After Another": "mpegts"}, **{name: "hls" for name in catalog}))
        shows = self.get("/library/sections/%s/all" % sections["Stremio Shows"]["key"])["MediaContainer"]["Metadata"]
        self.assertEqual([show["title"] for show in shows], [STREMIO["/cinemeta/catalog/series/top.json"]["metas"][0]["name"]])
        episodes = self.get("/library/metadata/%s/allLeaves" % shows[0]["ratingKey"])["MediaContainer"]["Metadata"]
        self.assertEqual([(e["parentIndex"], e["index"]) for e in episodes], [(1, 1), (1, 2), (1, 3)])
        # The live TV addon's channels: a library of their own, named by the addon. CW has no
        # stream, so it is left out (and the sync is partial, keeping rows it cannot see).
        channels = self.get("/library/sections/%s/all" % sections["Live TV"]["key"])["MediaContainer"]["Metadata"]
        # No duration: 0, or left out, as rex leaves out every empty field (nightly 2026.11.05).
        self.assertEqual([(channel["title"], channel.get("duration", 0)) for channel in channels], [("ABC", 0)])
        self.assertEqual(sections["Live TV"]["type"], "movie")
        # At play the relay asks every stream addon again and redirects to the pinned stream.
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as answer:
            opener.open("http://127.0.0.1:%d/stremio-shows/top-shows/top-shows-tt11540908-1-2.mkv" % self.relay, timeout=30)
        self.assertEqual(answer.exception.code, 302)
        self.assertEqual(answer.exception.headers["Location"], TORRENTIO[0]["url"])
        # DMM Cast's film plays its best labelled stream, not the link out to DMM's site.
        with self.assertRaises(urllib.error.HTTPError) as answer:
            opener.open("http://127.0.0.1:%d/stremio-films/dmm-cast/dmm-cast-tt30144839.ts" % self.relay, timeout=30)
        self.assertEqual(answer.exception.headers["Location"], DMM[2]["url"])
        # A channel plays its HD stream, its master as the best variant alone (the master as
        # captured, not fetched: tests read no real host).
        with open(os.path.join(FIXTURES, "hls-master-usatv-abc.m3u8")) as handle:
            master = handle.read()
        fetched = []
        with unittest.mock.patch.object(rex_stremio, "fetch_text", lambda url, agent: fetched.append(url) or (master, url)):
            with opener.open("http://127.0.0.1:%d/live-tv/usa-tv/usa-tv-%s.m3u8" % (self.relay, ABC), timeout=30) as answer:
                body = answer.read().decode()
        self.assertEqual(fetched, [USATV[0]["url"]])
        self.assertEqual(len([line for line in body.splitlines() if line.startswith("#EXT-X-STREAM-INF")]), 1)

    def test_catalogs_as_collections(self):
        """Cinemeta's Popular and Featured both list Tenzing (tt6262770): with `collections` on it
        is one film, probed once, in both catalogs' collections."""
        values = read_json(os.path.join(self.work, "config.json"))
        top = values["catalogs"][1]
        # Only catalogs every title of which plays, so each sync is complete and removes.
        values["catalogs"] = values["catalogs"][:3]
        values["catalogs"].insert(2, {"url": top["url"].replace("top.json", "imdbRating.json"), "title": "Featured films", "limit": 1})
        self.app.save(values, sync=False)
        self.sync.once()
        sections = {row["title"]: row for row in self.get("/library/sections")["MediaContainer"]["Directory"]}
        names = [meta["name"] for meta in STREMIO["/cinemeta/catalog/movie/top.json"]["metas"]]
        self.assertEqual(sorted(film["title"] for film in self.films(sections)), sorted(["One Battle After Another", names[1]] + names))
        # Turned on, every id moves (one per library, not per catalog), each with a file of its
        # own, which rex refuses for an id whose file another has: more than half of what rex
        # holds goes, which rex refuses unless the snapshot is forced, as a save forces it.
        values["collections"] = True
        self.app.save(values, sync=False)
        probed = []
        real_probe = self.sync.probe
        with unittest.mock.patch.object(self.sync, "probe", lambda vid, *rest: probed.append(vid) or real_probe(vid, *rest)):
            self.sync.once()
        self.assertIn("items in rex", self.sync.status)
        self.assertFalse([vid for vid in probed if vid.startswith("featured-films-")])
        films = self.films(sections)
        self.assertEqual(sorted(film["title"] for film in films), sorted(["One Battle After Another"] + names))
        listed = self.get("/library/sections/%s/collections" % sections["Stremio Films"]["key"])["MediaContainer"].get("Metadata", [])
        members = {collection["title"]: sorted(row["title"] for row in self.get("/library/collections/%s/children" % collection["ratingKey"])
                                               ["MediaContainer"].get("Metadata", []))
                   for collection in listed}
        self.assertEqual(members, {"DMM Cast": ["One Battle After Another"], "Top films": sorted(names), "Featured films": [names[1]]})
        shows = self.get("/library/sections/%s/collections" % sections["Stremio Shows"]["key"])["MediaContainer"].get("Metadata", [])
        self.assertEqual([collection["title"] for collection in shows], ["Top shows"])
        # Popular stops listing it: Featured takes it over, and rex keeps the film it was.
        shared = next(film for film in films if film["title"] == names[1])
        self.scrobble(shared["ratingKey"])
        values["catalogs"][1]["limit"] = 1
        self.app.save(values, sync=False)
        self.sync.once()
        self.assertIn("items in rex", self.sync.status)
        moved = [film for film in self.films(sections) if film["title"] == names[1]]
        self.assertEqual([(film["ratingKey"], film.get("viewCount")) for film in moved], [(shared["ratingKey"], 1)])

    def test_empty_catalog_keeps_existing_rows_and_upserts_healthy_addition(self):
        self.sync.once()

        def rows():
            sections = self.get("/library/sections")["MediaContainer"]["Directory"]
            result = []
            for section in sections:
                entries = self.get("/library/sections/%s/all" % section["key"])["MediaContainer"].get("Metadata") or []
                for entry in entries:
                    detail = self.get("/library/metadata/%s" % entry["ratingKey"])["MediaContainer"]["Metadata"][0]
                    parts = (detail.get("Media") or [{}])[0].get("Part") or [{}]
                    result.append(parts[0].get("file"))
            return set(result)

        before = rows()
        new_catalog = {"url": self.config["catalogs"][1]["url"], "title": "New Top", "library": "New Films",
                       "limit": 1, "slug": "new-top", "directory": "new-films", "type": "movie"}
        with self.sync.config_guard:
            self.sync.config["catalogs"].append(new_catalog)
        vid = "new-top-tt34384900"
        rex_stremio.write_json(self.sync.path(rex_stremio.PROBES, vid), read_json(os.path.join(FIXTURES, "probe-top-films-tt34384900.json")))
        dmm_url = "/dmm/catalog/movie/casted-movies.json"
        original = STREMIO[dmm_url]
        STREMIO[dmm_url] = dict(original, metas=[])
        self.addCleanup(STREMIO.__setitem__, dmm_url, original)

        self.sync.once()
        after = rows()
        self.assertTrue(before.issubset(after))
        self.assertIn("/mnt/rex-stremio/new-films/new-top/new-top-tt34384900.m3u8", after)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
