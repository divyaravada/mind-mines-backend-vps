from threading import Thread
from django.db import close_old_connections
import logging

log = logging.getLogger(__name__)

def fire_and_forget(fn, *args, **kwargs):
    def _runner():
        try:
            close_old_connections()  # avoid DB connection reuse issues across threads
            fn(*args, **kwargs)
        except Exception:
            log.exception("Background task failed")
    Thread(target=_runner, daemon=True).start()
