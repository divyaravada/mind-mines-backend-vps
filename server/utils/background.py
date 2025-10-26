# server/utils/background.py
import threading
import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)

def fire_and_forget(func: Callable[..., Any], *args, **kwargs) -> None:
    """
    Run a callable in a daemon thread and return immediately.
    """
    t = threading.Thread(target=_safe_wrap, args=(func, args, kwargs), daemon=True)
    t.start()

def _safe_wrap(func, args, kwargs):
    try:
        func(*args, **kwargs)
    except Exception:
        logger.exception("fire_and_forget task failed: %s", func)

