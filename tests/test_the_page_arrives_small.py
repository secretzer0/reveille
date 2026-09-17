"""The page and the vendored assets arrive compressed, and arrive once.

MEASURED over the wire 2026-09-16 against the live broker: `/ui` answered
396226 bytes with `content-encoding: none` AND `cache-control: none` -- the
whole app re-downloaded, uncompressed, on every load. These gates pin both
halves, and the NEGATIVE is the one that matters most: compression is scoped
to four static payloads and must never become middleware, because a blanket
GZip would also wrap the tailing /audio stream.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

PLAIN = {"accept-encoding": "identity"}
ASKS = {"accept-encoding": "gzip, deflate"}


def test_the_page_is_compressed_when_the_client_asks(broker):
    plain = broker.get("/ui", headers=PLAIN)
    assert plain.status_code == 200
    assert plain.headers.get("content-encoding") is None, plain.headers
    assert "<title>" in plain.text

    gz = broker.get("/ui", headers=ASKS)
    assert gz.status_code == 200
    assert gz.headers["content-encoding"] == "gzip", gz.headers
    assert gz.headers["vary"] == "Accept-Encoding"
    # THE BYTES THAT LEAVE are what this is about, and httpx decodes the body
    # for us -- so the wire size is read off Content-Length, and the fact that
    # httpx could decode it at all is the proof the gzip was well formed.
    on_the_wire = int(gz.headers["content-length"])
    assert on_the_wire < len(plain.content) / 2, (on_the_wire, len(plain.content))
    assert gz.content == plain.content


def test_a_second_load_costs_a_304_and_no_body(broker):
    first = broker.get("/ui", headers=PLAIN)
    etag = first.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"'), etag
    # The page revalidates every load -- it changes on every deploy -- and the
    # ETag is what makes that cheap rather than 396 KB.
    assert first.headers["cache-control"] == "no-cache"

    again = broker.get("/ui", headers={**PLAIN, "if-none-match": etag})
    assert again.status_code == 304, again.status_code
    assert again.content == b""
    assert again.headers["etag"] == etag

    # A stale validator gets the page, not a 304.
    stale = broker.get("/ui", headers={**PLAIN, "if-none-match": '"not-the-page"'})
    assert stale.status_code == 200 and stale.content == first.content


def test_the_vendored_assets_are_compressed_and_cacheable(broker):
    r = broker.get("/ui/opus-decoder.js", headers=ASKS)
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip", r.headers
    assert r.headers["cache-control"] == "public, max-age=86400"
    etag = r.headers["etag"]
    assert broker.get("/ui/opus-decoder.js",
                      headers={**ASKS, "if-none-match": etag}).status_code == 304

    v = broker.get("/ui/vad/vad.bundle.min.js", headers=ASKS)
    assert v.status_code == 200 and v.headers["content-encoding"] == "gzip"
    assert v.headers["etag"] != etag          # each payload validates as itself


def test_compression_is_scoped_and_never_middleware(broker):
    """THE NEGATIVE, and the reason the helper is not GZipMiddleware.

    Starlette's GZipMiddleware has no content-type filter: mounting it would
    also wrap the tailing /audio/<id>.webm stream, putting a compressor between
    the synthesizer and an ear that is already starving on a mobile link, to
    re-compress Opus -- which does not compress. If compression is ever moved
    to middleware, these two go red first.
    """
    for path in ("/version", "/health"):
        r = broker.get(path, headers=ASKS)
        assert r.status_code == 200
        assert r.headers.get("content-encoding") is None, (path, r.headers)

    from reveille import daemon
    app = daemon.build_app()
    mounted = [type(m).__name__ for m in getattr(app, "user_middleware", [])]
    assert not any("GZip" in n for n in mounted), mounted
