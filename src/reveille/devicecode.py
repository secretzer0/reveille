"""The code a human reads in BOTH places, so a link cannot sign them in for
somebody else.

Without it the flow is a link-click account takeover (architect, 12183): send
someone `/auth/cli?cli=<your state>`, they click, their live provider session
signs them in silently, and the session parked under YOUR state is a 14-day
credential that mints agents on THEIR account. Nothing they saw would have
looked wrong.

RFC 8628 answers this with the user_code: the terminal prints it, the page
shows it, and the human is the one comparison the attacker cannot forge --
they never see the code the victim's screen would need to match. So it is
derived from the state, not stored beside it: one pure function, both sides,
nothing to keep in sync.
"""
import base64
import hashlib

# base32's alphabet is A-Z2-7 -- no 0/O and no 1/I to misread aloud or retype.
ALPHABET_NOTE = "base32: no 0/O, no 1/I"


def cli_code(state):
    """XXXX-XXXX for a sign-in state. Short enough to compare at a glance;
    it is a comparison, never a secret -- the state stays the credential."""
    digest = hashlib.sha256(state.encode()).digest()
    code = base64.b32encode(digest).decode()[:8]
    return f"{code[:4]}-{code[4:]}"
