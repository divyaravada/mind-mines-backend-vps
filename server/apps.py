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
        # Use an instance-level flag; fresh AppConfig in tests shouldn't inherit prior state.
        if getattr(self, "_signals_connected", False):
            return
        try:
            from . import signals  # noqa
            signals.connect_signals()
            self._signals_connected = True
        except Exception as e:
            logger.exception("Failed to connect signals in ServerConfig.ready(): %s", e)