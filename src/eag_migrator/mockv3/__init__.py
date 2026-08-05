"""A local stand-in for v3's HTTP API, for rehearsing a migration end to end.

This is NOT v3, and it never pretends to be. It is a faithful-as-we-know-it
mock built from the survey of the real API: the `/api/V1` routes, the
`{data, messages, succeeded}` envelope, UUID `key`s, the `CUST-000N` display
ids, and the reference data (pricing profiles, locations, payment terms,
users) the mapping has to point at. It exists so the whole pipeline —
transform, insert, capture the new id, resolve the next entity's foreign key,
roll back — can run for real without touching a production tenant.

What it does and does not tell you:

  * It DOES exercise the mechanics: that the mapping produces the fields you
    have told it are required, that ids come back and dependent records
    resolve against them, that a rollback removes what a run wrote.
  * It does NOT know v3's real field names or its full validation. It knows
    what the survey found. The exact write payload still has to be confirmed
    once against real v3 with `eagm api-post` — a mock cannot reject a field
    nobody told it about.

It binds to localhost, marks every page as a rehearsal target, and stores
records in its own throwaway SQLite file.
"""

from .server import (
    DEFAULT_PORT,
    MockConfig,
    is_mock_url,
    reset_store,
    serve,
)

__all__ = [
    "DEFAULT_PORT",
    "MockConfig",
    "is_mock_url",
    "reset_store",
    "serve",
]
