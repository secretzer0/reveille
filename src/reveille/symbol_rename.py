"""Safe Python symbol rename across the repo via rope.

Thin CLI wrapper around `rope.refactor.rename.Rename`. Rope walks
the project's parsed module graph to resolve references properly
(imports, attribute access, type annotations, conditional imports)
rather than dumb text-replace.

Usage:
    uv run symbol-rename OLD NEW [--root PATH] [--dry-run]

Example:
    uv run symbol-rename streamablehttp_client streamable_http_client

Pair with `ruff check --select F src tests` after to catch
string-literal references rope cannot resolve.
"""

from __future__ import annotations

import argparse
import io
import sys
import token
import tokenize
from collections.abc import Iterator
from pathlib import Path

from rope.base.project import Project
from rope.refactor.rename import Rename


def _name_token_offsets(source: str, symbol: str) -> Iterator[int]:
    """Byte offsets of every NAME token equal to ``symbol``, in file order.

    Tokenizing rather than scanning the raw text is the whole point: a text
    search with word-boundary checks also matches inside COMMENTS and
    DOCSTRINGS, and rope cannot resolve an identifier from there. It returns
    an EMPTY change set instead, which used to be reported as a successful
    rename -- the tool said "Renamed X -> Y" and changed nothing. A cross-file
    rename that silently no-ops is the exact failure this tool exists to
    prevent, so the anchor must be a real code token.
    """
    line_starts = [0]
    for line in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    for tok in tokens:
        if tok.type == token.NAME and tok.string == symbol:
            row, col = tok.start
            yield line_starts[row - 1] + col


def _occurrences(project: Project, symbol: str) -> list[tuple[object, int]]:
    """Every code-token occurrence of ``symbol``, in a STABLE file order.

    ``project.get_python_files()`` does not promise an order and does not give
    the same one twice, so an anchor taken from it is arbitrary -- and WHICH
    site anchors decides what rope renames. Anchored on a definition it renames
    the definition and every reference; anchored on an imported name it can
    rename that module's binding ALONE, leaving the definition and every other
    caller behind. That change set is non-empty, so the empty-set backstop
    below never fires and a half-applied rename reports success -- the exact
    broken-caller state this tool exists to prevent. Sorting by path at least
    makes the choice reproducible; _still_present() is what makes it safe.
    """
    found: list[tuple[object, int]] = []
    for resource in sorted(project.get_python_files(), key=lambda r: r.path):
        try:
            source = resource.read()
        except Exception:
            continue
        found.extend((resource, off) for off in _name_token_offsets(source, symbol))
    return found


def _still_present(project: Project, symbol: str) -> list[str]:
    """Files where ``symbol`` survives as a code token after the rename.

    The completeness check the anchor cannot give us: whatever rope decided to
    touch, a leftover occurrence means the rename is PARTIAL, and a partial
    rename is worse than none -- it compiles at the definition and breaks at
    the callers, which is how the field incident behind this rule shipped.
    """
    project.validate()
    return [r.path for r, _ in _occurrences(project, symbol)]


def _find_first_occurrence(project: Project, symbol: str) -> tuple[object, int] | None:
    hits = _occurrences(project, symbol)
    return hits[0] if hits else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", help="Symbol name to rename from")
    parser.add_argument("new", help="Symbol name to rename to")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root (default: current working directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the change set without writing files",
    )
    args = parser.parse_args()

    project = Project(str(args.root.resolve()))
    try:
        anchor = _find_first_occurrence(project, args.old)
        if anchor is None:
            print(f"ERROR: symbol {args.old!r} not found under {args.root}", file=sys.stderr)
            return 1
        resource, offset = anchor
        renamer = Rename(project, resource, offset)
        changes = renamer.get_changes(args.new)
        print(changes.get_description())
        # Backstop for the same hazard from the other end: whatever the anchor
        # was, an empty change set means NOTHING was renamed, and reporting
        # that as success is how a half-applied refactor ships green.
        if not changes.changes:
            print(
                f"ERROR: rope resolved no references for {args.old!r} -- nothing renamed.",
                file=sys.stderr,
            )
            return 1
        if args.dry_run:
            print()
            print("dry-run: no files written. Re-run without --dry-run to apply.")
            return 0
        project.do(changes)
        # The anchor is one site out of many and rope renames what that site
        # resolves to, so "it applied" is not "it finished". A survivor means
        # the tree is now HALF renamed -- fail loudly while the diff is still
        # in front of whoever ran this, rather than letting CI find it at the
        # callers.
        left = _still_present(project, args.old)
        if left:
            print()
            print(
                f"ERROR: {args.old!r} still present as code in: {', '.join(left)}\n"
                f"  The rename was PARTIAL -- rope resolved only the sites reachable\n"
                f"  from the anchor. Inspect the diff, `git checkout -- .`, and re-run\n"
                f"  anchored at the definition.",
                file=sys.stderr,
            )
            return 1
        print()
        print(f"Renamed {args.old} -> {args.new}.")
        print("Now run:  ruff check --select F src tests")
        print("to catch string-literal refs rope could not resolve.")
        return 0
    finally:
        project.close()


if __name__ == "__main__":
    sys.exit(main())
