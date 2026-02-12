from django.urls import path
from . import views

urlpatterns = [
    path("", views.base, name="base"),
    path("ping/", views.ping),
    path("exams/", views.exam, name="exam"),
    path("feedbacks/", views.feedback, name="feedbacks"),
    path("feedback-detail/", views.feedback_detail),
    # path("check/", views.check_voice_file_exists),
    path("speaking/", views.speaking),
    path("writing/", views.writing),
    path("writing/<int:exam_id>/", views.writing_exam, name="writing_exam"),
    path("writing/<int:user_exam_id>/pause/", views.writing_pause, name="writing_pause"),
    path("writing/<int:user_exam_id>/resume/", views.writing_resume, name="writing_resume"),
    path("writing/<int:user_exam_id>/autosave/", views.writing_autosave, name="writing_autosave"),
    path("writing/<int:user_exam_id>/submit/", views.writing_submit, name="writing_submit"),
    path("writing/<int:user_exam_id>/exit/", views.writing_exit, name="writing_exit"),
]