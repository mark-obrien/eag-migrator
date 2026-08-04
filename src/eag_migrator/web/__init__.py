"""Reading the v2 site over HTTP, when there is no database and no API access.

    recon    -> what is this site, and does it already expose JSON?
    capture  -> what does it call at runtime? (drives a real browser)
    harvest  -> pull it into state/staging.sqlite

The staging database is then an ordinary v2 database, so the rest of the
migrator works on it unchanged.
"""

from .fetcher import Fetcher, Response
from .staging import Staging

__all__ = ["Fetcher", "Response", "Staging"]
