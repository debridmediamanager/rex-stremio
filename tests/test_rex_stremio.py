#!/usr/bin/env python3
"""rex-stremio's own logic, its settings page, and one sync against a real rex.

The fixtures are captured: `stremio.json` is Stremio addons' real answers (DMM Cast for the RD
test account, Cinemeta, httpx, Torrentio over RD, Public Domain Movies; secrets redacted) and
`manifests.json` their manifests, all served by a fake addon host; the probes of their streams
were captured through a relay. `fake_ffprobe.py` answers from them. The end-to-end case needs a
rex binary: REX_BIN.

    REX_BIN=/path/to/rex python3 -m unittest discover -s tests
"""
import json
import os
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


def addon_host():
    """The captured addons on one local host, `/<addon>/...` as each answered."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urllib.parse.unquote(self.path)
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

    def test_serves_by_manifest(self):
        cinemeta, torrentio, dmm, httpx = (MANIFESTS["/%s/manifest.json" % name] for name in ("cinemeta", "torrentio", "dmm", "httpx"))
        self.assertTrue(rex_stremio.serves(torrentio, "stream", "series", "tt11540908:1:2"))
        self.assertTrue(rex_stremio.serves(dmm, "stream", "movie", "tt30144839"))
        self.assertTrue(rex_stremio.serves(httpx, "stream", "movie", "tt6262770"))  # types and prefixes at the top
        self.assertFalse(rex_stremio.serves(cinemeta, "stream", "movie", "tt6262770"))
        self.assertFalse(rex_stremio.serves(dmm, "stream", "other", "dmm:1"))

    def test_catalogs_listed_without_a_question(self):
        cinemeta = rex_stremio.catalogs_of(MANIFESTS["/cinemeta/manifest.json"])
        # "New" needs a genre, "Last videos" and "Calendar videos" a list of ids.
        self.assertEqual([(c["type"], c["id"]) for c in cinemeta],
                         [("movie", "top"), ("series", "top"), ("movie", "imdbRating"), ("series", "imdbRating")])
        dmm = rex_stremio.catalogs_of(MANIFESTS["/dmm/manifest.json"])
        self.assertEqual([(c["type"], c["id"]) for c in dmm], [("movie", "casted-movies"), ("series", "casted-shows")])
        self.assertEqual(rex_stremio.catalogs_of(MANIFESTS["/torrentio/manifest.json"]), [])

    def test_quality_from_labels(self):
        self.assertEqual(rex_stremio.quality(TORRENTIO[0]), (2160, int(5.49 * (1 << 30))))
        self.assertEqual(rex_stremio.quality(DMM[2]), (2160, int(83.52 * (1 << 30))))
        self.assertEqual(rex_stremio.quality(DMM[1]), (0, int(83.77 * (1 << 30))))
        pdm = STREMIO["/pdm/stream/movie/tt0012349.json"]["streams"][0]
        self.assertEqual(rex_stremio.quality(pdm), (1080, int(859.37 * (1 << 20))))

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

    def test_save_config_private_and_without_derived_values(self):
        path = config_file(self, {"catalogs": [{"url": "https://a/catalog/movie/top.json", "library": "Films"}]})
        rex_stremio.save_config(path, rex_stremio.load_config(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        saved = json.load(open(path))
        self.assertEqual(saved["catalogs"], [{"url": "https://a/catalog/movie/top.json", "library": "Films", "limit": 50}])
        self.assertNotIn("relay_url", saved)

    def test_own_network(self):
        own = rex_stremio.own_network
        self.assertTrue(own(FakeRequest("127.0.0.1", "127.0.0.1:9982")))
        self.assertTrue(own(FakeRequest("192.168.1.30", "192.168.1.20:9982")))
        self.assertTrue(own(FakeRequest("100.101.1.2", "localhost:9982")))  # Tailscale
        self.assertFalse(own(FakeRequest("8.8.8.8", "192.168.1.20:9982")))
        self.assertFalse(own(FakeRequest("127.0.0.1", "evil.example:9982")))  # a name rebound to us
        self.assertFalse(own(FakeRequest("127.0.0.1", "127.0.0.1:9982", "POST", "https://evil.example")))
        self.assertTrue(own(FakeRequest("127.0.0.1", "127.0.0.1:9982", "POST", "http://127.0.0.1:9982")))

    def test_stremio_item(self):
        config = rex_stremio.load_config(config_file(self, {"catalogs": [
            {"url": "https://h/catalog/series/top.json", "title": "Top shows"},
            {"url": "https://h/catalog/movie/casted-movies.json", "title": "DMM Cast"}]}))
        shows, films = config["catalogs"]
        meta = STREMIO["/cinemeta/meta/series/tt11540908.json"]["meta"]
        video = [v for v in meta["videos"] if (v.get("season"), v.get("episode")) == (1, 2)][0]
        probe = json.load(open(os.path.join(FIXTURES, "probe-top-shows-tt11540908-1-2.json")))
        pushed = rex_stremio.stremio_item(config, shows, "top-shows-tt11540908-1-2", "tt11540908", meta, video, probe, "mkv")
        self.assertEqual((pushed["kind"], pushed["season"], pushed["episode"], pushed["ids"]), ("episode", 1, 2, {"imdb": "tt11540908"}))
        self.assertEqual(pushed["path"], "stremio-shows/top-shows/top-shows-tt11540908-1-2.mkv")
        self.assertEqual(pushed["size"], int(probe["format"]["size"]))
        self.assertEqual(pushed["display"]["show"]["title"], meta["name"])
        catalog = STREMIO["/dmm/catalog/movie/casted-movies.json"]["metas"][0]
        pushed = rex_stremio.stremio_item(config, films, "dmm-cast-tt30144839", "tt30144839", catalog, None,
                                          json.load(open(os.path.join(FIXTURES, "probe-dmm-cast-tt30144839.json"))), "ts")
        self.assertEqual((pushed["kind"], pushed["ids"], pushed["path"][-3:]), ("movie", {"imdb": "tt30144839"}, ".ts"))
        self.assertEqual(pushed["display"]["title"], "One Battle After Another")


class Resolve(unittest.TestCase):
    """The relay's choice at play: ranked, pinned to what was probed, the next when a link is dead."""

    def setUp(self):
        self.host, self.base = addon_host()
        self.addCleanup(self.host.shutdown)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        self.config = rex_stremio.load_config(config_file(self, {"cache": work, "addons": [addon(self.base, "torrentio")]}))
        self.sync = rex_stremio.Sync(self.config)
        with open(self.sync.path("info", "carrie-1-2"), "w") as handle:
            json.dump({"kind": "series", "sid": "tt11540908:1:2"}, handle)
        self.opened = []

    def opens(self, dead):
        def opens(url):
            self.opened.append(url)
            return None if url in dead else url + "#final"
        return opens

    def test_first_that_opens_is_pinned_then_kept(self):
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({TORRENTIO[0]["url"]})):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[1]["url"] + "#final")
        info = json.load(open(self.sync.path("info", "carrie-1-2")))
        self.assertEqual(info["stream"], rex_stremio.stream_key(TORRENTIO[1]))
        # An hour later the best link opens again, but the pinned one still plays first.
        self.sync.cache.clear()
        self.opened.clear()
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens(set())):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[1]["url"] + "#final")
        self.assertEqual(self.opened, [TORRENTIO[1]["url"]])

    def test_pinned_gone_falls_back(self):
        with open(self.sync.path("info", "carrie-1-2"), "w") as handle:
            json.dump({"kind": "series", "sid": "tt11540908:1:2", "stream": rex_stremio.stream_key(TORRENTIO[2])}, handle)
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({TORRENTIO[2]["url"]})):
            self.assertEqual(self.sync.resolve("carrie-1-2"), TORRENTIO[0]["url"] + "#final")
        self.assertEqual(self.opened, [TORRENTIO[2]["url"], TORRENTIO[0]["url"]])

    def test_nothing_opens(self):
        with unittest.mock.patch.object(rex_stremio, "opens", self.opens({s["url"] for s in TORRENTIO})):
            with self.assertRaises(RuntimeError):
                self.sync.resolve("carrie-1-2")


class Settings(unittest.TestCase):
    """The page, through HTTP: add addons, choose catalogs, rank streams."""

    def setUp(self):
        self.host, self.base = addon_host()
        self.addCleanup(self.host.shutdown)
        work = tempfile.mkdtemp(prefix="rex-stremio-")
        self.path = os.path.join(work, "config.json")
        rex_stremio.save_config(self.path, {"listen": "127.0.0.1:0", "cache": work})
        self.config = rex_stremio.load_config(self.path)
        self.sync = rex_stremio.Sync(self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_stremio.App(self.path, self.config, self.sync).handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
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
        saved = json.load(open(self.path))
        self.assertEqual([a["manifest"]["name"] for a in saved["addons"]], ["Cinemeta", "Torrentio"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # Rows 0 and 1 are Cinemeta's Popular films and series.
        self.post("/settings", {"take": ["0", "1"], "url0": "%s/cinemeta/catalog/movie/top.json" % self.base, "title0": "Cinemeta Popular",
                                "library0": "Films", "limit0": "10", "url1": "%s/cinemeta/catalog/series/top.json" % self.base,
                                "title1": "Cinemeta Popular", "library1": "", "limit1": "x", "max_resolution": "1080", "max_size_gb": "20",
                                "rex_url": "http://192.168.1.20:32400/"})
        saved = json.load(open(self.path))
        self.assertEqual(saved["catalogs"], [
            {"url": "%s/cinemeta/catalog/movie/top.json" % self.base, "title": "Cinemeta Popular", "library": "Films", "limit": 10},
            {"url": "%s/cinemeta/catalog/series/top.json" % self.base, "title": "Cinemeta Popular", "limit": 50}])
        self.assertEqual((saved["max_resolution"], saved["max_size_gb"], saved["rex_url"]), (1080, 20.0, "http://192.168.1.20:32400"))
        self.assertEqual([c["slug"] for c in self.config["catalogs"]], ["cinemeta-popular", "cinemeta-popular-2"])
        self.assertTrue(self.sync.wake.is_set())
        page = self.post("/addons/remove", {"at": "0"})
        self.assertNotIn("Cinemeta: Popular", page)
        self.assertEqual(json.load(open(self.path))["catalogs"], [])  # its catalogs leave with it

    def test_refuses_other_pages_and_non_addons(self):
        with self.assertRaises(urllib.error.HTTPError) as answer:
            self.post("/addons", {"url": "%s/cinemeta/manifest.json" % self.base}, origin="https://evil.example")
        self.assertEqual(answer.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as answer:
            self.post("/addons", {"url": "%s/nothing/manifest.json" % self.base})
        self.assertEqual(answer.exception.code, 400)
        self.assertEqual(json.load(open(self.path)).get("addons", []), [])


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
        self.addCleanup(self.rex.terminate)
        for _ in range(100):
            try:
                self.get("/identity")
                break
            except OSError:
                time.sleep(0.1)
        host, base = addon_host()
        self.addCleanup(host.shutdown)
        cinemeta = rex_stremio.CINEMETA
        rex_stremio.CINEMETA = base + "/cinemeta"
        self.addCleanup(setattr, rex_stremio, "CINEMETA", cinemeta)
        relay = free_port()
        path = os.path.join(self.work, "config.json")
        json.dump({"rex_url": "http://127.0.0.1:%d" % self.port, "listen": "127.0.0.1:%d" % relay, "cache": os.path.join(self.work, "cache"),
                   "ffprobe": os.path.join(HERE, "fake_ffprobe.py"),
                   "addons": [addon(base, "dmm"), addon(base, "cinemeta"), addon(base, "httpx"), addon(base, "torrentio")],
                   "catalogs": [
                       {"url": base + "/dmm/catalog/movie/casted-movies.json", "title": "DMM Cast"},
                       {"url": base + "/cinemeta/catalog/movie/top.json", "title": "Top films", "limit": 2},
                       {"url": base + "/cinemeta/catalog/series/top.json", "title": "Top shows", "limit": 3},
                       {"url": base + "/pdm/catalog/movie/publicdomainmovies.json", "title": "Public domain", "limit": 3}]},
                  open(path, "w"))
        self.config = rex_stremio.load_config(path)
        self.sync = rex_stremio.Sync(self.config)
        server = ThreadingHTTPServer(("127.0.0.1", relay), rex_stremio.App(path, self.config, self.sync).handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.relay = relay
        # The captured links are real hosts' (and their keys redacted): taken as opening, as they did.
        patch = unittest.mock.patch.object(rex_stremio, "opens", lambda url: url)
        patch.start()
        self.addCleanup(patch.stop)

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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
