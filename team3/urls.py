from django.urls import path
from . import views

urlpatterns = [
    path("", views.base),
    path("ping/", views.ping),
    path("exams/", views.exam),
    path("feedbacks/", views.feedback),
    path("feedback-detail", views.feedback_detail),
]