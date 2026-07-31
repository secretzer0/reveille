#!/usr/bin/env python3
"""Pure-function checks for the daemon (no server). Run: uv run pytest.
The live HTTP/WS path is covered by tests/smoke_ws.py."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import __version__, daemon  # noqa: E402

# The bus page, now a flat file (ui/bus/index.html) read through the same
# fixed-table loader the server uses -- so what these tests inspect is what
# chat_http serves.
PAGE = daemon._ui_read("index.html")


def test_ui_loader_serves_only_the_fixed_table(tmp_path, monkeypatch):
    # Ruling 8635: no request-derived component may reach the filesystem, and
    # REVEILLE_UI_PATH makes the base operator-controlled -- so the table is
    # the whole reachable set, traversal included.
    for name in ("../daemon.py", "index.html/../../store.py", "app.js", ""):
        try:
            daemon._ui_read(name)
            raise AssertionError(f"loader read {name!r} -- outside the table")
        except ValueError:
            pass
    # the override is read PER CALL (live edit needs no restart), and only
    # a file of the served NAME is reachable inside it
    (tmp_path / "index.html").write_text("<body>DEV</body>")
    monkeypatch.setenv("REVEILLE_UI_PATH", str(tmp_path))
    assert daemon._ui_read("index.html") == "<body>DEV</body>"
    (tmp_path / "index.html").write_text("<body>DEV2</body>")
    assert daemon._ui_read("index.html") == "<body>DEV2</body>"
    monkeypatch.delenv("REVEILLE_UI_PATH")
    assert daemon._ui_read("index.html") == PAGE


def test_ui_override_announces_itself(monkeypatch):
    # The forward_anthropic class: an ambient env var silently changing what a
    # pinned tag serves. Chosen AND legible -- absent the override, /version
    # is the bare string every probe parses.
    import asyncio
    monkeypatch.delenv("REVEILLE_UI_PATH", raising=False)
    r = asyncio.run(daemon.version_http(None))
    assert r.body.decode() == daemon.__version__
    monkeypatch.setenv("REVEILLE_UI_PATH", "/tmp/devui")
    r = asyncio.run(daemon.version_http(None))
    assert "ui override: /tmp/devui" in r.body.decode()


def test_changes_newest_entry_is_this_version():
    """A version bump and its CHANGES entry ship together or neither ships.

    WHAT THIS DOES NOT CATCH, measured rather than assumed (architect, 8663):
    it does NOT catch the incident that prompted it. 7d8408e changed code and
    bumped NEITHER -- pyproject said 0.2.34, CHANGES' newest entry said 0.2.34,
    they agree, and this assertion passes on that exact tree. What caught that
    was server-image refusing to rebuild an existing tag, at deploy, which
    cannot be fooled.

    So this covers ONE direction: a bump whose CHANGES entry was forgotten, or
    an entry whose bump was. The other direction -- code changed with no bump
    at all -- belongs to the deploy refusal on purpose, because detecting it
    here means guessing which file changes deserve a bump, and a heuristic
    guarding a case that is already refused later is worse than nothing.

    Kept honest deliberately: an overclaiming gate is worse than a missing one,
    because the next person reads the claim and stops looking."""
    newest = next(ln for ln in daemon.CHANGES.splitlines()
                  if ln[:1].isdigit())
    assert newest.split()[0] == __version__, (
        f"CHANGES newest entry is {newest.split()[0]!r} but the package is "
        f"{__version__!r} -- bump both or neither")


def test_usage_names_the_hive_in_standing_doctrine():
    """Boot-doctrine gap (msg 8407): brief/recall/memory_add must appear in the STANDING
    USAGE text, not only in CHANGES -- a capability that lives only in the changelog is
    unreachable by an agent following instructions. USAGE is the standing protocol;
    CHANGES is the version history usage() appends after it."""
    for tool in ("brief(", "recall(", "memory_add("):
        assert tool in daemon.USAGE, f"{tool} missing from standing USAGE doctrine"
    # and in the CLAUDE.md block agents actually paste into their repos
    block = daemon.USAGE.split("CLAUDE.md block", 1)[1]
    assert "brief(" in block and "memory_add(" in block


def test_wake_url_from_http():
    assert daemon._wake_url_from("http://bigbox.local:8765") == "ws://bigbox.local:8765/wake"


def test_wake_url_strips_path():
    # the join url may carry the /mcp path; the wake url is scheme://host/wake
    assert daemon._wake_url_from("http://bigbox:8765/mcp") == "ws://bigbox:8765/wake"


def test_wake_url_https_to_wss():
    assert daemon._wake_url_from("https://host:9/mcp") == "wss://host:9/wake"


def test_wake_url_empty():
    assert daemon._wake_url_from("") == ""
    assert daemon._wake_url_from(None) == ""


def test_when_ns_relative_iso_and_bad():
    import time
    from datetime import datetime, timezone
    from reveille import store
    assert daemon._when_ns("") is None
    rel = daemon._when_ns("2h")
    assert abs(rel - (time.time_ns() - 2 * 3600 * 1_000_000_000)) < int(2e9)
    # naive ISO = UTC, never server-local: a UTC-intended window must not shift
    utc_midnight = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp() * 1e9)
    assert daemon._when_ns("2026-07-15") == utc_midnight
    assert daemon._when_ns("2026-07-15T00:00Z") == utc_midnight
    assert daemon._when_ns("2026-07-15T00:00-05:00") == utc_midnight + 5 * 3600 * 10**9
    try:
        daemon._when_ns("next tuesday")
        assert False, "should raise"
    except store.BusError:
        pass


def test_poke_gate_one_outstanding_per_agent_until_ttl():
    # The gate is keyed (token_id, name) -- per AGENT, not per agent-room. A 3-room
    # agent must take ONE ring per turn, not three; inbox() unions its rooms anyway.
    import time
    key = ("tok-a", "x")
    daemon._poke_pending.clear()
    assert daemon._poke_ok(key)                                   # nothing outstanding
    daemon._poke_pending[key] = time.time_ns()
    assert not daemon._poke_ok(key)                               # poked, unacked -> gated
    assert daemon._poke_ok(("tok-b", "x"))                        # same name, other token
    daemon._poke_pending[key] = time.time_ns() - daemon.POKE_TTL_NS - 1
    assert daemon._poke_ok(key)                                   # TTL expired -> resumes
    daemon._poke_pending.clear()


def test_notify_rings_named_agents_holding_that_room(tmp_path):
    # _notify(room, names) rings a waiter only when BOTH hold: the agent is named, and
    # its TOKEN carries that room. The room side is looked up per call, which is what
    # makes an unassign take effect without the waiter reconnecting.
    import asyncio
    from reveille import store
    db = str(tmp_path / "notify.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.create_user(conn, "owner", "pw-not-a-real-secret")
    r1 = store.create_room(conn, u["id"], "r1")
    r2 = store.create_room(conn, u["id"], "r2")
    t_alice = store.create_token(conn, u["id"], "alice")
    t_bob = store.create_token(conn, u["id"], "bob")
    t_carol = store.create_token(conn, u["id"], "carol")
    store.assign_room(conn, t_alice["id"], r1["id"], u["id"])
    store.assign_room(conn, t_bob["id"], r1["id"], u["id"])
    store.assign_room(conn, t_carol["id"], r2["id"], u["id"])   # carol is NOT in r1

    q_alice, q_bob, q_carol = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    daemon._waiters.clear()
    daemon._waiters[(t_alice["id"], "alice")] = {q_alice}
    daemon._waiters[(t_bob["id"], "bob")] = {q_bob}
    daemon._waiters[(t_carol["id"], "carol")] = {q_carol}
    prev = daemon._conn
    daemon._conn = conn
    try:
        daemon._notify(r1["id"], ["alice", "carol"])
        assert q_alice.qsize() == 1     # named, and its token holds r1
        assert q_bob.qsize() == 0       # holds r1 but was not named
        assert q_carol.qsize() == 0     # named, but its token has no r1

        # unassign r1 from alice -> the very next _notify must not ring her
        store.unassign_room(conn, t_alice["id"], r1["id"], u["id"])
        daemon._notify(r1["id"], ["alice"])
        assert q_alice.qsize() == 1     # still the one from before: no new ring
    finally:
        daemon._waiters.clear()
        daemon._conn = prev
        conn.close()


def test_upload_limits_and_quota():
    MB = 1 << 20
    prev = daemon.QUOTA_BYTES
    try:
        # Homebrew default: no quota. Nobody self-hosting inherits a hosted tier's limit.
        daemon.QUOTA_BYTES = 0
        assert daemon._upload_refusal(999 * MB, 1 * MB) is None       # used is irrelevant
        assert "too large" in daemon._upload_refusal(0, 26 * MB)      # per-file cap still applies

        # Hosted tier: a quota set in the unit file.
        daemon.QUOTA_BYTES = 100 * MB
        assert daemon._upload_refusal(0, 10 * MB) is None
        assert daemon._upload_refusal(90 * MB, 10 * MB) is None       # exactly full still fits
        assert "storage full" in daemon._upload_refusal(90 * MB, 11 * MB)
        assert "storage full" in daemon._upload_refusal(100 * MB, 1)
        # The per-file cap wins even with room to spare: one caller must not be able to
        # park 25MB+ in a single write just because the tenant is empty.
        assert "too large" in daemon._upload_refusal(0, 26 * MB)
    finally:
        daemon.QUOTA_BYTES = prev


def test_send_room_comes_from_the_query_scope():
    # The composer already picked a room (?room=, same as every other web endpoint).
    # A 2-room web user must NOT get room_required for a room they are looking at,
    # and the send -- shout included -- must land in THAT room only.
    from types import SimpleNamespace
    from reveille import store
    p = SimpleNamespace(rooms={"r1": "Private Talk", "r2": "Reveille"})

    req = SimpleNamespace(query_params={"room": "r2"})
    assert store.resolve_send_room(daemon._scope(req, p)) == "r2"

    # no room picked -> still ambiguous rather than a guess
    try:
        store.resolve_send_room(daemon._scope(SimpleNamespace(query_params={}), p))
        assert False, "should raise"
    except store.AmbiguousRoom:
        pass

    # a room out of reach is a 403, not a silent fallback
    try:
        daemon._scope(SimpleNamespace(query_params={"room": "nope"}), p)
        assert False, "should raise"
    except store.AccessError:
        pass


def test_mem_ctx_never_inherits_owner_admin(tmp_path):
    """F3: an agent is not its owner. A token minted by an instance admin must NOT
    carry the admin bit onto the MCP plane -- otherwise every fleet token bypasses
    the global doctrine gate. Admin memory powers are web-principal only (S6)."""
    from types import SimpleNamespace
    from reveille import store
    db = str(tmp_path / "f3.db")
    c = store.connect(db)
    store.migrate(c, db)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    tok = store.create_token(c, admin["id"], "fleet", agent_name="agent-x",
                             mem_tier="ratify")
    prev = daemon._conn
    daemon._conn = c
    try:
        bound, tier, adm, _ = daemon._mem_ctx(SimpleNamespace(token_id=tok["id"]))
        assert bound and tier == "ratify" and adm is False
    finally:
        daemon._conn = prev
        c.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


# ---- DES-006 U3: the broker's ONE configured nav link ------------------------
# The invariant is "the broker never depends on the launcher", not "the broker
# may never render a link": unset config must be byte-identical to before.

def test_nav_link_renders_nothing_unless_both_values_are_set():
    assert daemon.nav_link_html("", "") == ""
    assert daemon.nav_link_html("Agents", "") == ""      # path missing
    assert daemon.nav_link_html("", "/agents") == ""     # label missing
    assert daemon.nav_link_html("   ", "  ") == ""       # whitespace is unset


def test_nav_link_is_a_path_and_names_no_service():
    out = daemon.nav_link_html("Agents", "/agents")
    assert out == '<a class="navlink" href="/agents">Agents</a>'
    for word in ("launcher", "8766", "docker", "localhost"):
        assert word not in out.lower()


def test_nav_link_escapes_operator_supplied_text():
    out = daemon.nav_link_html('<script>x</script>', '/a"onmouseover="x')
    assert "<script>" not in out and 'onmouseover="x"' not in out
    assert "&lt;script&gt;" in out and "&quot;" in out


def test_unconfigured_page_renders_no_link_element():
    page = PAGE.replace("<!--NAVLINK-->", daemon.nav_link_html("", ""))
    assert '<a class="navlink"' not in page     # the CSS rule may exist; no element
    assert "<!--NAVLINK-->" not in page         # and no leftover placeholder


# ---- who wakes whom (msg 8496) ----------------------------------------------
# One table pins the entire rule: a HUMAN broadcast wakes the room, an AGENT
# broadcast does not, and unicast wakes its recipient on either plane. The
# agent-broadcast row is the ONLY thing terminating an agent-agent broadcast
# loop -- the poke gate coalesces simultaneous rings, not the ~2.4-minute
# cadence the 74-message storm actually ran at.

def _woke(plane, to, delivery):
    """The one line each plane computes, extracted so the rule is testable
    without a broker: web always wakes, MCP never wakes on broadcast."""
    if plane == "web":
        return delivery
    return delivery if to != "*" else []


def test_web_broadcast_wakes_the_whole_room():
    assert _woke("web", "*", ["a", "b", "c"]) == ["a", "b", "c"]


def test_agent_broadcast_wakes_nobody():
    assert _woke("mcp", "*", ["a", "b", "c"]) == []


def test_unicast_wakes_its_recipient_on_either_plane():
    assert _woke("web", "dev", ["dev"]) == ["dev"]
    assert _woke("mcp", "dev", ["dev"]) == ["dev"]


def test_shout_parameter_is_gone_from_the_served_surface():
    # A retired parameter that still appears in doctrine is a parameter people
    # keep sending. Clean cutover: no shout in the UI, the handler, or usage.
    assert "shout" not in PAGE
    assert "shout=true" not in daemon.USAGE
    assert "shout" not in (daemon.send_http.__doc__ or "")


def test_boot_doctrine_states_the_broadcast_rule():
    # A capability absent from the boot text does not exist (ratified lesson).
    assert "HUMAN" in daemon.USAGE and "broadcast" in daemon.USAGE
    assert "direct" in daemon.USAGE


def test_only_allowlisted_types_render_in_the_browser():
    # The dangerous set is open-ended, so the safe set is the enumerated one.
    for name in ("shot.png", "SHOT.PNG", "log.txt", "notes.md"):
        media, disp = daemon.file_headers(name)
        assert disp == "inline", name
        assert not media.startswith("application/octet-stream"), name
    for name in ("note.html", "page.htm", "logo.svg", "app.js", "data.xml",
                 "file.bin", "noext"):
        media, disp = daemon.file_headers(name)
        assert (media, disp) == ("application/octet-stream", "attachment"), name


def test_the_ui_only_tries_to_render_what_the_server_serves_inline():
    # An <img> pointed at an octet-stream download is a broken tile, and a
    # broken tile reads as a corrupt upload -- which is the exact bug this
    # slice exists to kill. So the UI's list is checked against the server's.
    m = re.search(r"const IMG_RE=/\\\.\(([^)]+)\)\$/i;", PAGE)
    assert m, "the UI's inline-image test moved; this invariant needs re-aiming"
    for alt in m.group(1).split("|"):
        for ext in ("." + alt.replace("jpe?g", "jpeg"), "." + alt.replace("?", "")):
            assert daemon.file_headers("x" + ext)[1] == "inline", ext


def test_multipart_is_detected_by_content_type_whatever_its_case():
    assert daemon._looks_multipart("multipart/form-data; boundary=x")
    assert daemon._looks_multipart("MULTIPART/FORM-DATA; boundary=x")
    assert not daemon._looks_multipart("application/octet-stream")
    assert not daemon._looks_multipart("")
    assert not daemon._looks_multipart(None)


def test_the_multipart_refusal_names_the_call_that_works():
    # A refusal that does not say what to do instead is a dead end, not a gate.
    assert "--data-binary" in daemon._MULTIPART_HELP
    assert "upload()" in daemon._MULTIPART_HELP


def test_agents_control_exists_only_where_the_launcher_was_declared():
    # A capability with no reachable path must not have a button: U6 shipped this
    # control unconditionally, so a broker with no launcher grew a button whose
    # every click ended in "agent management is unavailable".
    assert daemon.agents_nav_html("") == ""
    assert daemon.agents_nav_html("   ") == ""
    on = daemon.agents_nav_html("/agents")
    assert 'id="agentsNav"' in on
    assert 'const AGBASE="/agents"' in on


def test_agents_base_is_normalised_and_cannot_close_the_script_tag():
    # Operator-supplied text landing inside <script>: a path containing </script>
    # would end the block and run whatever follows as markup.
    assert 'const AGBASE="/agents"' in daemon.agents_nav_html("/agents/")
    evil = daemon.agents_nav_html("/x</script><script>alert(1)</script>")
    assert "</script><script>" not in evil.split("</script>")[0]
    assert "\\u003c" in evil


def test_the_page_renders_no_agents_control_when_unconfigured():
    page = PAGE.replace("<!--AGENTSNAV-->", daemon.agents_nav_html(""))
    assert 'id="agentsNav"' not in page
    assert "const AGBASE=" not in page      # nothing to click, nothing declared
    assert "<!--AGENTSNAV-->" not in page   # the placeholder itself never ships


def test_api_carries_the_status_so_callers_need_not_parse_the_message():
    # api() used to throw away r.status whenever the body carried any text, so a
    # caller wanting "did this service answer, and how" had to sniff the message
    # string for digits. That silently fails on any endpoint answering
    # {"error": ...}: the launcher's own 401 does exactly that, so the digit test
    # called a live-but-unauthenticated service "not reachable".
    api = PAGE[PAGE.index("async function api(path,opts)"):]
    api = api[:api.index("\n}")]
    assert "err.status=r.status" in api, "the status must survive the throw"


def test_the_pane_tells_a_wrong_answer_apart_from_no_answer():
    # Three distinct causes must read differently, because "not reachable" for
    # all three is what sent a reviewer after the wrong service for a whole pass
    # (msg 8589): a 404 from the WRONG service, an expired session, and a
    # launcher that genuinely is not there.
    block = PAGE[PAGE.index("U6: agents, embedded"):
                           PAGE.index("The presence poll is armed once")]
    # Assert before indexing: a bare .index() raises ValueError on the unfixed
    # head, which reads as a broken test rather than a caught defect.
    assert "agUnavailable(e.status" in block, "the pane must branch on the status"
    branch = block[block.index("agUnavailable(e.status"):]
    branch = branch[:branch.index(");") + 2]
    # Branch on the STATUS, never on the message text -- the message is prose.
    assert "e.status===401" in branch          # session expired, not "unreachable"
    assert "'the launcher returned '+e.status" in branch
    assert "e.message" in branch               # only the no-response case uses it
    assert "/^\\d+$/" not in block, "digit-sniffing the message is the old defect"
    # A status that names no address is barely better than "not reachable".
    assert "AGBASE" in branch, "say WHICH url answered"


def test_every_launcher_call_in_the_embedded_pane_is_prefixed():
    # The defect: U6 called api('/agents') and api('/profile') -- unprefixed, so
    # the proxy routed them to the BROKER, which 404s. Reachable service, wrong
    # address. Every launcher-owned path must go through lapi().
    # /profile followed the credentials block into the Account tab (operator,
    # msg 8752: those are the USER's credentials, not any agent's), so the pane
    # no longer owns that path. The rule went with it rather than lapsing: each
    # region is scanned for the paths it actually calls.
    pane = PAGE[PAGE.index("U6: agents, embedded"):
                PAGE.index("The presence poll is armed once")]
    acct = PAGE[PAGE.index("function openAccount()"):
                PAGE.index("function pruneAgent(")]
    for block, paths in ((pane, ("'/agents'", "'/agents/'", "'/rooms-mine'")),
                         (acct, ("'/profile'",))):
        # Comments in these blocks QUOTE the old wrong calls on purpose (that is
        # the explanation), so scan code lines only.
        code = "\n".join(ln for ln in block.splitlines()
                         if not ln.lstrip().startswith("//"))
        for path in paths:
            # every bare api(<path>) must be the tail of an lapi(<path>)
            assert code.count(f"api({path}") == code.count(f"lapi({path}"), path
            assert code.count(f"lapi({path}") > 0, path


def test_the_copy_the_docker_gated_smokes_assert_is_still_there():
    """Both real gates for these two pages need a docker socket, so a change to
    served copy sails past every check a dev container can run -- that is how a
    deleted "NEW AGENT" heading broke launcher-api-smoke, and how lowercasing
    one word here would have broken two gates at once.

    Mirrors the string assertions in tests/single_origin_smoke.py and
    tests/launcher_api_smoke.py. If you retire one there, retire it here.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "revlaunch", os.path.join(os.path.dirname(__file__), os.pardir,
                                  "scripts", "reveille_launch.py"))
    revlaunch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revlaunch)

    import ui_copy
    for s in ui_copy.BUS_PAGE:
        assert s in ui_copy.joined(PAGE), f"single-origin-smoke asserts {s!r}"
    ui = revlaunch._ui_read("index.html")
    for s in ui_copy.LAUNCHER_PAGE:
        assert s in ui_copy.joined(ui), f"launcher-api-smoke asserts {s!r}"
    assert ui_copy.DISCLOSURE_OPEN not in ui, \
        "the create form must not ship expanded -- that is the U7 adjacency bug"
    # U8: the roster must ship HIDDEN. Visible by default it renders a second
    # agent list beneath the chat filter list -- two selectors for one thing,
    # which DES-006 6.4 rules out -- and the diff reads identically either way.
    assert ui_copy.ROSTER_VISIBLE not in PAGE, \
        "the U8 roster must ship hidden; visible it is a second agent list"
    # U8: terminals live on ONE page. This is the operator's own constraint and
    # DES-006 6.4's, and window.open is the only way to break it -- so the gate
    # is its absence, not the presence of the tab strip. A tab strip that
    # shipped alongside a surviving window.open would look complete and would
    # still put a terminal in a browser tab the first time anything reached the
    # older path.
    for s in ui_copy.BUS_PAGE_FORBIDDEN:
        assert s not in PAGE, \
            f"the bus page must not contain {s!r} -- terminals live on one page"


def test_the_manage_rail_hides_and_reads_what_it_claims():
    """U9, from two operator screenshots (msgs 8751, 8752).

    Both defects were CSS beating markup, which is exactly the class no diff
    review catches: the JS said hide it and the rule said display, the JS said
    this is a status word and the rule said this is a 7px dot. Each assertion
    below is the property, not the wording.
    """
    # 1. [hidden] is the lowest-specificity rule there is, so #fmode's own
    #    display: silently won and the chat filter stayed on screen in manage
    #    mode -- a control that does nothing to what is in front of you.
    assert "#fmode[hidden]{display:none}" in PAGE, \
        "FROM/TO sets display, so it needs its own [hidden] rule to be hideable"
    # 2. The roster's status is a WORD; .agent .st is the presence DOT. Every
    #    geometry property the dot sets must be reset, not the ones that showed:
    #    half a reset is the same overlap one property along.
    st = PAGE[PAGE.index("#roster .st{"):]
    st = st[:st.index("}")]
    for prop in ("width:auto", "height:auto", "border:0", "border-radius:0"):
        assert prop in st, f"#roster .st must reset {prop} off the presence dot"
    # 3. A roster row is a button: Tab reaches every agent, Enter opens it.
    #    A clickable <div> looks identical in a screenshot and is unreachable.
    assert "#roster button.agent{" in PAGE, "roster rows must be real buttons"
    assert "class=\"agent'+" in PAGE and "data-rost=" in PAGE
    # 4. The well is the terminal. A management list, a create disclosure or a
    #    credentials block down there is something competing for the space the
    #    operator asked to give back to tmux -- and the credentials are the
    #    USER's, so they live in the Account tab now.
    well = PAGE[PAGE.index('<div id="agentsWell">'):PAGE.index('<div id="toasts"')]
    for gone in ('id="agList"', 'class="addAgent"', 'id="agCredClaude"'):
        assert gone not in well, f"{gone} must not be in the agents well"
    assert '<div class="pSec">CREDENTIALS</div>' in \
        PAGE[PAGE.index("function openAccount()"):], \
        "the user's credentials belong to the user, in the Account tab"
    # 5. The claude token field is dead: the browser login replaced it, and a
    #    second way to set the same credential is a second thing to keep true.
    assert "claude token (setup-token or API key)" not in PAGE


def test_the_keyboard_can_reach_the_terminal_tabs():
    """U8 follow-up: the tab strip was operable by mouse only.

    Each tab was a <span> carrying data-tab and a click handler, while the
    controls INSIDE it -- stop, edit, destroy, close -- were real buttons. So
    a keyboard user could destroy an agent's container from its tab and could
    not select that tab, which is the reachability ordered backwards. The
    comment above tabActions() claimed Tab reached them in reading order; that
    was true of the actions and false of the tab.

    Every assertion below is a PROPERTY of the served bytes, because the
    rendered DOM is not something a gate can reach (ui_copy contract, 8706).
    """
    strip = PAGE[PAGE.index("function drawTabs()"):PAGE.index("function drawNote()")]
    # 1. The thing that selects a tab is a real button. A span with a click
    #    handler renders identically in a screenshot and is unreachable.
    assert '<button type="button" class="tlab" data-tab="' in strip, \
        "the tab label must be a button -- a clickable span is mouse-only"
    #    ...and the old shape must be gone, not merely joined: a span still
    #    carrying data-tab would take the click first and nothing would look
    #    wrong. COUNTED, not quoted (architect, msg 8808): this assertion used
    #    to be a verbatim copy of the old source line, newline and indent
    #    included, so reintroducing the span with any other formatting would
    #    have gone green with the defect back -- a gate asserting over a LINE
    #    instead of over the property, which is what ui_copy.joined() exists to
    #    stop. Two is the whole set: the emitted label button, and the
    #    querySelector that finds it again for the focus restore.
    assert strip.count('data-tab="') == 2, \
        "exactly two data-tab sites: the label button and the refocus lookup"
    # 2. Which tab is current is SPOKEN, not only tinted. Same for the roster
    #    row: the left bar and the tint are the eye's channel.
    assert 'aria-current="true"' in strip, \
        "the open tab must say it is current, not only look it"
    roster = PAGE[PAGE.index("function drawRoster()"):]
    assert 'aria-current="true"' in roster[:roster.index("const groups=new Map()")], \
        "the selected roster row must say it is selected"
    # 3. Selecting a tab replaces the strip, which destroys the focused
    #    element. Without the restore, Enter on a tab drops focus to the body
    #    and the next Tab starts from the top of the page.
    assert "strip.contains(document.activeElement)" in strip and "cur.focus()" in strip, \
        "re-rendering the strip must put focus back on the current tab"
    # 4. A button inside a button is invalid markup and would be reparented by
    #    the parser, so the actions must stay siblings of the label.
    assert strip.index("class=\"tlab\"") < strip.index("+tabActions(t)+"), \
        "actions must follow the label button, never nest inside it"


def test_two_tab_strips_do_not_share_one_selector():
    """Opening Settings un-highlighted the active terminal tab.

    panel() toggled the "on" class across a document-wide ".tab" query, and
    #agTabs uses the same class name -- so every terminal tab lost its
    highlight the moment any Settings tab was opened, and got it back only
    when something else happened to repaint the strip. The class name is not
    the defect; a selector that does not name its widget is.
    """
    assert "document.querySelectorAll('.tab')" not in PAGE, \
        "a '.tab' query must name its strip -- two widgets use that class"
    assert PAGE.count("$('panTabs').querySelectorAll('.tab')") == 2, \
        "both the paint and the binding must be scoped to the panel's strip"


def test_transient_answers_reach_a_screen_reader():
    """A toast is the page's whole answer channel for a refused driver grant,
    an unreachable launcher, a copied token -- and it removes itself after
    five seconds. Painted without a live region, it is an answer a screen
    reader is never given and cannot go back for."""
    assert '<div id="toasts" role="status" aria-live="polite">' in PAGE, \
        "the toast host is the page's live region; without it toasts are silent"


def test_esc_is_safe_where_it_is_actually_used():
    """esc() escaped &, < and > and left both quotes alone.

    Correct for a text position and wrong for the 23 places this page
    interpolates into an attribute VALUE -- one double quote closes the
    attribute early and the rest of the string is parsed as markup. Nothing
    reachable carried a quote (ROLE_RE in the launcher, NAME_RE in the store
    both forbid it), so this was a constraint held two services away from the
    line depending on it, silent the day either widened. Architect's finding,
    msg 8808, older than the branch that surfaced it.

    Asserted as the PROPERTY of the escaper, not as a survey of its callers:
    a survey goes stale the next time someone interpolates a name.
    """
    fn = PAGE[PAGE.index("function esc(s)"):]
    fn = fn[:fn.index("\n\n")]
    for pair in ('/"/g', "&quot;", "/'/g", "&#39;"):
        assert pair in fn, f"esc() must escape {pair} -- it is used in attributes"
    # The order is load-bearing: textContent/innerHTML escapes & FIRST, so the
    # entities added afterwards cannot be double-escaped into &amp;quot;.
    assert fn.index("d.innerHTML") < fn.index("&quot;"), \
        "quote escaping must follow the innerHTML round-trip, never precede it"


def test_every_url_this_page_builds_is_checked_not_just_escaped():
    """esc() makes a string safe to SIT in an attribute and says nothing about
    what the browser DOES with it: href and src act on the value.

    THIS GATE USED TO ASSERT A FALSE PROPERTY. It said "the page builds exactly
    one href from a value it did not author" and enforced that by counting one
    SPELLING -- the esc() form -- so it could not see the three attachment
    sites written with a bare variable, which is where the stored-XSS path ran
    (architect, msg 8816). A gate that names a property and then counts one way
    of writing it is the quoted-line failure one level up, and the assertion's
    own words are what made the gap invisible.

    So: enumerate EVERY built href/src/data-src by its interpolated expression
    and pin the whole set. A new one in any spelling changes the set and fails
    here, which is the only way this stays true as the page grows.
    """
    sites = {}
    for expr in re.findall(r'(?:href|src|data-src)="\'\+([A-Za-z_]+)', PAGE):
        sites[expr] = sites.get(expr, 0) + 1
    assert sites == {"esc": 1, "safe": 4, "tile": 1, "u": 1}, \
        f"a URL interpolation appeared or moved: {sites} -- every one needs a check"
    # AND THE SINKS THAT ARE NOT BUILT STRINGS. The regex above sees only
    # concatenation, so a URL set by PROPERTY ASSIGNMENT was invisible to it --
    # while the docstring said "every". That is this gate's own failure mode
    # repeating one level in (architect, msg 8823): el.src=t.url on the
    # terminal iframe was safe only because of a concatenation two hundred
    # lines away, stated nowhere and gated nowhere.
    assigned = dict()
    for expr in re.findall(r'\.(?:src|href)\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)', PAGE):
        assigned[expr] = assigned.get(expr, 0) + 1
    assert assigned == {"frameSrc": 1}, \
        f"a URL property assignment appeared: {assigned} -- route it through a check"
    assert "const PATH_URL_RE=/^\\/[^/\\\\]/;" in PAGE, \
        "an assigned URL must be site-relative: // leaves the origin, \\ is the same trick"
    assert "function frameSrc(u){return PATH_URL_RE.test(u||'')?u:'about:blank';}" in PAGE, \
        "a URL that fails the path check must land on about:blank, never on itself"
    # Each name above, and the check that makes it safe. attUrl is the shape
    # check: /files/ paths are same-origin, so a scheme allowlist would prove
    # nothing there -- what matters is that the broker minted it.
    # The accept-set mirrors the server's sanitiser exactly (senior-dev, msg
    # 8824) rather than being a second opinion about the same fact: leading
    # character not a dot, so "." and ".." are refused here instead of relying
    # on the server to 404 them.
    assert "const FILE_URL_RE=/^\\/files\\/[A-Za-z0-9_-][A-Za-z0-9._-]*$/;" in PAGE, \
        "attachment urls must be pinned to the shape /upload actually mints"
    assert "function attUrl(u){return FILE_URL_RE.test(u||'')?esc(u+qs()):'';}" in PAGE, \
        "attUrl must both shape-check AND escape -- either alone is not enough"
    for name in ("const safe=attUrl(a.url);", "const tile=attUrl(a.url);"):
        assert name in PAGE, f"{name} -- the raw url must never reach an attribute"
    # A refused attachment renders as TEXT, in place. Dropping it silently would
    # read as a message that never had one.
    assert 'return \'<span class="attlink bad"' in PAGE, \
        "an attachment url that fails the check is shown as text, not swallowed"
    # The markdown renderer's link target is foreign too: escaped already, but
    # escaping says nothing about the scheme.
    md = PAGE[PAGE.index("function mdToHtml"):]
    assert "/^(https?:\\/\\/|\\/)/i.test(u)" in md[:md.index("let out=")], \
        "a markdown link target must be scheme-checked before it becomes an href"
    # And the login URL, which is where this gate started.
    sec = PAGE[PAGE.index("async function refreshLoginSection"):]
    assert "/^https:\\/\\//i.test(" in sec, \
        "the login URL must be scheme-checked, not merely escaped"
    assert "will not open: '+esc(st.url)" in sec, \
        "a rejected URL must still be shown, as text"


def test_a_humans_presence_is_their_open_tab():
    """Operator report 2026-07-30: a web user stayed 'active' in a room after
    switching rooms or signing out -- their live flag came from a member row
    the presence poll touched, and the poll only touches the room being
    VIEWED, so the old room kept a stale timestamp for the whole 40-minute
    window. Humans are now computed from the feed; AGENTS are deliberately
    left on the heartbeat, because an agent's absence is a state to see."""
    agents = [
        {"name": "ana", "tag": "web:ana", "room": "r1", "live": True},
        {"name": "ana", "tag": "web:ana", "room": "r2", "live": True},
        {"name": "ui-dev", "tag": "ui-dev", "room": "r1", "live": True},
        {"name": "gone-dev", "tag": "gone-dev", "room": "r1", "live": False},
    ]
    saved = dict(daemon._feed)
    try:
        daemon._feed.clear()
        daemon._feed["q1"] = ("r1", "ana")       # one tab, on r1 only
        daemon._human_live(agents)
        assert agents[0]["live"] is True, "the room she is watching"
        assert agents[1]["live"] is False, \
            "she switched away from r2 -- she must not read as present there"
        assert agents[2]["live"] is True, "an AGENT keeps its heartbeat liveness"
        assert agents[3]["live"] is False
        daemon._feed.clear()                      # signed out / closed the tab
        daemon._human_live(agents)
        assert agents[0]["live"] is False and agents[1]["live"] is False
        assert agents[2]["live"] is True, "signing a human out must not touch agents"
    finally:
        daemon._feed.clear()
        daemon._feed.update(saved)


# ---- the erase control: reachable, and honest about what it keeps ------------
# pruneAgent() shipped as a fully-written confirm dialog that NOTHING called --
# store function, HTTP route, typed-name confirm, all present, no control. An
# unreachable capability is indistinguishable from one that was never built,
# which is how the operator came to ask for a feature the code already had.

def test_the_erase_control_is_reachable_from_the_page():
    # 1. A real button, so the keyboard reaches it: Tab into the Rooms panel,
    #    Enter on "members" to expand the room, Tab to the agent's "erase".
    assert 'data-prune="' in PAGE, "no control carries data-prune"
    assert re.search(r'<button class="danger" data-prune="', PAGE), \
        "the erase control must be a real button -- a clickable span is mouse-only"
    # 2. Something must WIRE it. The defect was a definition with no caller, so
    #    count call sites rather than asserting the function exists: a definition
    #    is what the broken version also had.
    calls = len(re.findall(r"(?<!function )pruneAgent\(", PAGE))   # definition excluded
    assert calls >= 1, \
        f"pruneAgent is called {calls}x -- a definition with no caller is the defect"
    # Over the STATEMENT, never the line: this source wraps, and a line-granular
    # read passes on broken code simply by not seeing the half that matters.
    stmt = PAGE[PAGE.index("[data-prune]"):]
    stmt = stmt[:stmt.index(";") + 1]
    assert "pruneAgent(" in stmt, f"the [data-prune] handler does not call it: {stmt!r}"
    # 3. The dialog cannot open without the footprint: a confirm that silently
    #    drops the cost when the read fails is the dialog this replaced.
    start = PAGE.index("async function pruneAgent(")
    fn = PAGE[start:start + PAGE[start:].index("\n}\n")]
    assert fn.index("return;") < fn.index("confirming={kind:'prune'"), \
        "pruneAgent must bail before opening the dialog when the footprint read fails"


def test_the_erase_dialog_states_the_hive_it_will_keep():
    """Extracted from the served page and RUN, not re-implemented: a copy drifts,
    and this is the method the behind-predicate gate already uses.

    The property: a dialog that lists only what it deletes implies the rest went
    too, and the rest -- the agent's own lessons, and other agents' facts
    distilled from its messages -- is the half that keeps being served at boot.
    """
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not on PATH -- served-JS gates need it")
    lines = PAGE.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("function pruneAsk("))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    src = "\n".join(lines[start:end + 1])
    prog = """
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));
function askRow(title,msg,body){return title+'\\n'+msg+'\\n'+body;}
""" + src + """
const out = pruneAsk({id:'devops', foot:{counts:{authored:2,citing:1},
  authored:[{kind:'lesson',slug:'his-rule',fact:'obey this'},
            {kind:'decision',slug:null,fact:'<img src=x onerror=alert(1)>'}],
  citing:[{kind:'decision',slug:null,fact:'the fact he taught'}]}}, 'Reveille');
const need = ['KEPT: 2 memories it wrote', 'KEPT: 1 facts distilled',
              'his-rule', 'obey this', 'the fact he taught',
              'Type the agent name to confirm'];
for (const s of need) if (!out.includes(s)) throw new Error('dialog omits: ' + s);
// A hostile fact is agent-authored text landing in the operator's privileged
// session: escaped at render, exactly like every other memory string.
if (out.includes('<img src=x')) throw new Error('fact interpolated raw into the dialog');
if (!out.includes('&lt;img src=x')) throw new Error('fact not escaped');
// The empty case must still say the two headings, or a zero footprint reads as
// "nothing was kept" only because the section vanished.
const none = pruneAsk({id:'nobody', foot:{counts:{authored:0,citing:0},
  authored:[], citing:[]}}, 'Reveille');
for (const s of ['KEPT: 0 memories it wrote', 'nothing it authored is live here',
                 'no live fact cites its messages'])
  if (!none.includes(s)) throw new Error('empty footprint omits: ' + s);
console.log('ok');
"""
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    assert "ok" in res.stdout


def test_cancel_is_not_gated_on_the_state_it_recovers_from():
    """Architect ruling 8867. The cancel button rendered only while the page
    believed a login was pending; the failure was the page believing wrong; so
    the escape hatch was hidden in the exact state that needed it.

    Asserted over the STATEMENT, not the line, and by ENUMERATING every place the
    control can be built rather than counting one spelling of it -- a second
    branch reintroducing a pending-gated cancel in any other form is what this
    has to catch.
    """
    sec = PAGE[PAGE.index("async function refreshLoginSection()"):
               PAGE.index("function pruneAgent(")]
    # 1. Exactly one place builds the control, and its condition is the CONTAINER.
    builds = re.findall(r'id="lgCancel"', sec)
    assert len(builds) == 1, \
        f"the cancel control is built in {len(builds)} places -- one, or they drift"
    stmt = sec[sec.index("const lgCancel="):]
    stmt = stmt[:stmt.index(";")]
    assert "st.container" in stmt, f"cancel is gated on something else: {stmt!r}"
    for gated in ("st.pending", "L.present", "st.relayed"):
        assert gated not in stmt, \
            f"cancel renders on {gated} -- that is the state it recovers from"
    # 2. Every branch that paints the section offers it. Four paints: logged in,
    #    awaiting-code, pending/failed, no login. A branch that omits it is a
    #    state where the container can exist and the way out cannot be reached.
    paints = re.findall(r"paint\(el,", sec)
    assert len(paints) == 4, f"{len(paints)} paints -- update this gate deliberately"
    assert sec.count("lgCancel") >= 5, \
        "some paint branch does not include the cancel control"
    # 3. The status call that reads the container must not sit inside the
    #    credential-absent branch, or the logged-in state cannot see it -- which
    #    is also the state whose missing reap started this.
    assert sec.index("const st=await lapi('/login/status')") < sec.index("if(L.present)"), \
        "status is read inside a branch -- the logged-in state must read it too"

def test_the_rail_selection_is_repainted_wherever_the_active_tab_moves():
    """Operator screenshot, msg 8864: clicking an agent in the rail opened the
    right tab and left the HIGHLIGHT on the previously selected agent, so the
    rail and the tab strip disagreed about which agent you were looking at.

    Cause: agTabOn moves in eight places, all of which call drawTabs(), and
    drawTabs repainted the note but not the roster -- only the strip's own click
    handler repainted the rail, which is why clicking a TAB looked correct and
    clicking the RAIL did not. Two channels for one selection, repainted by
    different callers.
    """
    body = PAGE[PAGE.index("function drawTabs()"):]
    body = body[:body.index("\n}\n")]
    # 1. The funnel repaints every reading of the active tab -- in its own TAIL,
    #    not merely somewhere inside it. "drawRoster() appears in drawTabs" is
    #    inert: the strip's click handler is written inside drawTabs, so the
    #    broken version satisfies it too. The tail is the part that runs on every
    #    call, whatever moved agTabOn.
    tail = body[body.index("if(refocus)"):]
    assert "drawRoster()" in tail, \
        "drawTabs does not repaint the rail on every call -- it can disagree with the strip"
    assert "drawNote()" in tail, "drawTabs stopped repainting the note"
    # 2. ...and it is the ONLY repainter here. A second caller inside a click
    #    handler is what let the two paths drift in the first place: one of them
    #    had it, the other did not, and both looked reasonable in review.
    click = body[body.index("[data-tab]"):]
    click = click[:click.index("};")]
    assert "drawRoster" not in click, \
        "the strip click repaints the rail itself -- put it in the funnel, once"
    # 3. Every place that MOVES the active tab reaches the funnel. Enumerated by
    #    pattern rather than counted, so a ninth assignment in any spelling is
    #    caught: openAgent's four returns, the mint, the mint's catch, closeTab,
    #    and the strip click.
    sites = [m.start() for m in re.finditer(r"(?<!let )agTabOn=(?!=)", PAGE)]
    assert len(sites) >= 8, f"only {len(sites)} assignment sites found -- did they move?"
    for s in sites:
        assert "drawTabs()" in PAGE[s:s + 400], \
            f"an agTabOn assignment never reaches drawTabs: {PAGE[s:s + 80]!r}"


# ---- DES-009 commit 3: the play queue ----------------------------------------

def _voice_fns():
    """vWant and vTake, extracted from the served page. Executed rather than
    re-implemented: a copy in the test drifts from the page, which is the whole
    reason the behind-predicate gate reads the page too."""
    out = []
    for name in ("function vWant(", "function vTake("):
        start = PAGE.index(name)
        out.append(PAGE[start:start + PAGE[start:].index("\n}\n") + 2])
    return "\n".join(out)


def test_the_voice_queue_plays_in_id_order_and_skips_when_it_falls_behind():
    """The product requirement is that everyone hears the same voices in the same
    ORDER, so the ordering is the thing to gate, given arrival that is not ordered.
    """
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not on PATH -- served-JS gates need it")
    prog = _voice_fns() + """
const eq=(got,want,what)=>{const g=JSON.stringify(got),w=JSON.stringify(want);
  if(g!==w)throw new Error(what+': '+g+' != '+w);};

// ORDER. Arrival 7,5,9,6 must play 5,6,7,9 -- id order, not arrival order.
let q=[{id:7},{id:5},{id:9},{id:6}], played=[];
for(let i=0;i<4;i++){const t=vTake(q,8);played.push(t.id);
  if(t.dropped)throw new Error('dropped with a queue of 4');}
eq(played,[5,6,7,9],'play order');
eq(vTake(q,8),null,'an empty queue yields nothing');

// A GAP IS NOT WAITED FOR. 5 then 9 with no 6,7,8 plays both rather than stalling:
// a message may have no audio at all, and waiting on an id that never comes is the
// stall DES-009 section 2 forbids.
q=[{id:9},{id:5}];
eq([vTake(q,8).id, vTake(q,8).id],[5,9],'a gap must not stall the queue');

// FALLING BEHIND IS VISIBLE. Past the bound, skip to the NEWEST and report how many
// went -- not the oldest, and never silently.
q=[];for(let i=1;i<=12;i++)q.push({id:i});
const t=vTake(q,8);
eq([t.id,t.dropped],[12,11],'skip to newest, count what was dropped');
eq(q.length,0,'the skipped queue is cleared, not left to replay');

// OFF ISSUES NOTHING, and nobody hears themselves. vWant is the only gate before a
// fetch, so both properties are one function.
const m={id:1,from:'architect'};
eq(vWant(m,'me',false),false,'off must queue nothing');
eq(vWant({id:1,from:'me'},'me',true),false,'a listener must not hear themselves');
eq(vWant(m,'me',true),true,'someone else, voices on');
eq(vWant({id:1},'me',true),false,'a message with no sender is not speakable');
console.log('ok');
"""
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    assert "ok" in res.stdout


def test_the_voice_toggle_defaults_off_and_advances_on_events_not_timers():
    # 1. Default off, in the markup AND in the state, or a page load speaks.
    assert 'id="voice" aria-pressed="false"' in PAGE, \
        "the toggle must ship pressed=false -- it is also the autoplay gesture"
    assert "let voiceOn=false;" in PAGE, "voiceOn must default to off"
    assert "localStorage" not in PAGE[PAGE.index("let voiceOn=false;"):
                                      PAGE.index("function clearFeed()")], \
        "a restored 'on' has no user gesture behind it and would queue into silence"
    # 2. The player is never given a src at load: preload none, no src attribute.
    assert '<audio id="vPlayer" preload="none"></audio>' in PAGE
    # 3. The queue advances on media EVENTS. A timer-driven queue stops draining in a
    #    backgrounded tab, which is exactly where audio gets left running.
    for ev in ("'ended',vDone", "'error',vDone"):
        assert ev in PAGE, f"the queue does not advance on {ev}"
    pump = PAGE[PAGE.index("function vPump()"):PAGE.index("function vDone()")]
    assert "setTimeout" not in pump and "setInterval" not in pump, \
        "vPump advances on a timer -- a backgrounded tab throttles it to a stop"
    # 4. Autoplay refusal is NAMED. Without this the queue drains into silence and
    #    reads as a broken synthesizer.
    assert "NotAllowedError" in pump, "an autoplay refusal must be visible, not silent"
    # 5. Live arrivals only: the speak call hangs off the socket's message case, not
    #    off add(), which also renders backlog.
    case = PAGE[PAGE.index("case 'message':"):]
    case = case[:case.index("\n")]
    assert "vPush(m)" in case, "voices must be fed by the live socket case"
    body = PAGE[PAGE.index("function add(m){"):]
    body = body[:body.index("\n}\n")]
    assert "vPush" not in body, \
        "add() renders the backlog too -- a late joiner would be blasted with history"


# ---- DES-008 item D: the install block shows and never runs -------------------

def _install_fns():
    """installCmds and installBlock, extracted from the served page and executed:
    a copy in the test would drift from the panel, and the panel is what a human
    pastes into a root shell."""
    out = []
    for name in ("const INIT_CMD", "function installCmds(", "function installBlock("):
        start = PAGE.index(name)
        # INIT_CMD spans two LINES since the uvx form retired (ruling 9078), so it
        # is read to its terminating semicolon rather than to the first newline --
        # a line-granular read here silently truncated the constant and took the
        # next function with it, which surfaced as "installCmds is not defined"
        # rather than as anything about the constant.
        end = PAGE[start:].index("\n}\n") + 2 if name.startswith("function") \
            else PAGE[start:].index(";\n") + 1
        out.append(PAGE[start:start + end])
    return "\n".join(out)


SENDS = ("api(", "lapi(", "fetch(", "XMLHttpRequest", "sendBeacon", "window.open")


def test_nothing_that_sends_can_ever_take_the_install_command():
    """DES-008 section 4, gated by following the STRING rather than the
    neighbourhood (architect, msg 8957).

    The first version of this gate asserted the absence of api( in the region
    holding the four helper functions -- a region that never had one and never
    plausibly grows one. The panel is RENDERED inside openTokens, which is full of
    legitimate api( calls, so the helpful commit this exists to catch ("a run
    button next to the copy button") would land outside the region and pass. The
    docstring claimed a property of the panel; the assertion measured a
    neighbourhood. Widening the region cannot work, because openTokens must call
    api. Following the value can: wherever the command is built, the only places
    it may go are the DOM and the clipboard.
    """
    # 1. No sending call may take the command as an argument, anywhere in the file.
    for send in SENDS:
        pat = re.escape(send) + r"[^;]{0,400}?installCmds\("
        hit = re.search(pat, PAGE, re.S)
        assert not hit, f"{send} is given the install command: {hit.group(0)[:120]!r}"
    # 2. ...nor may one read it back out of the DOM and send THAT, which is the
    #    same defect with the string laundered through an element id.
    for send in SENDS:
        pat = re.escape(send) + r"[^;]{0,400}?installCmd"
        hit = re.search(pat, PAGE, re.S)
        assert not hit, f"{send} is given the rendered command: {hit.group(0)[:120]!r}"
    # 3. THIS IS THE ASSERTION DOING THE REAL WORK -- do not delete it as redundant
    #    with 1 and 2 (architect, msg 8961). Those two match a sender within one
    #    statement, so the obvious dodge is a local:
    #        const cmd = installCmds(t, origin);  await api('/exec', {cmd});
    #    The semicolon ends their window and neither fires. What kills it is here: a
    #    line that only ASSIGNS is neither clipboard nor dom, so it classifies as
    #    "unknown" and fails. 1 and 2 are the cheap ones; this is the airtight one.
    #    The command reaches exactly two consumers, and a third call site is a new
    #    consumer that has to be justified deliberately rather than merely counted.
    assert PAGE.count("function installCmds(") == 1
    sites = [m.start() for m in re.finditer(r"(?<!function )installCmds\(", PAGE)]
    assert len(sites) == 2, f"installCmds has {len(sites)} call sites, want 2"
    consumers = []
    for i in sites:
        line = PAGE[:i].rsplit("\n", 1)[-1] + PAGE[i:].split("\n", 1)[0]
        consumers.append("clipboard" if "clipboard" in line else
                         "dom" if "<pre" in line or "esc(" in line else "unknown")
    assert sorted(consumers) == ["clipboard", "dom"], consumers
    # 4. The token never rides a url or an anchor: those land in history, in a
    #    referer and in an access log. Checked over the whole helper region, where
    #    the command and the secret are in scope together.
    start = PAGE.index("// ---- DES-008 item D")
    helpers = PAGE[start:PAGE.index("async function openTokens()")]
    for sink in ("href", "location.href", "window.open", "src="):
        assert sink not in helpers, f"the install helpers build a {sink}"


def test_the_install_command_carries_the_secret_once_and_says_it_is_shown_once():
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not on PATH -- served-JS gates need it")
    prog = """
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',
  '"':'&quot;',"'":'&#39;'}[c]));
const location={origin:'http://bus.local:8765'};
""" + _install_fns() + """
const mint={agent_name:'roc-api-dev', secret:'sec-ABC123', id:'t1'};
const out = installBlock(mint);
const count = (h, n) => h.split(n).length - 1;
if (count(out, 'sec-ABC123') !== 1)
  throw new Error('the secret appears ' + count(out,'sec-ABC123') + ' times, want 1');
for (const need of ['shown once', 'does not run them',
                    'export REVEILLE_AGENT_ROLE=roc-api-dev',
                    'http://bus.local:8765', 'reveille init'])
  if (!out.includes(need)) throw new Error('install block omits: ' + need);
// An UNBOUND token installs nothing: there is no agent identity to install, and
// showing an install command for one would teach a shape that cannot work.
if (installBlock({secret:'x'}) !== '')
  throw new Error('an unbound token was offered an install command');
// The name is agent-authored text landing in the operator's privileged session,
// so it is escaped like every other such string on this page.
const nasty = installBlock({agent_name:'<img src=x onerror=alert(1)>', secret:'s'});
if (nasty.includes('<img src=x')) throw new Error('the agent name was interpolated raw');
if (!nasty.includes('&lt;img src=x')) throw new Error('the agent name was not escaped');
console.log('ok');
"""
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    assert "ok" in res.stdout


def test_the_init_invocation_is_pinned_because_it_is_a_contract():
    """The command shape belongs to senior-dev's `reveille init` (DES-008 items
    A-C). It is pinned HERE so a drift fails on this branch rather than on the
    machine where somebody is pasting it into a root shell."""
    # Pinned as the exact two-line statement, backslash-n and all: the shape is
    # the contract, not just the words in it.
    assert ("const INIT_CMD = 'uv tool install --force --from "
            "git+https://github.com/secretzer0/reveille reveille\\n' +") in PAGE and \
        "'reveille init';" in PAGE, \
        "the init invocation moved -- confirm the new shape with senior-dev, then pin it"
    # The package-name form is what senior-dev corrected at 8956 and it FAILS on a
    # real machine: there is no `reveille` on any index, so the git url is the only
    # fetchable source. Pinned as an absence too, because the short form is what
    # anyone tidying this line would reach for.
    assert "--from reveille reveille" not in PAGE, \
        "the package-name form is back -- it cannot resolve, there is no published package"
    # AND uvx MUST NOT COME BACK (ruling 9078). It runs the package ephemerally from
    # a bin dir under ~/.cache/uv that wins PATH for that process, which is how a
    # Stop hook came to point into a cache. Absence, because the one-line form is
    # shorter and will look like an improvement to whoever meets it next.
    assert "uvx" not in PAGE, \
        "uvx is back in the panel -- it runs ephemerally and taught a cache-path hook"
    # All three values ride the ENVIRONMENT. A documented form that puts a
    # root-equivalent credential in argv puts it in .bash_history on every machine
    # that runs it.
    cmds = PAGE[PAGE.index("function installCmds("):]
    cmds = cmds[:cmds.index("\n}\n")]
    assert "--token" not in cmds and "--role" not in cmds, \
        "a value became a flag -- the token must never be an argument"


def test_the_panel_mints_rooms_in_one_call_rather_than_attaching_after():
    """Architect ruling 9010, from the operator's failed install: a mint that
    attaches its rooms in a SECOND call has a window where a credential exists and
    reaches nothing. The installer had that shape and its second call could never
    succeed, so every --login run produced a token that joined no room. The panel
    is the other caller of POST /tokens and had the same two-step, on a path where
    a human notices more slowly.
    """
    fn = PAGE[PAGE.index("$('mkTok').onclick"):]
    fn = fn[:fn.index("\n for(const b of")]
    # 1. The rooms ride the MINT body.
    assert "rooms:picked" in fn, "the mint does not carry its rooms"
    assert "data-newroom" in fn, "nothing collects the rooms before the mint"
    # 2. ...and no PATCH follows it. The incremental attach stays for EXISTING
    #    tokens; what must not exist is a second call on the freshly minted one.
    assert "PATCH" not in fn, "the mint is followed by a PATCH -- that is the window"
    # 3. Nothing is pre-ticked: a credential that reaches a room by default is
    #    reach nobody chose. Asserted on the chip builder, where it would be lost.
    chips = PAGE[PAGE.index("function newRoomChips("):]
    chips = chips[:chips.index("\n}\n")]
    assert "checked" not in chips, "a room is pre-ticked -- that is reach nobody chose"
    assert "the room you are in" in chips, "the current room must be named, not just first"
    # 4. A zero-room mint is legal and nearly always a mistake, so it is NAMED.
    assert "This token carries NO ROOMS" in PAGE, \
        "a token that reaches nothing must say so -- it looks healthy from every angle"
