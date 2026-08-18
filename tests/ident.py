"""Test helpers for the identity-keyed store (DES-011 s6.1(b)).

The store keys membership, receipts and sends on a PRINCIPAL (agent:<id> |
user:<id>) that the daemon derives from the credential. Tests that think in
names get these three: P() turns a name into the principal it denotes on this
database, T() the bound token behind an agent name, join() mints a bound
identity for a name (owned by the first user) and joins it to a room.
"""
from reveille import store


def P(conn, name):
    """agent:<id> for the live agent called `name`, else user:<id> for the person."""
    a = conn.execute("SELECT id FROM agents WHERE name=? AND retired_ns IS NULL",
                     (name,)).fetchone()
    if a:
        return store.agent_principal(a["id"])
    u = conn.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
    if u:
        return store.user_principal(u["id"])
    raise LookupError(f"no identity called {name!r} on this db")


def T(conn, name):
    """The id of the token bound to the live agent called `name`."""
    r = conn.execute(
        "SELECT t.id FROM tokens t JOIN agents a ON a.id=t.agent_id "
        "WHERE a.name=? AND a.retired_ns IS NULL", (name,)).fetchone()
    if not r:
        raise LookupError(f"no bound token for {name!r}")
    return r["id"]


def _owner(conn, owner_id=None):
    if owner_id:
        return owner_id
    return conn.execute("SELECT id FROM users ORDER BY created_ns, rowid LIMIT 1").fetchone()["id"]


def bind(conn, name, *rooms, owner_id=None):
    """Mint (or reuse) the bound token for agent `name`, reach `rooms`. Returns
    the token id."""
    owner_id = _owner(conn, owner_id)
    r = conn.execute(
        "SELECT t.id FROM tokens t JOIN agents a ON a.id=t.agent_id "
        "WHERE a.name=? AND a.owner_id=? AND a.retired_ns IS NULL", (name, owner_id)).fetchone()
    if r:
        tid = r["id"]
    else:
        # A live identity without a token ATTACHES (create=False, the body-swap
        # verb); no identity yet mints one (create=True).
        held = conn.execute("SELECT 1 FROM agents WHERE owner_id=? AND name=? "
                            "AND retired_ns IS NULL", (owner_id, name)).fetchone()
        tid = store.create_token(conn, owner_id, name, agent_name=name,
                                 create=not held)["id"]
    for rid in rooms:
        if not conn.execute("SELECT 1 FROM token_rooms WHERE token_id=? AND room_id=?",
                            (tid, rid)).fetchone():
            store.assign_room(conn, tid, rid, owner_id)
    return tid


def join(conn, name, room_id, tag=None, owner_id=None, **kw):
    """Join agent `name` (bound identity minted on demand) to one room; returns
    the room-name the store assigned."""
    tid = bind(conn, name, room_id, owner_id=owner_id)
    return store.join(conn, name, tag or name, room_id, tid, **kw)
