from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class ExamSection(models.TextChoices):
    LISTENING = 'listening', 'Listening'
    READING = 'reading', 'Reading'
    WRITING = 'writing', 'Writing'
    SPEAKING = 'speaking', 'Speaking'


class ExamSystem(models.TextChoices):
    IELTS = 'ielts', 'IELTS'
    TOEFL = 'toefl', 'TOEFL'
    GENERAL = 'general', 'General English'


class UserExamStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    IN_PROGRESS = 'in_progress', 'In Progress'
    SUBMITTED = 'submitted', 'Submitted'
    REVIEWED = 'reviewed', 'Reviewed'
    GRADED = 'graded', 'Graded'


class ExamPack(models.Model):
    id = models.BigAutoField(primary_key=True)
    system = models.CharField(max_length=50, choices=ExamSystem.choices)
    title = models.CharField(max_length=120)

    # Soft delete support (MySQL-safe)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'team3'
        db_table = "exam_pack"
        indexes = [
            models.Index(fields=["system"]),
            models.Index(fields=["is_deleted"]),
        ]
        constraints = [
            # Unique only among active rows => include is_deleted
            models.UniqueConstraint(
                fields=["system", "title", "is_deleted"],
                name="uq_exam_pack_system_title_activeflag",
            )
        ]

    def __str__(self):
        return f"{self.get_system_display()} - {self.title}"

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])


class Exam(models.Model):
    id = models.BigAutoField(primary_key=True, verbose_name='ID')
    section = models.CharField(max_length=50, choices=ExamSection.choices, verbose_name='Exam Section')
    system = models.CharField(max_length=50, choices=ExamSystem.choices, verbose_name='Exam System')
    exam_time_seconds = models.PositiveIntegerField(
        validators=[MinValueValidator(60), MaxValueValidator(36000)],
        verbose_name='Exam Time (seconds)'
    )

    pack = models.ForeignKey(
        ExamPack,
        on_delete=models.CASCADE,
        related_name="exams",
        null=True,
        blank=True,
        verbose_name="Exam Pack",
    )

    # Soft delete support (MySQL-safe)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name='Deleted At')

    created_at = models.DateTimeField(default=timezone.now, verbose_name='Created At')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Updated At')

    class Meta:
        app_label = 'team3'
        db_table = 'exam'
        verbose_name = 'Exam'
        verbose_name_plural = 'Exams'
        constraints = [
            # One section per pack among active rows
            models.UniqueConstraint(
                fields=['pack', 'section', 'is_deleted'],
                name='uq_exam_pack_section_activeflag',
            )
        ]
        indexes = [
            models.Index(fields=['pack', 'section']),
            models.Index(fields=['section', 'system']),
            models.Index(fields=['is_deleted']),
        ]

    def __str__(self):
        return f"{self.get_system_display()} - {self.get_section_display()} ({self.exam_time_seconds}s)"

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])


class Question(models.Model):
    id = models.BigAutoField(primary_key=True, verbose_name='ID')
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='questions', verbose_name='Exam')
    number = models.PositiveIntegerField(verbose_name='Question Number')
    description = models.TextField(verbose_name='Question Description')

    # Soft delete support (MySQL-safe)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name='Deleted At')

    created_at = models.DateTimeField(default=timezone.now, verbose_name='Created At')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Updated At')

    class Meta:
        app_label = 'team3'
        db_table = 'question'
        verbose_name = 'Question'
        verbose_name_plural = 'Questions'
        constraints = [
            # Unique only among active rows
            models.UniqueConstraint(
                fields=['exam', 'number', 'is_deleted'],
                name='uq_question_exam_number_activeflag',
            )
        ]
        indexes = [
            models.Index(fields=['exam', 'number']),
            models.Index(fields=['is_deleted']),
        ]

    def __str__(self):
        return f"Q{self.number}: {self.description[:50]}..."

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])


class Feedback(models.Model):
    id = models.BigAutoField(primary_key=True, verbose_name='ID')
    description = models.TextField(verbose_name='Feedback Description')

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name='Deleted At')

    created_at = models.DateTimeField(default=timezone.now, verbose_name='Created At')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Updated At')

    class Meta:
        app_label = 'team3'
        db_table = 'feedback'
        verbose_name = 'Feedback'
        verbose_name_plural = 'Feedbacks'
        indexes = [
            models.Index(fields=['is_deleted']),
        ]

    def __str__(self):
        return f"Feedback #{self.id}: {self.description[:50]}..."

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])


class UserExam(models.Model):
    id = models.BigAutoField(primary_key=True, verbose_name='ID')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_exams',
        verbose_name='User'
    )
    exam = models.ForeignKey(
        Exam,
        on_delete=models.CASCADE,
        related_name='user_exams',
        verbose_name='Exam'
    )
    attempt_no = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)], verbose_name='Attempt Number')
    status = models.CharField(max_length=20, choices=UserExamStatus.choices, default=UserExamStatus.DRAFT, verbose_name='Status')
    response_text = models.TextField(blank=True, null=True, verbose_name='Text Response')
    response_voice_path = models.CharField(max_length=500, blank=True, null=True, verbose_name='Voice File Path')
    started_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # timer state (server-side truth)
    remaining_seconds = models.PositiveIntegerField(null=True, blank=True)

    is_paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)

    # answers per question (simple + practical)
    answers = models.JSONField(default=dict, blank=True)
    feedback = models.ForeignKey(
        Feedback,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='user_exams',
        verbose_name='Feedback'
    )

    # Soft delete support (MySQL-safe)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name='Deleted At')

    created_at = models.DateTimeField(default=timezone.now, verbose_name='Created At')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Updated At')

    class Meta:
        app_label = 'team3'
        db_table = 'user_exam'
        verbose_name = 'User Exam'
        verbose_name_plural = 'User Exams'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'exam', 'attempt_no', 'is_deleted'],
                name='uq_user_exam_attempt_activeflag'
            )
        ]
        indexes = [
            models.Index(fields=['user', 'exam']),
            models.Index(fields=['status'], name='ix_user_exam_status'),
            models.Index(fields=['is_deleted']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.exam} (Attempt #{self.attempt_no})"

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])

    def get_next_attempt_number(self):
        last_attempt = UserExam.objects.filter(
            user=self.user,
            exam=self.exam,
            is_deleted=False
        ).order_by('-attempt_no').first()

        if last_attempt:
            return last_attempt.attempt_no + 1
        return 1

    def save(self, *args, **kwargs):
        if not self.attempt_no:
            self.attempt_no = self.get_next_attempt_number()
        super().save(*args, **kwargs)
