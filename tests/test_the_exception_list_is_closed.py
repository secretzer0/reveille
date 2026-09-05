"""apt is the default; the non-apt installer list is CLOSED (ruled 14462,
kept through 14466: this gate guards WHICH installers exist, never which
versions they fetch -- an upstream release can never turn it red).

The policy was one `curl x | bash` away from silent growth, and we wrote a
lesson tonight about a capitalised invariant living in a comment above the
line that broke it. A new non-apt install site fails here by NAME: adding a
legitimate exception is a deliberate edit to ALLOWED in the same PR, which
is exactly the explicit act the ruling requires. Versions, tags and branches
are invisible to this test on purpose (operator 14464: recommended
installers run as published)."""
import pathlib
import re

DOCKERFILE = (pathlib.Path(__file__).resolve().parent.parent
              / "docker" / "Dockerfile")

# The closed list (operator 14461 froze 1-11; sdkman joined as #12 by ruling
# 14462). One entry per installer, keyed by a stable fingerprint of its
# install site -- never a version.
ALLOWED = {
    "node-nodesource": r"deb\.nodesource\.com/setup_",
    "go-tarball": r"go\.dev/dl/",
    "ttyd-release": r"github\.com/tsl0922/ttyd/releases/",
    "gh-release": r"(?:api\.)?github\.com/repos/cli/cli/releases"
                  r"|github\.com/cli/cli/releases/",
    "claude-npm": r"npm install -g @anthropic-ai/claude-code",
    "playwright-npm": r"npm install -g playwright",
    "chromium-playwright": r"playwright install chromium",
    "uv-installer": r"astral\.sh/uv/install\.sh",
    "caveman-clone": r"git clone https://github\.com/JuliusBrussee/caveman",
    "ponytail-clone": r"git clone https://github\.com/DietrichGebert/ponytail",
    "reveille-source": r"uv tool install /tmp/reveille",
    "sdkman-installer": r"get\.sdkman\.io",
}

# What counts as a non-apt install site. Anything a RUN line does that
# fetches-and-executes or installs outside apt.
SITE = re.compile(
    r"curl [^\n|]*\|\s*(?:bash|sh)"        # curl | bash / sh
    r"|curl [^\n]*(?:releases|/dl/)[^\n]*"  # fetched release artifacts
    r"|npm install -g [^\n]*"
    r"|git clone [^\n]*"
    r"|uv tool install [^\n]*"
    r"|playwright install [^\n]*"
    r"|pip install [^\n]*"
    r"|wget [^\n]*")


def _install_sites(text):
    """Every non-apt install site in RUN lines, comments stripped."""
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    return [m.group(0).strip() for m in SITE.finditer(code)]


def test_every_install_site_wears_an_allowed_name():
    text = DOCKERFILE.read_text()
    sites = _install_sites(text)
    assert sites, "the extractor found nothing -- its patterns rotted, fix it"
    unmatched = [s for s in sites
                 if not any(re.search(p, s) for p in ALLOWED.values())]
    assert not unmatched, (
        "NON-APT INSTALLER NOT ON THE EXCEPTION LIST -- this is exception "
        f"#{len(ALLOWED) + 1}: it needs a ruling, not a line. apt is the "
        "default (operator 14455); if this installer was ruled in, add it to "
        "ALLOWED in this test in the same PR.\n  " + "\n  ".join(unmatched))


def test_every_allowed_name_still_exists():
    """The list may only shrink deliberately too: a fingerprint with no site
    is a stale entry or a silent removal, and both deserve a look."""
    text = DOCKERFILE.read_text()
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    gone = [name for name, p in ALLOWED.items() if not re.search(p, code)]
    assert not gone, (
        "exception-list entries with no matching install site (removed "
        f"installer, or a moved URL the fingerprint no longer matches): {gone}")


def test_the_gate_is_blind_to_versions():
    """The negative that keeps this compatible with the operator's ruling:
    moving a version, tag or branch must not change what this gate sees."""
    text = DOCKERFILE.read_text()
    mutated = (text.replace("setup_24.x", "setup_99.x")
                   .replace("NODE_MAJOR=24", "NODE_MAJOR=99"))
    assert _install_sites(text) != [] \
        and len(_install_sites(mutated)) == len(_install_sites(text))
