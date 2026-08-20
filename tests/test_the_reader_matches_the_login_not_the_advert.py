r"""The pane reader returns the sign-in URL, never the banner's advert
(operator report 13384, ruled 13386/13388/13390).

The first end-to-end human run of the login flow produced a link -- the page
fix working -- to a support article with two hundred U+2594 blocks welded on.
Two defects in one regex: _LOGIN_URL_RE matched ANY claude.com URL and the
claude 2.1.237 startup banner prints "Learn more: https://support.claude.com/
en/articles/15424964-claude-fable-5-promotional-access" ABOVE the login
region, first match winning; and [^\s]+ over a newline-stripped pane welded
the banner's own underline row (U+2594, not whitespace) onto the match. A
third symptom rides the same cause: the banner URL exists from boot, so the
stage read awaiting-code with the advert before the picker was even answered.

The rule: the pattern matches THE LOGIN ENDPOINT -- host anchored bare
claude.com right after the scheme, oauth/authorize path required -- and runs
over URL CHARACTERS only, so the join (load-bearing: tmux hard-wraps the real
URL across three rows, measured tonight) cannot carry decoration. A future
pane whose path moves fails CLOSED: no url, stage starting, never an advert.

Proven red on main fff53b2 with tonight's pane replayed: the reader returns
the operator's exact garbage -- the support URL with the block tail.
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

RULE = "▔" * 200
BANNER = (
    " ▎ Fable 5 is now a standard part of your Max plan\n"
    " ▎ Learn more: https://support.claude.com/en/articles/"
    "15424964-claude-fable-5-promotional-access\n"
    + RULE + "\n")
# The wrap as the pane shows it (2026-08-20 capture): the query continues on
# two bare rows; state/challenge synthesized, shape kept.
URL_ROWS = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b"
    "-44d9-88ed-5944d1962f5e&response_type=code&scope=org%3Acreate_api_key+user%",
    "3Aprofile+user%3Ainference&code_challenge=SYNTHETIC&code_challenge_method"
    "=S256&state=SYNTHETICSTATE",
    "Q3sRLTStzo")
PANE_TONIGHT = (
    BANNER +
    "   Login\n"
    "   Browser didn't open? Use the url below to sign in (c to copy)\n\n"
    + "\n".join(URL_ROWS) + "\n\n"
    "   Paste code here if prompted >\n"
    "   Esc to cancel")


def test_the_reader_returns_the_sign_in_url_and_nothing_else():
    st = rl.parse_login_pane(PANE_TONIGHT)
    assert st["stage"] == "awaiting-code"
    assert st["url"] == "".join(URL_ROWS), (
        f"the reader returned {st['url'][:90]}... -- the operator pasted this "
        f"exact shape from their phone (13384)")
    assert "support.claude.com" not in st["url"]
    assert "▔" not in st["url"]


def test_the_banner_alone_is_not_a_flow():
    """The banner URL exists from boot; before this fix the stage read
    awaiting-code with the advert while the picker had not even rendered."""
    st = rl.parse_login_pane(BANNER + "\n   Welcome to Claude Code\n")
    assert st["stage"] != "awaiting-code", (
        "a startup banner was read as a login waiting for its code")
    assert st["url"] is None


def test_the_advert_url_never_matches_the_pattern():
    m = rl._LOGIN_URL_RE.search(
        "Learn more: https://support.claude.com/en/articles/"
        "15424964-claude-fable-5-promotional-access")
    assert m is None, f"the pattern matched the advert: {m.group(0)[:60]}"


def test_decoration_cannot_ride_the_join_even_adjacent():
    """Worst case the wrap join can produce: the URL's last row directly
    followed by the rule row, no blank between -- the charset is what stops
    the weld, not the layout's goodwill."""
    pane = ("https://claude.com/cai/oauth/authorize?code=true&state=ABC\n"
            + RULE + "\nPaste code here if prompted >")
    st = rl.parse_login_pane(pane)
    assert st["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=ABC"
    assert "▔" not in st["url"]


def test_a_moved_endpoint_fails_closed():
    """If claude ever moves the login path, the reader must hand the page
    NOTHING rather than the nearest URL-shaped thing."""
    pane = ("   Login\n"
            "https://claude.com/totally/new/login?x=1\n"
            "   Esc to cancel")
    assert rl.parse_login_pane(pane)["url"] is None


def test_fail_closed_is_not_fail_silent():
    """13390's addition, not optional: the Paste-code prompt with NO matching
    URL is ITS OWN STATE -- a vendor path move must land as a sentence the
    page can say, never as an eternal 'starting...'. Fixture: tonight's pane
    with the sign-in rows deleted, banner kept."""
    pane = (BANNER +
            "   Login\n"
            "   Browser didn't open? Use the url below to sign in (c to copy)\n\n"
            "   Paste code here if prompted >\n"
            "   Esc to cancel")
    st = rl.parse_login_pane(pane)
    assert st["stage"] == "url-missing", (
        f"stage={st['stage']!r}: a prompt with no readable URL must name "
        f"itself, not impersonate awaiting-code or starting")
    assert st["url"] is None
    # and without the banner too -- the state is about the missing URL, not
    # about what else the pane holds
    st2 = rl.parse_login_pane("   Paste code here if prompted >")
    assert st2["stage"] == "url-missing" and st2["url"] is None
