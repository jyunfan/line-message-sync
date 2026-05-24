"""
Poll LINE's WAL file mtime every 2 seconds.
FSEvents is blocked for sandboxed LINE containers; stat polling is reliable.
"""

import glob
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_WAL_GLOB = os.path.expanduser(
    "~/Library/Containers/jp.naver.line.mac/Data/Library/Containers"
    "/jp.naver.line/Data/db/*.edb-wal"
)
_POLL_INTERVAL = 2.0


def _find_wal() -> str | None:
    matches = glob.glob(_WAL_GLOB)
    return matches[0] if matches else None


def _poll(wal_path: str, on_change):
    last_mtime = os.stat(wal_path).st_mtime
    while True:
        time.sleep(_POLL_INTERVAL)
        try:
            mtime = os.stat(wal_path).st_mtime
        except FileNotFoundError:
            continue
        if mtime != last_mtime:
            last_mtime = mtime
            log.debug("WAL changed")
            on_change()


def start(on_change, _unused=None):
    """Poll LINE's WAL file; call on_change() whenever a message is written."""
    wal = _find_wal()
    if not wal:
        log.warning("LINE WAL file not found — is LINE running?")
        return
    t = threading.Thread(target=_poll, args=(wal, on_change), daemon=True)
    t.start()
    log.info("WAL poller started on %s", wal)
