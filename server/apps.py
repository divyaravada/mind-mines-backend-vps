
from django.apps import AppConfig

class ServerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "server"

    def ready(self):
        # connect signals here (we’ll do it safely below)
        from . import signals  # noqa
        signals.connect_signals()
