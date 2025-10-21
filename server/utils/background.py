# server/utils/background.py
import threading
from typing import Callable, Any

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
        # Swallow exceptions to avoid crashing the main request thread
        import logging
        logging.getLogger(__name__).exception("fire_and_forget task failed: %s", func)
