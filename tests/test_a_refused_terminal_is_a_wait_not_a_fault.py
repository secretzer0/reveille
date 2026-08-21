"""A refused terminal is a wait, not a fault (operator report 13407, ruled
13408/13410).

The field case: press play, the container starts, the page dials ttyd before
it has bound 7681, the connection is REFUSED -- and attach_http returned 502
"agent terminal unreachable" on the FIRST throw with no retry. The operator's
close-tab-wait-reopen was the missing retry performed by hand. UNREACHABLE
reads as broken-or-gone; the true state was NOT-UP-YET, and those need
different words -- one is a defect, the other is a wait.

Ruled shape: retry on ConnectionRefusedError ONLY, bounded ~250ms x 12
(~3s), then give up; a timeout or resolution failure propagates on the FIRST
throw, because retrying those turns a fault into a hang. When the grace runs
out, the text NAMES THE STATE -- not listening yet, container running since
<ts>, try again. The 403 conflation stays exactly as it is (an ownership-
oracle defence), and attach_ws needs nothing: by the time the socket opens,
the page has already loaded.

Proven red on main 3436178: fetch_with_boot_grace does not exist and
attach_http gives up on the first refusal.
"""
import importlib.util
import pathlib
import urllib.error

import pytest

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()


class _Resp:
    status = 200
    headers = {"Content-Type": "text/html"}
    def read(self):
        return b"ttyd page"
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _refusal():
    return urllib.error.URLError(ConnectionRefusedError(111, "refused"))


def test_refused_then_up_yields_the_page():
    """The boot race won by waiting: two refusals while ttyd binds, then the
    page -- the operator's close-tab-and-reopen, done by the code."""
    calls = {"n": 0}
    def opener(req, timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _refusal()
        return _Resp()
    snoozes = []
    t = {"now": 0.0}
    def clock():
        return t["now"]
    def snooze(s):
        snoozes.append(s)
        t["now"] += s
    status, body, ctype = rl.fetch_with_boot_grace(
        "http://c:7681/", opener=opener, clock=clock, snooze=snooze)
    assert (status, body, ctype) == (200, b"ttyd page", "text/html")
    assert snoozes == [0.25, 0.25]


def test_the_grace_gives_up_and_is_bounded():
    """The half nobody sees until it matters (13410): a retry loop must be
    SEEN to give up. Refusals forever -> the refusal propagates once the ~3s
    grace is spent, after a bounded number of naps."""
    snoozes = []
    t = {"now": 0.0}
    def clock():
        return t["now"]
    def snooze(s):
        snoozes.append(s)
        t["now"] += s
    def opener(req, timeout):
        raise _refusal()
    with pytest.raises(urllib.error.URLError):
        rl.fetch_with_boot_grace("http://c:7681/", opener=opener,
                                 clock=clock, snooze=snooze)
    assert 0 < len(snoozes) <= 13, f"{len(snoozes)} naps -- the grace never ends"


def test_a_non_refusal_is_not_retried_into_a_hang():
    """Ruled explicitly: timeout and resolution failures propagate on the
    FIRST throw. Only a refusal is a wait."""
    for reason in (TimeoutError("timed out"), OSError("no route")):
        naps = []
        def opener(req, timeout, _r=reason):
            raise urllib.error.URLError(_r)
        with pytest.raises(urllib.error.URLError):
            rl.fetch_with_boot_grace("http://c:7681/", opener=opener,
                                     clock=lambda: 0.0,
                                     snooze=lambda s: naps.append(s))
        assert naps == [], f"retried a {type(reason).__name__} into a hang"


def test_attach_http_uses_the_grace_and_names_the_state():
    """Source gates on the serve() closure (no HTTP harness -- named in the
    ship message): the dial goes through fetch_with_boot_grace, an exhausted
    refusal names NOT-LISTENING-YET with the container's start time, a
    non-refusal keeps the unreachable wording, the 403 stays one message,
    and attach_ws is untouched."""
    body = SRC[SRC.index("async def attach_http"):SRC.index("async def attach_ws")]
    assert "fetch_with_boot_grace" in body
    assert "not listening yet" in body and "running since" in body
    assert "agent terminal unreachable" in body, (
        "the non-refusal failure keeps its own honest words")
    assert body.count("no attachable agent by that name") == 1, (
        "the 403 conflation is an ownership-oracle defence -- do not improve it")
    ws = SRC[SRC.index("async def attach_ws"):SRC.index("async def attach_ws") + 2000]
    assert "fetch_with_boot_grace" not in ws
