"""LEX: a room's codebook for agent-to-agent messages. Pure -- no store, no I/O.

THE ROOM OWNS ITS LEX; THIS FILE ONLY STARTS ONE. SEED is a room's first table,
and every part of it -- codes, ref TYPES, and the drift RULES -- is the room's
to change by proposal and vote (store.lex_tally). Nothing here caps what a room
may grow into; lint() only refuses a table that decodes two ways.

A table is {"codes": [{code, means}], "types": {name: {chars, min, max}},
"rules": {name: int}}. A code with a <type> placeholder is a REF: `m<id>`
matches m29844 and `means` carries one {} per placeholder ("bus message {}").

A body is codes, refs, `L?` proposals and quoted free text, separated by
whitespace or ';'. Backticks (`...` or a ``` fence) carry VERBATIM artifacts --
code, errors, paths -- and are exempt. "..." carries PROSE, which is drift: at
most rules.prose_max words a message, and a phrase the room keeps quoting
(rules.hint_at times) is pushed back at its sender as a code to propose.
Glued tokens decode by the fewest-piece split (`+m29844` = `+` `m29844`), so a
code that prefixes another (`!` / `!!`) is not ambiguous. An unknown token is
refused from an agent in a LEX room; everywhere else it is an identifier,
rendered verbatim, never given a meaning.

PROPOSALS (one message may carry several; a reply `+` / `x` votes on them all):
  L?<code>="<meaning>"      add or redefine a code   L?P<n>="pull request {}"
  L?-<code>                 retire a code            L?-?L
  L?<name>=[<chars>]{m,n}   add or redefine a type   L?<ver>=[0-9.]{3,20}
  L?<rule>=<int>            set a rule               L?prose_max=6

The decode is a table lookup and a regex, never a model: the script writer is
handed this gloss so it narrates from meanings it cannot misread.
"""
import copy
import re

# A room's FIRST table. A starting point only: every code is retirable, every
# type redefinable, every rule settable -- by the room, through proposals.
SEED = {
    "codes": [
        {"code": "+", "means": "ok, done, yes"},
        {"code": "!", "means": "blocked"},
        {"code": "?", "means": "question"},
        {"code": ">", "means": "go"},
        {"code": "~", "means": "change requested"},
        {"code": "x", "means": "no, rejected"},
        {"code": "_", "means": "no reply wanted"},
        {"code": "?L", "means": "cannot decode, LEX unclear"},
        {"code": "^H", "means": "needs a human"},
        {"code": "m<id>", "means": "bus message {}"},
        {"code": "L<n>", "means": "LEX version {} adopted"},
        {"code": "a<n>", "means": "attachment {} of this message"},
    ],
    "types": {
        "id": {"chars": "0123456789", "min": 1, "max": 20},
        "n": {"chars": "0123456789", "min": 1, "max": 20},
        "sha": {"chars": "0123456789abcdef", "min": 7, "max": 40},
    },
    # prose_max: "..." words one message may carry. hint_at: times a phrase must
    # already appear in the room before a send is told to code it. verbatim_max:
    # words one message may carry inside backtick spans that contain whitespace
    # -- an error line fits, a paragraph of English does not (98868dc2).
    "rules": {"prose_max": 12, "hint_at": 3, "verbatim_max": 40},
}
RULES = frozenset(SEED["rules"])

_PH = re.compile(r"<([a-z][a-z0-9_]*)>")
_PROPOSAL = r'L\?-?[^\s;="`]+(?:=(?:"[^"]*"|[^\s;]+))?'
_TOKEN = re.compile(rf"(?P<p>{_PROPOSAL})|```[\s\S]*?```|`[^`]*`|\"[^\"]*\"|[^\s;\"`]+|[\"`]")
_TYPESPEC = re.compile(r"\[(.+)\](?:\{(\d+),(\d+)\})?")


class LexError(ValueError):
    pass


def seed():
    return copy.deepcopy(SEED)


def _expand(cls):
    """'0-9a-f.' -> the sorted characters a type admits."""
    out, i = set(), 0
    while i < len(cls):
        if i + 2 < len(cls) and cls[i + 1] == "-":
            out.update(chr(c) for c in range(ord(cls[i]), ord(cls[i + 2]) + 1))
            i += 3
        else:
            out.add(cls[i])
            i += 1
    return "".join(sorted(out))


def _ref_regex(code, types):
    parts, last = [], 0
    for m in _PH.finditer(code):
        t = types[m.group(1)]
        parts.append(re.escape(code[last:m.start()]))
        parts.append(f"([{re.escape(t['chars'])}]{{{t['min']},{t['max']}}})")
        last = m.end()
    parts.append(re.escape(code[last:]))
    return re.compile("".join(parts))


def compile_table(table):
    """(codes {code: means}, refs [(regex, means)])."""
    codes, refs = {}, []
    for e in table["codes"]:
        if _PH.search(e["code"]):
            refs.append((_ref_regex(e["code"], table["types"]), e["means"]))
        else:
            codes[e["code"]] = e["means"]
    return codes, refs


def lint(table):
    """Refuse a table that cannot be decoded one way, naming every bad entry.
    This is the ONLY limit on what a room adopts."""
    bad, seen, types = [], set(), table["types"]
    for name, t in types.items():
        if not t.get("chars") or not 1 <= t.get("min", 0) <= t.get("max", 0):
            bad.append(f"<{name}>: needs chars and 1 <= min <= max")
    for e in table["codes"]:
        code, means = e.get("code", ""), e.get("means", "")
        if not code or not means.strip():
            bad.append(f"{code!r}: code and means are both required")
            continue
        if code in seen:
            bad.append(f"{code!r}: defined twice")
        seen.add(code)
        if re.search(r"[\s;\"`=]", code) or code.startswith("L?"):
            bad.append(f"{code!r}: no whitespace, ';', '=', quotes, backticks, or leading 'L?'")
            continue
        phs = list(_PH.finditer(code))
        for m in phs:
            name = m.group(1)
            if name not in types:
                bad.append(f"{code!r}: no type <{name}>; the room has "
                           + " ".join(f"<{k}>" for k in types))
                continue
            alpha = types[name]["chars"]
            before = code[m.start() - 1] if m.start() else ""
            after = code[m.end()] if m.end() < len(code) else ""
            if before and before in alpha:
                bad.append(f"{code!r}: '{before}' is in <{name}>'s own alphabet, so the "
                           f"prefix and the payload run together; pick a prefix outside it")
            if after == "<" or (after and after in alpha):
                bad.append(f"{code!r}: what follows <{name}> runs into it")
        if phs and means.count("{}") != len(phs):
            bad.append(f"{code!r}: means needs one {{}} per placeholder ({len(phs)})")
    for k, v in table["rules"].items():
        if k not in RULES or not isinstance(v, int) or v < 0:
            bad.append(f"rule {k}={v!r}: rules are {sorted(RULES)}, each an int >= 0")
    if bad:
        raise LexError("LEX refused: " + "; ".join(bad))


def _segment(word, codes, refs):
    """Fewest-piece split of a glued word into codes/refs, or None."""
    best = {0: []}
    for i in range(len(word)):
        if i not in best:
            continue
        cands = [(c, m) for c, m in codes.items() if word.startswith(c, i)]
        for rx, means in refs:
            m = rx.match(word, i)
            if m:
                cands.append((m.group(0), means.format(*m.groups())))
        for tok, means in cands:
            j = i + len(tok)
            path = best[i] + [(tok, means)]
            if j not in best or len(path) < len(best[j]):
                best[j] = path
    return best.get(len(word))


def _parse_proposal(tok):
    """('retire', code) | ('type', name, spec) | ('rule', name, int) |
    ('code', code, means). Raises LexError when malformed."""
    body = tok[2:]
    if body.startswith("-"):
        return ("retire", body[1:])
    code, eq, val = body.partition("=")
    if not eq:
        raise LexError(f"{tok}: needs =<value>")
    val = val[1:-1] if val[:1] == '"' else val
    ph = re.fullmatch(r"<([a-z][a-z0-9_]*)>", code)
    if ph:
        spec = _TYPESPEC.fullmatch(val)
        if not spec:
            raise LexError(f"{tok}: a type is [chars]{{min,max}}")
        lo, hi = (int(spec.group(2)), int(spec.group(3))) if spec.group(2) else (1, 64)
        return ("type", ph.group(1), {"chars": _expand(spec.group(1)), "min": lo, "max": hi})
    if code in RULES:
        if not val.isdigit():
            raise LexError(f"{tok}: a rule is an int")
        return ("rule", code, int(val))
    return ("code", code, val)


def _proposal_gloss(tok):
    try:
        p = _parse_proposal(tok)
    except LexError:
        return None
    if p[0] == "retire":
        return f"proposes retiring LEX code {p[1]}"
    if p[0] == "type":
        return f"proposes LEX ref type {p[1]}"
    return f"proposes LEX {p[1]} = {p[2]}"


def decode(body, table):
    """[(token, means | None, kind)] where kind is code | quote | prose | propose |
    unknown. means is None only for an unknown token (render it verbatim)."""
    codes, refs = compile_table(table)
    out = []
    for m in _TOKEN.finditer(body or ""):
        t = m.group(0)
        if m.group("p"):
            g = _proposal_gloss(t)
            out.append((t, g, "propose" if g else "unknown"))
        elif t in ('"', "`"):
            out.append((t, None, "unknown"))           # an unterminated quote
        elif t[0] == "`":
            out.append((t, t.strip("`").strip(), "quote"))
        elif t[0] == '"':
            out.append((t, t.strip('"').strip(), "prose"))
        else:
            seg = _segment(t, codes, refs)
            if seg is None:
                out.append((t, None, "unknown"))
            else:
                out.extend((tok, means, "code") for tok, means in seg)
    return out


def proposals(body):
    """The change a message proposes, as {add, retire, types, rules}, or None."""
    ch = {"add": [], "retire": [], "types": {}, "rules": {}}
    for m in _TOKEN.finditer(body or ""):
        if not m.group("p"):
            continue
        p = _parse_proposal(m.group(0))
        if p[0] == "retire":
            ch["retire"].append(p[1])
        elif p[0] == "type":
            ch["types"][p[1]] = p[2]
        elif p[0] == "rule":
            ch["rules"][p[1]] = p[2]
        else:
            ch["add"].append({"code": p[1], "means": p[2]})
    return ch if any(ch.values()) else None


def apply(table, change):
    """A new table: `table` with `change` applied, linted whole. Order is kept
    so a table reads the same version to version."""
    retire = set(change.get("retire", ()))
    missing = retire - {e["code"] for e in table["codes"]}
    if missing:
        raise LexError(f"LEX refused: cannot retire {sorted(missing)}: not in the table")
    add = list(change.get("add", ()))
    new = {e["code"] for e in add}
    codes = [e for e in table["codes"] if e["code"] not in retire and e["code"] not in new]
    out = {"codes": codes + [{"code": e["code"], "means": e["means"]} for e in add],
           "types": {**table["types"], **change.get("types", {})},
           "rules": {**table["rules"], **change.get("rules", {})}}
    lint(out)
    return out


def rules(table):
    """The rules a table is held to: the seed's value for every rule it never
    set. A room that adopted before a rule existed is held to the seed's."""
    return {**SEED["rules"], **table["rules"]}


def challenges(body, table):
    """Does this message challenge with `?L`? A challenge is the sender's
    business alone: it goes unicast and is never voiced (98868dc2)."""
    return any(t == "?L" and k == "code" for t, _m, k in decode(body, table))


def refusal(body, table, ver):
    """None if `body` is valid LEX, else the refusal text naming the way out.

    BACKTICKS ARE FOR ARTIFACTS, NOT ENGLISH (98868dc2): 7/7 fleet reviews
    found 86-97% of agent tokens in backtick prose. A span with no whitespace
    (path, sha, id, flag) is free; every word of a span WITH whitespace, inline
    or fenced, counts toward rules.verbatim_max. Past it the artifact goes by
    upload -- referenced, never truncated, paid only by whoever fetches it."""
    toks = decode(body, table)
    unknown = [t for t, _m, k in toks if k == "unknown"]
    if unknown:
        return (f"not LEX v{ver}: {' '.join(unknown)}. Use a code from lex(), put "
                f"code/errors/paths in `...`, prose in \"...\", or propose a code: "
                f"L?<code>=\"<meaning>\"")
    rule = rules(table)
    cap = rule["prose_max"]
    words = sum(len(m.split()) for _t, m, k in toks if k == "prose")
    if words > cap:
        return (f"LEX v{ver} drift: {words} words of \"...\" prose, max {cap}. "
                f"Say it in codes and refs; propose a code for what has none: "
                f"L?<code>=\"<meaning>\"")
    cap = rule["verbatim_max"]
    words = sum(len(m.split()) for _t, m, k in toks if k == "quote" and len(m.split()) > 1)
    if words > cap:
        return (f"LEX v{ver} drift: {words} words inside backticks, verbatim_max {cap}. "
                f"Backticks carry artifacts, not English: put a path, sha or id in `...`, "
                f"say the rest in codes, and send a long log or traceback by upload "
                f"and cite it as a<n>")
    return None


def prose(body):
    """The "..." phrases of a body, normalized (lowercase, single spaces)."""
    return [" ".join(m.group(0)[1:-1].lower().split())
            for m in _TOKEN.finditer(body or "")
            if not m.group("p") and m.group(0)[:1] == '"' and len(m.group(0)) > 1]


def hints(phrases_seen, table):
    """Pushback for a sender: [(phrase, times)] already counted in the room ->
    one line per phrase at or past rules.hint_at, naming the proposal to send."""
    return [f'"{p}" x{n} in room: propose L?<code>="{p}"'
            for p, n in phrases_seen if n >= rules(table)["hint_at"]]


def gloss(body, table):
    """One line per token, `token = meaning`, for the script writer. An unknown
    token says so -- the writer reads it out verbatim and invents nothing.
    "..." prose is a FRAGMENT to restore into a spoken sentence: told "say it
    as written", the writer read `skip ok if 0010 rewrites old rows` raw."""
    lines = []
    for t, means, kind in decode(body, table):
        if kind == "prose":
            lines.append(f"{t} = fragment, restore it to a full spoken sentence, meaning intact")
        elif kind == "quote":
            lines.append(f"{t} = verbatim artifact, speak it by the mouth rules, give it no meaning")
        elif means is None:
            lines.append(f"{t} = identifier, say it verbatim, give it no meaning")
        else:
            lines.append(f"{t} = {means}")
    return "\n".join(lines)


def render(table):
    """The whole table as compact text, for boot: what join() hands every body
    so it speaks the CURRENT LEX from its first send."""
    codes = " | ".join(f"{e['code']} {e['means']}" for e in table["codes"])
    types = " ".join(f"<{k}>=[{t['chars']}]{{{t['min']},{t['max']}}}"
                     for k, t in table["types"].items())
    held = " ".join(f"{k}={v}" for k, v in rules(table).items())
    return f"codes: {codes}\ntypes: {types}\nrules: {held}"


def spoken(body, table):
    """The message as plain meanings, '; '-joined: what the voice reads when no
    writer is up. Quoted text and unknown tokens pass through as written."""
    return "; ".join(t if means is None else means for t, means, _k in decode(body, table))


if __name__ == "__main__":
    t = apply(seed(), {"add": [{"code": "P<n>", "means": "pull request {}"},
                               {"code": "@<sha>", "means": "commit {}"},
                               {"code": "!!", "means": "stop fleet"}]})
    # The defect this exists for: m29837, `L8 P880 cf18dd410 _` narrated as
    # "line eighty-eight ... commit C F one eight".
    assert spoken("L8 P880 @f18dd410 _", t) == \
        "LEX version 8 adopted; pull request 880; commit f18dd410; no reply wanted"
    assert spoken("+m29844;!!", t) == "ok, done, yes; bus message 29844; stop fleet"
    assert refusal("+ \"queued field\" P1327", t, 9) is None
    assert "cf18dd410" in (refusal("L8 cf18dd410", t, 9) or "")
    assert refusal('+ "unterminated', t, 9)
    assert "drift" in (refusal('+ "' + "word " * 13 + '"', t, 9) or "")
    assert refusal("+ `" + "Traceback " * 40 + "`", t, 9) is None   # verbatim is exempt
    try:
        apply(t, {"add": [{"code": "c<sha>", "means": "commit {}"}]})
        raise AssertionError("c<sha> must be refused")
    except LexError:
        pass
    # THE ROOM GROWS IT: a new type, a code on it, a tighter rule, a retirement.
    ch = proposals('L?<ver>=[0-9.]{3,20} L?V<ver>="version {}" L?prose_max=4 L?-?L')
    assert ch == {"add": [{"code": "V<ver>", "means": "version {}"}], "retire": ["?L"],
                  "types": {"ver": {"chars": ".0123456789", "min": 3, "max": 20}},
                  "rules": {"prose_max": 4}}, ch
    t2 = apply(t, ch)
    assert spoken("V3.3.17", t2) == "version 3.3.17"
    assert "drift" in (refusal('+ "one two three four five"', t2, 10) or "")
    assert refusal('L?qf="queued field" "why not"', t2, 10) is None
    assert refusal("L?broken", t2, 10)                       # malformed: refused
    assert "P<n> pull request {}" in render(t) and "prose_max=12" in render(t)
    assert prose('+ "Queued  Field" `x y` L?q="not prose"') == ["queued field"]
    assert hints([("queued field", 3), ("rare", 1)], t) == \
        ['"queued field" x3 in room: propose L?<code>="queued field"']
    print("ok")
