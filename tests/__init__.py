from contextlib import contextmanager

from skillmem import storage as S


@contextmanager
def as_owner():
    """A person at the terminal for the duration. pytest has no terminal, and
    storage asks owner_present() itself before approving, sealing or changing
    a sealed record (INV-02, INV-03)."""
    real = S.owner_present
    S.owner_present = lambda: True
    try:
        yield
    finally:
        S.owner_present = real


def owner_trusts(conn, slug, **kw):
    """The owner approving ``slug`` at a terminal."""
    with as_owner():
        return S.set_trust(conn, slug, trusted=kw.pop("trusted", True), **kw)
