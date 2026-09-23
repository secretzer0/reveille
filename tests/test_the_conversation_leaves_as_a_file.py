"""EXPORT: the conversation leaves as a file, and it is the one on screen.

The bus UI filters in the BROWSER -- selected agents, FROM or TO, a text box,
history mode's own result set -- so the rows the feed shows are the only honest
definition of "this conversation". The client therefore names its rows and the
server renders exactly those, checking each against the caller's rooms. What is
gated here is that trusting the client's list is SAFE, that the file is
readable without a network, and that other people's text cannot become script
in a page opened from a mail client.
"""
import io
import pathlib
import sys
import zipfile


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from reveille import daemon, store                                  # noqa: E402

from test_store import fixture                                      # noqa: E402


def _room_and_people(c):
    admin = store.create_user(c, "boss", "boss-password")
    room = store.create_room(c, admin["id"], "Room")
    tok = store.create_token(c, admin["id"], "ana", agent_name="ana", create=True)
    store.assign_room(c, tok["id"], room["id"], admin["id"])
    store.join(c, "ana", "ana", room["id"], tok["id"])
    return admin, room, tok


def test_only_the_named_messages_and_only_from_your_rooms():
    """The list is the client's account of what it is showing; the ROOM CHECK
    is what makes believing it safe. An id from elsewhere is dropped, not
    refused -- one stale row must not cost the whole export."""
    c, *_ = fixture()
    admin, room, tok = _room_and_people(c)
    other_owner = store.create_user(c, "stranger", "stranger-password")
    other = store.create_room(c, other_owner["id"], "Elsewhere")
    otok = store.create_token(c, other_owner["id"], "bob", agent_name="bob", create=True)
    store.assign_room(c, otok["id"], other["id"], other_owner["id"])
    store.join(c, "bob", "bob", other["id"], otok["id"])

    mine, theirs = [], []
    for i in range(3):
        mine.append(store.send(c, f"agent:{tok['agent_id']}", "*", f"mine {i}",
                               room=room["id"])["id"])
    theirs.append(store.send(c, f"agent:{otok['agent_id']}", "*", "not yours",
                             room=other["id"])["id"])

    got = store.messages_by_ids(c, mine + theirs, [room["id"]])
    assert [m["id"] for m in got] == mine, "an id outside the caller's rooms leaked"
    assert all("not yours" not in m["body"] for m in got)

    # The order is the conversation's, not the order the client happened to ask in.
    shuffled = store.messages_by_ids(c, list(reversed(mine)), [room["id"]])
    assert [m["id"] for m in shuffled] == mine

    assert store.messages_by_ids(c, [], [room["id"]]) == []
    assert store.messages_by_ids(c, mine, []) == []


def test_the_page_is_self_contained_and_escapes_what_people_wrote():
    """AN EXPORT IS OPENED FROM A MAIL CLIENT, which is exactly where a stored
    script would want to run and exactly where no origin protects anyone. Every
    body is other people's text; it is rendered as text."""
    msgs = [{"id": 1, "from": "ana", "to": "*", "subject": "<b>subject</b>",
             "body": "<script>alert('x')</script> & <img src=x onerror=1>",
             "ts_ns": 1_700_000_000 * 10**9, "attachments": []}]
    page = daemon.export_html(msgs, {"title": "<i>t</i>", "lines": []})

    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "onerror=1>" not in page
    assert "<b>subject</b>" not in page and "&lt;b&gt;subject" in page

    # Nothing is fetched: no network, no bus, no stylesheet, no font, no script.
    for fetched in ("src=\"http", "href=\"http", "@import", "<script"):
        assert fetched not in page, f"the export reaches for {fetched!r}"


def test_the_export_wears_the_room_s_own_colours():
    """A person reads the export beside the screen it came from, so the same
    agent must be the same colour in both. Ported from the UI's hue(), 32-bit
    wrap and all; `local-*` initials are LA/LC, never LO."""
    assert daemon.export_hue("local-architect") == 59
    assert daemon.export_hue("admin") == 71
    assert daemon.export_initials("local-architect") == "LA"
    assert daemon.export_initials("local-codex-dev") == "LC"
    assert daemon.export_initials("admin") == "AD"
    assert daemon.export_initials("") == "?"
    # The 32-bit wrap is load-bearing: a name long enough to overflow must land
    # where the browser's >>>0 lands, or long-named agents change colour.
    assert daemon.export_hue("a" * 64) == daemon.export_hue("a" * 64)
    assert 0 <= daemon.export_hue("x" * 200) < 360


def test_an_attachment_the_export_left_out_is_named_in_place():
    """SAID, NOT SWALLOWED: a reader must not take an export that dropped a
    file for a conversation that never had one."""
    msgs = [{"id": 1, "from": "ana", "to": "*", "subject": "", "body": "see this",
             "ts_ns": 1_700_000_000 * 10**9,
             "attachments": [{"url": "/files/abc.png", "name": "shot.png"}]}]

    without = daemon.export_html(msgs, {"title": "t", "lines": []})
    assert "attachment not included" in without and "shot.png" in without

    withit = daemon.export_html(msgs, {"title": "t", "lines": []},
                                {"/files/abc.png": "attachments/abc.png"})
    assert "<img src=\"attachments/abc.png\"" in withit
    assert "attachment not included" not in withit


def test_a_non_image_attachment_is_a_link_not_an_image():
    msgs = [{"id": 1, "from": "ana", "to": "*", "subject": "", "body": "log",
             "ts_ns": 1_700_000_000 * 10**9,
             "attachments": [{"url": "/files/a.txt", "name": "run.log"}]}]
    page = daemon.export_html(msgs, {"title": "t", "lines": []},
                              {"/files/a.txt": "attachments/a.txt"})
    assert "<a href=\"attachments/a.txt\">run.log</a>" in page
    assert "<img" not in page


def test_the_zip_holds_one_page_and_its_attachments():
    """The shape the browser is handed: one HTML file it can email, and the
    folder that file points at."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("conversation.html", daemon.export_html(
            [{"id": 1, "from": "ana", "to": "*", "subject": "", "body": "hi",
              "ts_ns": 1_700_000_000 * 10**9, "attachments": []}],
            {"title": "t", "lines": ["<b>1</b> message"]}))
        zf.writestr("attachments/shot.png", b"\x89PNG\r\n")
    names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    assert names[0] == "conversation.html", "the page a reader opens comes first"
    assert "attachments/shot.png" in names
