"""
URL configuration for ats_resume_web project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path,include
from checker_app.views import home

urlpatterns = [
    path('', home), 
    path('admin/', admin.site.urls),
    path('', include('checker_app.urls')),
    path("auth/", include("allauth.urls")), 
    path('api/feedback/', include('feedBack.urls')),
]
# Health endpoint
from django.urls import path
from ats_resume_web.health import health
urlpatterns += [path("health", health)]


from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
urlpatterns += [
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]

from checker_app.views import GeminiSuggestView
urlpatterns += [path("api/ai/suggest/", GeminiSuggestView.as_view(), name="ai-suggest")]
