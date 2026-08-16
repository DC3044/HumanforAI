"""Hold visits in memory and write them in batches, so the database can sleep.

Serving an agent costs no database at all. `/mcp`, `/llms.txt`, `/robots.txt`
and `/terms.md` are each zero queries; the register was what turned that traffic
into one write per request. On a serverless Postgres that suspends after five
minutes idle, a write every thirty seconds means it never suspends, and the
compute bill is for a database that is awake around the clock to record that
nothing much happened.

Batching converts that into a handful of wakes a day. Thirty minutes between
flushes is roughly forty-eight wakes, each costing the five-minute minimum
before suspension — about four compute-hours a day rather than twenty-four.

The trade is that visits buffered when an instance is killed are lost. That is
acceptable *here* and nowhere else in this project: the register is telemetry
that already expires on a schedule, while the inbox is the record and is written
through immediately, every time. Losing a row that says a crawler passed by is
proportionate; losing a message is not.

Development writes through immediately (`REGISTER_FLUSH_ROWS = 1` in dev.py), so
a request you just made shows up in the admin while you are looking at it.
"""

import atexit
import logging
import threading
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# Per-process, like everything else in a gunicorn worker. Two workers means two
# buffers, which is fine: they flush independently and the rows all land in the
# same table.
_lock = threading.Lock()
_pending = []
_last_flush = time.monotonic()


def _setting(name, default):
    return getattr(settings, name, default)


def _trim_locked():
    """Keep the buffer bounded when flushes are failing.

    Dropping the oldest visits beats growing without limit inside a container
    that has a memory cap and a job to do.
    """
    excess = len(_pending) - _setting("REGISTER_BUFFER_MAX", 2000)
    if excess > 0:
        del _pending[:excess]
        logger.warning(
            "Register of visits: buffer full, dropped %d oldest visit(s)", excess
        )


def _due_locked():
    if len(_pending) >= _setting("REGISTER_FLUSH_ROWS", 100):
        return True
    # An elapsed interval only matters if there is something to write; otherwise
    # a quiet site would flush an empty batch on its first request after a lull.
    if not _pending:
        return False
    return (time.monotonic() - _last_flush) >= _setting("REGISTER_FLUSH_SECONDS", 1800)


def _take_locked():
    global _last_flush
    batch = _pending[:]
    _pending.clear()
    _last_flush = time.monotonic()
    return batch


def _write(batch):
    if not batch:
        return 0

    from .models import AgentVisit

    try:
        AgentVisit.objects.bulk_create(batch)
    except Exception:
        # Put them back rather than discard: a flush failing usually means the
        # database is briefly unreachable, not that these rows are bad.
        logger.exception("Register of visits: failed to flush %d visit(s)", len(batch))
        with _lock:
            _pending[:0] = batch
            _trim_locked()
        return 0

    return len(batch)


def add(visit):
    """Queue one visit, writing the batch out if it is due.

    Returns the number of rows written, which is zero on most calls.
    """
    with _lock:
        _pending.append(visit)
        _trim_locked()
        if not _due_locked():
            return 0
        batch = _take_locked()
    # Written outside the lock: bulk_create is the slow part, and holding the
    # lock across it would serialise every other request behind the flush.
    return _write(batch)


def flush():
    """Write whatever is queued. Used at shutdown, and by the tests."""
    with _lock:
        batch = _take_locked()
    return _write(batch)


def pending():
    with _lock:
        return len(_pending)


# Cloud Run stops instances with SIGTERM, which gunicorn turns into a clean
# worker exit, which runs this. It is the difference between losing the last
# half-hour of the register on every deploy and losing nothing.
atexit.register(flush)
