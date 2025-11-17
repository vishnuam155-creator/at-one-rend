from django.urls import path
from .views import FeedbackCreateView

urlpatterns = [
    path('submit/', FeedbackCreateView.as_view(), name='submit-feedback'),
]
