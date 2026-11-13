
from django.urls import path

from .views import (
    ChristmasGiftAPIView,
    ChristmasQuoteAPIView,
)

urlpatterns = [
    path(
        "gifts/",
        ChristmasGiftAPIView.as_view(),
        name="christmas-gifts",
    ),
    path(
        "quote/",
        ChristmasQuoteAPIView.as_view(),
        name="christmas-quote",
    ),
]
