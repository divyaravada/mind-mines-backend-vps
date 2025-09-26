from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from rest_framework_simplejwt.views import (
    TokenRefreshView,
)
from server.views import MyTokenObtainPairView
from django.views.generic import TemplateView


urlpatterns = [
    path("admin/", admin.site.urls),    
    path("api/", include("server.urls")),
    path('api/token/', MyTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += [
    re_path(r"^(?!static/|media/|admin/|api/).*$",
            TemplateView.as_view(template_name="index.html"),
            name="spa"),
]