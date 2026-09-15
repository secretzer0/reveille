"""What CI pushes and what a host pulls are the same string, or the release is
a rumour.

They are computed in two places that had no reason to agree: publish-images
reads the Makefile and prefixes the registry the WORKFLOW names, while the
deployment reads the Makefile and reveille_launch.py. The host-pulls cutover
moved $(REGISTRY) into AGENT_IMAGE and nothing noticed the publisher was still
prefixing on top of it -- main went red on the first push after the merge with
`invalid tag "ghcr.io/secretzer0/$(REGISTRY)/reveille-agent:0.2.40"`, because
the publish job runs on PUSH TO MAIN and no PR can reach it.

So the gate asks the publisher itself, through its own `tags` hook rather than
through a second copy of its grep: a gate that re-derives the value it is
checking cannot see the accessor go wrong.
"""
import pathlib
import subprocess

from conftest import makefile_image  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
REG = "ghcr.io/secretzer0"


def _published_refs():
    out = subprocess.run([str(ROOT / "scripts" / "publish-images"), "tags", REG],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return dict(line.split("\t") for line in out.stdout.strip().splitlines())


def test_the_agent_ref_ci_pushes_is_the_one_a_host_pulls():
    """One string, two readers. A host that pulls a tag CI never wrote gets a
    manifest error at provision time; a CI that writes a tag no host names
    publishes into nowhere."""
    assert _published_refs()["agent"] == makefile_image(ROOT)


def test_the_server_ref_ci_pushes_carries_the_declared_version():
    version = next(ln.split('"')[1] for ln in (ROOT / "pyproject.toml").read_text().splitlines()
                   if ln.startswith("version"))
    assert _published_refs()["server"] == f"{REG}/reveille-server:{version}"


def test_no_published_ref_carries_an_unexpanded_make_variable():
    """The exact shape that reached main: `$(REGISTRY)` travelling into a tag as
    four literal characters, valid to every reader until docker parsed it."""
    for name, ref in _published_refs().items():
        assert "$(" not in ref, f"{name} publishes to an unexpanded reference: {ref}"
        assert ref.count("/") == 2, f"{name} names two registries: {ref}"
