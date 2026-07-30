#!/usr/bin/env python3
"""S6c / 14.3: memory text is a security boundary in the ratifier's browser.

The UI is one static HTML string; facts arrive via fetch and render
client-side, so the server never interpolates a fact into markup -- the
boundary is the client render path. These tests pin that path: every
memory-text interpolation in the memory section flows through memText(),
and memText() is esc() alone -- the plain-text funnel, never the markdown
path. The data plane is checked from the other side: a fact carrying
<script> and markdown syntax is stored and recalled VERBATIM (escape on
OUTPUT, exactly once, at render). A real-browser check of the rendered DOM
runs at deploy verification; the unit suite has no DOM to consult and says
so rather than faking one.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

UI = daemon._ui_read("index.html")   # the served page, now a flat file
# The memory plane's entire render surface, memText through the end of
# openMemories -- the only place agent-authored memory text meets HTML.
MEM = UI[UI.index("function memText"):UI.index("async function openUsers")]

NASTY = '<script>alert(1)</script> **bold** <img src=x onerror=alert(2)>'


def test_memtext_is_esc_alone_and_markdown_free():
    assert "function memText(s){return esc(" in UI
    # the markdown path exists for chat; it must be unreachable from memory text
    assert "mdToHtml" not in MEM


def test_no_memory_text_field_concatenates_raw():
    # A raw '+m.fact+' (or any other agent-authored text field) concatenated
    # into an HTML string is the stored XSS 14.3 forbids. memText(m.fact) is
    # preceded by '(' not '+', so this regex finds only the unfunneled kind.
    leak = re.compile(
        r"\+\s*(?:m|det|a)\.(?:fact|symptom|root_cause|rule|detection|reason"
        r"|body|sender|author|actor|action|slug|status|kind|scope)\b")
    hits = leak.findall(MEM)
    assert not hits, f"unescaped memory-text interpolation(s): {hits}"


def test_fact_with_markup_is_stored_and_recalled_verbatim():
    # Escape-on-output means the DATA never mutates: the JSON the browser
    # fetches carries the attacker's bytes exactly, and the render funnel is
    # what keeps them inert. A server that pre-escaped (or worse, stripped)
    # would double-escape legitimate text and hide what the ratifier decides on.
    db = os.path.join(tempfile.mkdtemp(), "b.db")
    c = store.connect(db)
    store.migrate(c, db)
    admin = store.setup_first_admin(c, "op", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "R")
    tok = store.create_token(c, admin["id"], "t")
    store.assign_room(c, tok["id"], room["id"], admin["id"])
    d = store.memory_add(
        c, author="mallory", token_id=tok["id"], agent_bound=True, tier="state",
        is_admin=False, rooms={room["id"]}, owned_rooms=set(), fact=NASTY,
        kind="doctrine", scope=room["id"])
    q = store.ratify_queue(c, owned_rooms={room["id"]}, is_admin=False)
    assert q[0]["fact"] == NASTY            # verbatim on the queue
    det = store.memory_detail(c, d["id"])
    assert det["fact"] == NASTY             # verbatim on the detail
    c.close()


def test_reject_reason_and_ratify_paths_render_reasons_funneled():
    # The reason field is ratifier-authored but renders in OTHER ratifiers'
    # sessions later (decision history) -- same funnel, no exceptions.
    assert "memText(a.reason)" in MEM
    assert "memText(a.actor)" in MEM
