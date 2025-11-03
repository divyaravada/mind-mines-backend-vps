# server/apps.py
from pathlib import Path
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)

class ServerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "server"
    path = str(Path(__file__).resolve().parent)

    def ready(self):
        """
        Hook up signal handlers once per process.
        Guarded by an instance flag to avoid double-wiring (e.g., in tests or autoreload).
        """
        if getattr(self, "_signals_connected", False):
            return
        try:
            from . import signals  # noqa
            signals.connect_signals()
            self._signals_connected = True
        except Exception as e:
            logger.exception("Failed to connect signals in ServerConfig.ready(): %s", e)
