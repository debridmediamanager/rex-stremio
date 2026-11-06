#!/usr/bin/env python3
"""ffprobe as archive.org's file nodes made it fail now and then (2026-11-06, 1 of 99 reads): the
first run per state file answers ffprobe's own message for an HTTP 5xx, the next one the probe.
FLAKY_STATE names the state file; FLAKY_ERROR overrides the message (a file ffprobe cannot read)."""
import os
import sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
if "-h" in sys.argv:  # the options it has (`lenient_hls`): not a read
    sys.exit(0)
state = os.environ["FLAKY_STATE"]
with open(state, "a") as handle:
    handle.write("run\n")
with open(state) as handle:
    runs = len(handle.readlines())
error = os.environ.get("FLAKY_ERROR")
if error or runs == 1:
    sys.stderr.write("%s: %s\n" % (sys.argv[-1], error or "Server returned 5XX Server Error reply"))
    sys.exit(1)
print(open(os.path.join(HERE, "probe-top-films-tt6262770.json")).read())
