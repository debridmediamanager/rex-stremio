#!/usr/bin/env python3
"""ffprobe as the tests need it: asks the relay as the real one would (a refusal is ffprobe's
failure), then answers the captured probe of the video the relay URL names."""
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


if "-h" in sys.argv:  # the options it has: an FFmpeg 7.1+'s HLS demuxer, as the real ones
    print("  -extension_picky   <boolean>    .D......... Be picky with all extensions matching (default true)")
    sys.exit(0)
url = sys.argv[-1]
try:
    urllib.request.build_opener(Stay).open(urllib.request.Request(url, method="HEAD"), timeout=60)
except urllib.error.HTTPError as error:
    if error.code >= 400:
        sys.exit("%s: Server returned %d" % (url, error.code))
vid = re.search(r"/([\w-]+)\.\w+$", url)[1]
print(open(os.path.join(HERE, "probe-%s.json" % vid)).read())
