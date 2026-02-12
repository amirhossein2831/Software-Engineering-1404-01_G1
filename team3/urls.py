from django.urls import path
from . import views

urlpatterns = [
    path("", views.base, name="base"),
    path("ping/", views.ping),
    path("exams/", views.exam, name="exam"),
    path("feedbacks/", views.feedback, name="feedbacks"),
    path("feedback-detail/", views.feedback_detail),

    path("speaking/<int:exam_id>/", views.speaking_exam, name="speaking_exam"),
    path("speaking/<int:user_exam_id>/pause/", views.speaking_pause, name="speaking_pause"),
    path("speaking/<int:user_exam_id>/resume/", views.speaking_resume, name="speaking_resume"),
    path("speaking/<int:user_exam_id>/upload/", views.speaking_upload, name="speaking_upload"),
    path("speaking/<int:user_exam_id>/submit/", views.speaking_submit, name="speaking_submit"),
    path("speaking/<int:user_exam_id>/exit/", views.speaking_exit, name="speaking_exit"),

    path("writing/<int:user_exam_id>/pause/", views.writing_pause, name="writing_pause"),
    path("writing/<int:user_exam_id>/resume/", views.writing_resume, name="writing_resume"),
    path("writing/<int:user_exam_id>/autosave/", views.writing_autosave, name="writing_autosave"),
    path("writing/<int:user_exam_id>/submit/", views.writing_submit, name="writing_submit"),
    path("writing/<int:user_exam_id>/exit/", views.writing_exit, name="writing_exit"),
]