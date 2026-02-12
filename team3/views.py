import logging
import os
import re
import threading
import uuid
from typing import Dict, List
import json

import whisper
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.http import Http404
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
from django.views.decorators.http import require_POST
from openai import OpenAI
from core.auth import api_login_required
from team3.models import ExamPack, Exam, ExamSystem, ExamSection, UserExam, UserExamStatus, Feedback

logger = logging.getLogger(__name__)

TEAM_NAME = "team3"

KEY_SEP = "|||"
_Q_LINE_RE = re.compile(
    r"^Q(\d+)\s*" + re.escape(KEY_SEP) + r"\s*(.*)$",
    re.IGNORECASE,
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENAI_API_KEY)

SYSTEM_MAP = {
    "IELTS": ExamSystem.IELTS,
    "TOEFL": ExamSystem.TOEFL,
    "GENERAL": ExamSystem.GENERAL,
}

FINISHED_STATUSES = [
    UserExamStatus.SUBMITTED,
    UserExamStatus.REVIEWED,
    UserExamStatus.GRADED,
]

SYSTEM_DISPLAY_FA = {
    ExamSystem.IELTS: "آیلتس",
    ExamSystem.TOEFL: "تافل",
    ExamSystem.GENERAL: "جنرال",
}

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

_whisper_model = None
def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = whisper.load_model("tiny")
    return _whisper_model

@api_login_required
def ping(request):
    return JsonResponse({"team": TEAM_NAME, "ok": True})


def base(request):
    return render(request, f"{TEAM_NAME}/index.html")

@api_login_required
@csrf_exempt
def exam(request):
    system_param = (request.GET.get("system") or "").upper()
    system = SYSTEM_MAP.get(system_param)

    if not system:
        system = ExamSystem.IELTS

    packs_qs = (
        ExamPack.objects
        .filter(system=system, is_deleted=False)
        .order_by("id")
    )

    pack_cards = []
    for p in packs_qs:
        exams = (
            Exam.objects
            .filter(pack=p, is_deleted=False)
            .values("id", "section")
        )
        section_to_exam_id = {e["section"]: e["id"] for e in exams}

        pack_cards.append({
            "id": p.id,
            "title": p.title,
            "sections": {
                "writing": section_to_exam_id.get(ExamSection.WRITING),
                "speaking": section_to_exam_id.get(ExamSection.SPEAKING),
                "reading": section_to_exam_id.get(ExamSection.READING),
                "listening": section_to_exam_id.get(ExamSection.LISTENING),
            }
        })

    ctx = {
        "system_key": system_param,
        "system": system,
        "system_fa": SYSTEM_DISPLAY_FA.get(system, "آزمون"),
        "packs": pack_cards,
    }
    return render(request, f"{TEAM_NAME}/exam.html", ctx)


@api_login_required
def feedback(request):
    user_exams = (
        UserExam.objects
        .select_related("exam", "exam__pack")
        .filter(
            user=request.user,
            is_deleted=False,
            status__in=FINISHED_STATUSES,
            exam__is_deleted=False,
            exam__pack__is_deleted=False,
        )
        .order_by("-created_at")
    )

    cards_by_pack = {}

    for ue in user_exams:
        user_exam = ue.exam
        pack = user_exam.pack
        if not pack:
            continue

        card = cards_by_pack.get(pack.id)
        if not card:
            card = {
                "pack_id": pack.id,
                "title": pack.title,
                "system": pack.system,
                "sections": {
                    "speaking": None,
                    "writing": None,
                    "reading": None,
                    "listening": None,
                },
                "last_attempt_at": ue.created_at,
            }
            cards_by_pack[pack.id] = card

        if ue.created_at and (card["last_attempt_at"] is None or ue.created_at > card["last_attempt_at"]):
            card["last_attempt_at"] = ue.created_at

        if user_exam.section == ExamSection.SPEAKING and card["sections"]["speaking"] is None:
            card["sections"]["speaking"] = user_exam.id
        elif user_exam.section == ExamSection.WRITING and card["sections"]["writing"] is None:
            card["sections"]["writing"] = user_exam.id
        elif user_exam.section == ExamSection.READING and card["sections"]["reading"] is None:
            card["sections"]["reading"] = user_exam.id
        elif user_exam.section == ExamSection.LISTENING and card["sections"]["listening"] is None:
            card["sections"]["listening"] = user_exam.id

    cards = sorted(cards_by_pack.values(), key=lambda c: c["last_attempt_at"], reverse=True)

    return render(request, f"{TEAM_NAME}/feedback.html", {"cards": cards})

@api_login_required
def feedback_detail(request):
    exam_id = request.GET.get("exam_id")
    if not exam_id:
        raise Http404("exam_id is required")

    user_exam = (
        UserExam.objects
        .select_related("feedback", "exam", "exam__pack")
        .filter(
            user=request.user,
            exam_id=exam_id,
            is_deleted=False,
            status__in=FINISHED_STATUSES,
            exam__is_deleted=False,
        )
        .order_by("-attempt_no", "-created_at")
        .first()
    )

    if not user_exam:
        raise Http404("No finished attempt found for this exam")

    exam = user_exam.exam

    is_speaking = (exam.section == ExamSection.SPEAKING)
    is_processing_speaking = (
            is_speaking and not (user_exam.response_text or "").strip()
    )
    raw_answer_text = (user_exam.response_text or "").strip()
    has_real_answer = has_any_non_empty_answer(raw_answer_text)
    is_empty_submission = ( raw_answer_text and not has_real_answer)

    has_feedback = (
            bool(user_exam.feedback_id)
            and user_exam.feedback is not None
            and (not user_exam.feedback.is_deleted)
            and bool((user_exam.feedback.description or "").strip())
    )

    if not has_feedback and not is_processing_speaking and not is_empty_submission:
        prompt = build_openai_prompt(exam, user_exam)
        generated_text = call_openai_for_feedback(prompt)
        generated_text = normalize_openai_feedback_text(generated_text)

        if generated_text:
            with transaction.atomic():
                fb = Feedback.objects.create(description=generated_text)
                user_exam.feedback = fb
                user_exam.save(update_fields=["feedback"])

            user_exam = (
                UserExam.objects
                .select_related("feedback", "exam", "exam__pack")
                .get(pk=user_exam.pk)
            )
            has_feedback = True

    items = build_items(user_exam)
    context = {
        "user_exam": user_exam,
        "exam": exam,
        "pack_title": getattr(exam.pack, "title", ""),
        "section": exam.section,
        "has_feedback": has_feedback,
        "items": items,
        "is_processing_speaking": is_processing_speaking,
        "is_empty_submission": is_empty_submission,
    }

    return render(request, "team3/feedback_detail.html", context)

def build_openai_prompt(exam, user_exam: UserExam) -> str:
    qs = list(
        exam.questions
        .filter(is_deleted=False)
        .order_by("number")
        .values("number", "description")
    )

    question_count = len(qs)

    raw_answer_text = user_exam.response_text or ""
    answers_map = split_answers_by_question_count(raw_answer_text, question_count)
    fallback = raw_answer_text.strip()

    qa_lines = []
    for q in qs:
        qn = int(q["number"])
        q_text = q["description"].strip()

        ans = answers_map.get(qn)
        if ans is None:
            ans = fallback if fallback else "(no answer)"

        qa_lines.append(f"Q{qn}: {q_text}\nA{qn}: {ans}")

    prompt = f"""
You are an English speaking/writing examiner (IELTS/TOEFL/General).
Evaluate answers strictly based on what the user wrote.

Return feedback for EACH question in EXACT format (ONE LINE per question):

Q1{KEY_SEP}Fluency: ... | Grammar: ... | Vocabulary: ... | Structure: ... | Tip: ...
Q2{KEY_SEP}Fluency: ... | Grammar: ... | Vocabulary: ... | Structure: ... | Tip: ...

Rules:
- ONE line per question only.
- Do NOT add headings, markdown, or extra lines.
- Even if the answer is OFF-TOPIC/EMPTY, still fill ALL 5 parts.
- If OFF-TOPIC: say "Off-topic" in Structure and Tip must tell how to answer the actual prompt.
- Minimum length per feedback line: ~35 words (to avoid too short answers).
- Be specific to the question and the given answer.

Q&A:
{chr(10).join(qa_lines)}
""".strip()

    return prompt

def call_openai_for_feedback(prompt: str) -> str:
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict exam examiner. "
                        "Return ONE LINE per question in this exact format:\n"
                        "Q1|||Fluency: ... | Grammar: ... | Vocabulary: ... | Structure: ... | Tip: ...\n"
                        "Q2|||Fluency: ... | Grammar: ... | Vocabulary: ... | Structure: ... | Tip: ...\n"
                        "No headings. No extra lines."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1200,  # give room for richer lines
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("OpenAI feedback generation failed")
        return ""

def normalize_openai_feedback_text(text: str) -> str:
    if not text:
        return ""

    out_lines: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        up = line.upper()
        if not up.startswith("Q"):
            continue
        if KEY_SEP not in line:
            continue
        left, right = line.split(KEY_SEP, 1)
        left = left.strip().upper()  # Q1
        right = right.strip()
        if not left[1:].isdigit():
            continue
        if not right:
            continue
        out_lines.append(f"{left}{KEY_SEP}{right}")

    return "\n".join(out_lines).strip()

def parse_feedback_map(description: str) -> Dict[int, str]:
    if not description:
        return {}

    result: Dict[int, str] = {}
    for line in description.splitlines():
        line = line.strip()
        if not line or KEY_SEP not in line:
            continue

        left, fb = line.split(KEY_SEP, 1)
        left = left.strip().upper()  # Q1
        fb = fb.strip()

        if not left.startswith("Q"):
            continue

        num_str = left[1:]
        if not num_str.isdigit():
            continue

        qn = int(num_str)
        result[qn] = fb

    return result

def split_answers_by_question_count(raw: str, question_count: int) -> Dict[int, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}

    has_structured = any(
        line.strip().upper().startswith("Q") and KEY_SEP in line and line.strip().upper()[1:2].isdigit()
        for line in raw.splitlines()
    )
    if not has_structured:
        return {}

    ans_map: Dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or KEY_SEP not in line:
            continue

        left, ans = line.split(KEY_SEP, 1)
        left = left.strip().upper()
        ans = ans.strip()

        if not left.startswith("Q"):
            continue

        num_str = left[1:]
        if not num_str.isdigit():
            continue

        qn = int(num_str)
        ans_map[qn] = ans

    return ans_map

def build_items(user_exam: UserExam) -> List[dict]:
    exam = user_exam.exam

    qs = list(
        exam.questions
        .filter(is_deleted=False)
        .order_by("number")
        .values("number", "description")
    )
    question_count = len(qs)

    feedback_map: Dict[int, str] = {}
    if (
            user_exam.feedback_id
            and user_exam.feedback
            and not user_exam.feedback.is_deleted
            and (user_exam.feedback.description or "").strip()
    ):
        feedback_map = parse_feedback_map(user_exam.feedback.description)

    raw_answer_text = user_exam.response_text or ""
    answers_map = split_answers_by_question_count(raw_answer_text, question_count)

    fallback_answer = raw_answer_text.strip()

    items: List[dict] = []
    for q in qs:
        qn = int(q["number"])
        question_text = q["description"]

        answer = answers_map.get(qn)
        if answer is None:
            answer = fallback_answer if fallback_answer else "(پاسخی ثبت نشده)"

        fb = feedback_map.get(qn, "")

        items.append(
            {
                "number": qn,
                "question": question_text,
                "answer": answer,
                "feedback": fb,
            }
        )

    return items

def has_any_non_empty_answer(raw: str) -> bool:
    if not raw:
        return False

    for line in raw.splitlines():
        if KEY_SEP not in line:
            continue

        left, ans = line.split(KEY_SEP, 1)
        left = left.strip().upper()
        ans = ans.strip()

        if left.startswith("Q") and left[1:].isdigit() and ans:
            return True

    return False

def _calc_remaining_seconds(user_exam: UserExam) -> int:
    if user_exam.remaining_seconds is None:
        user_exam.remaining_seconds = user_exam.exam.exam_time_seconds

    if user_exam.is_paused:
        return user_exam.remaining_seconds

    now = timezone.now()
    anchor = user_exam.last_seen_at or user_exam.started_at or now
    elapsed = int((now - anchor).total_seconds())
    remaining = max(0, user_exam.remaining_seconds - elapsed)
    return remaining

@api_login_required
@ensure_csrf_cookie
@api_login_required
def writing_exam(request, exam_id: int):
    exam = get_object_or_404(
        Exam.objects.select_related("pack").prefetch_related("questions"),
        id=exam_id,
        section=ExamSection.WRITING,
        is_deleted=False,
    )
    if exam.pack and exam.pack.is_deleted:
        raise Http404("Pack deleted")

    already_done = UserExam.objects.filter(
        user=request.user,
        exam=exam,
        is_deleted=False,
        status__in=FINISHED_STATUSES,
    ).exists()

    if already_done:
        url = reverse("exam")
        return redirect(f"{url}?system={exam.system.upper()}&modal=already_done&exam_id={exam.id}")

    ue = (
        UserExam.objects.filter(
            user=request.user,
            exam=exam,
            is_deleted=False,
        )
        .exclude(status__in=FINISHED_STATUSES)
        .order_by("-created_at")
        .first()
    )

    if not ue:
        ue = UserExam.objects.create(
            user=request.user,
            exam=exam,
            attempt_no=1,
            status=UserExamStatus.IN_PROGRESS,
            started_at=timezone.now(),
            last_seen_at=timezone.now(),
            remaining_seconds=exam.exam_time_seconds,
            is_paused=False,
        )

    # update timer state
    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.last_seen_at = timezone.now()
    ue.is_paused = False
    ue.paused_at = None
    if ue.status == UserExamStatus.DRAFT:
        ue.status = UserExamStatus.IN_PROGRESS
    ue.save(update_fields=["remaining_seconds", "last_seen_at", "is_paused", "paused_at", "status"])

    questions = list(
        exam.questions.filter(is_deleted=False).order_by("number").values("id", "number", "description")
    )

    answers_map_int = parse_response_text(ue.response_text or "")
    # template tag expects string keys
    answers_map = {str(k): v for k, v in answers_map_int.items()}

    return render(request, f"{TEAM_NAME}/writing.html", {
        "user_exam": ue,
        "exam": exam,
        "pack": exam.pack,
        "questions": questions,
        "remaining_seconds": ue.remaining_seconds or exam.exam_time_seconds,
        "answers_map": answers_map,
    })

@api_login_required
@csrf_exempt
@require_POST
def writing_pause(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)

    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": False, "error": "Already finished"}, status=400)

    remaining = _calc_remaining_seconds(ue)
    ue.remaining_seconds = remaining
    ue.is_paused = True
    ue.paused_at = timezone.now()
    ue.last_seen_at = timezone.now()
    ue.save(update_fields=["remaining_seconds", "is_paused", "paused_at", "last_seen_at"])

    return JsonResponse({"ok": True, "remaining_seconds": remaining})

@api_login_required
@csrf_exempt
@require_POST
def writing_resume(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)

    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": False, "error": "Already finished"}, status=400)

    # Resume without changing remaining (we already froze it on pause)
    ue.is_paused = False
    ue.paused_at = None
    ue.last_seen_at = timezone.now()
    ue.status = UserExamStatus.IN_PROGRESS
    ue.save(update_fields=["is_paused", "paused_at", "last_seen_at", "status"])

    return JsonResponse({"ok": True, "remaining_seconds": ue.remaining_seconds or ue.exam.exam_time_seconds})


@api_login_required
@csrf_exempt
@require_POST
def writing_autosave(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)

    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": False, "error": "Already finished"}, status=400)

    payload = json.loads(request.body.decode("utf-8") or "{}")
    incoming = payload.get("answers", {})  # {"1":"text","2":"text"} keys are question numbers

    existing = parse_response_text(ue.response_text or "")

    # merge updates
    for k, v in (incoming or {}).items():
        k = str(k).strip()
        if not k.isdigit():
            continue
        qn = int(k)
        existing[qn] = (v or "").strip()

    ue.response_text = build_response_text(existing)
    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.last_seen_at = timezone.now()
    ue.save(update_fields=["response_text", "remaining_seconds", "last_seen_at"])

    return JsonResponse({"ok": True, "remaining_seconds": ue.remaining_seconds})

@api_login_required
@csrf_exempt
@require_POST
def writing_submit(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)

    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": True})

    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.last_seen_at = timezone.now()
    ue.is_paused = False
    ue.paused_at = None
    ue.status = UserExamStatus.SUBMITTED
    ue.save(update_fields=["remaining_seconds", "last_seen_at", "is_paused", "paused_at", "status"])

    return JsonResponse({"ok": True})

@api_login_required
@csrf_exempt
@require_POST
def writing_exit(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)

    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": True})

    remaining = _calc_remaining_seconds(ue)
    ue.remaining_seconds = remaining
    ue.is_paused = True
    ue.status = UserExamStatus.DRAFT
    ue.paused_at = timezone.now()
    ue.last_seen_at = timezone.now()
    ue.save(update_fields=["remaining_seconds", "is_paused", "status", "paused_at", "last_seen_at"])

    return JsonResponse({"ok": True, "redirect": "/"} )


def parse_response_text(raw: str) -> Dict[int, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}

    out: Dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        m = _Q_LINE_RE.match(line)
        if not m:
            continue

        qn = int(m.group(1))
        ans = (m.group(2) or "").strip()
        out[qn] = ans

    return out


def build_response_text(answer_map: Dict[int, str]) -> str:

    if not answer_map:
        return ""

    lines = []
    for qn in sorted(answer_map.keys()):
        ans = (answer_map.get(qn) or "").strip()
        lines.append(f"Q{qn}{KEY_SEP}{ans}")

    return "\n".join(lines).strip()

@ensure_csrf_cookie
@api_login_required
def speaking_exam(request, exam_id: int):
    exam = get_object_or_404(
        Exam.objects.select_related("pack").prefetch_related("questions"),
        id=exam_id,
        section=ExamSection.SPEAKING,
        is_deleted=False,
    )

    already_done = UserExam.objects.filter(
        user=request.user,
        exam=exam,
        is_deleted=False,
        status__in=FINISHED_STATUSES,
    ).exists()

    if already_done:
        url = reverse("exam")
        return redirect(f"{url}?system={exam.system.upper()}&modal=already_done&exam_id={exam.id}")

    ue = (
        UserExam.objects.filter(user=request.user, exam=exam, is_deleted=False)
        .exclude(status__in=FINISHED_STATUSES)
        .order_by("-created_at")
        .first()
    )

    if not ue:
        ue = UserExam.objects.create(
            user=request.user,
            exam=exam,
            attempt_no=1,
            status=UserExamStatus.IN_PROGRESS,
            started_at=timezone.now(),
            last_seen_at=timezone.now(),
            remaining_seconds=exam.exam_time_seconds,
            is_paused=False,
        )

    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.last_seen_at = timezone.now()
    ue.is_paused = False
    ue.paused_at = None
    if ue.status == UserExamStatus.DRAFT:
        ue.status = UserExamStatus.IN_PROGRESS
    ue.save(update_fields=["remaining_seconds", "last_seen_at", "is_paused", "paused_at", "status"])

    questions = list(
        exam.questions.filter(is_deleted=False).order_by("number").values("id", "number", "description")
    )

    voice_map_int = parse_response_text(ue.response_text or "")
    voice_map = {str(k): v for k, v in voice_map_int.items()}

    return render(request, f"{TEAM_NAME}/speaking.html", {
        "user_exam": ue,
        "exam": exam,
        "pack": exam.pack,
        "questions": questions,
        "remaining_seconds": ue.remaining_seconds or exam.exam_time_seconds,
        "voice_map": voice_map,
    })

@api_login_required
@csrf_exempt
@require_POST
def speaking_pause(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)
    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": False, "error": "Already finished"}, status=400)

    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.is_paused = True
    ue.paused_at = timezone.now()
    ue.last_seen_at = timezone.now()
    ue.save(update_fields=["remaining_seconds", "is_paused", "paused_at", "last_seen_at"])
    return JsonResponse({"ok": True, "remaining_seconds": ue.remaining_seconds})


@api_login_required
@csrf_exempt
@require_POST
def speaking_resume(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)
    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": False, "error": "Already finished"}, status=400)

    ue.is_paused = False
    ue.paused_at = None
    ue.last_seen_at = timezone.now()
    ue.status = UserExamStatus.IN_PROGRESS
    ue.save(update_fields=["is_paused", "paused_at", "last_seen_at", "status"])
    return JsonResponse({"ok": True, "remaining_seconds": ue.remaining_seconds or ue.exam.exam_time_seconds})

@api_login_required
@csrf_exempt
@require_POST
def speaking_upload(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)
    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": False, "error": "Already finished"}, status=400)

    qnum_raw = (request.POST.get("qnum") or "").strip()
    if not qnum_raw.isdigit():
        return JsonResponse({"ok": False, "error": "qnum is required"}, status=400)
    qn = int(qnum_raw)

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "file is required"}, status=400)

    orig_name = f.name or "audio"
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in [".mp3", ".wav", ".m4a", ".ogg", ".aac", ".webm"]:
        return JsonResponse({"ok": False, "error": "unsupported file type"}, status=400)

    root = "/app/team3/static/team3/public/"
    rel_dir = "voices"
    os.makedirs(os.path.join(root, rel_dir), exist_ok=True)

    filename = f"ue{ue.id}_q{qn}_{uuid.uuid4().hex}{ext}"
    rel_path = f"{rel_dir}/{filename}"
    abs_path = os.path.join(root, rel_path)

    with open(abs_path, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)

    voice_map = parse_response_text(ue.response_voice_path or "")
    voice_map[qn] = rel_path
    ue.response_voice_path = build_response_text(voice_map)

    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.last_seen_at = timezone.now()
    ue.save(update_fields=["response_voice_path", "remaining_seconds", "last_seen_at"])

    return JsonResponse({
        "ok": True,
        "qnum": qn,
        "rel_path": rel_path,
        "remaining_seconds": ue.remaining_seconds,
    })

@api_login_required
@csrf_exempt
@require_POST
def speaking_submit(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)

    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": True})

    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.last_seen_at = timezone.now()
    ue.is_paused = False
    ue.paused_at = None
    ue.status = UserExamStatus.SUBMITTED
    ue.save(update_fields=["remaining_seconds", "last_seen_at", "is_paused", "paused_at", "status"])

    start_transcription_thread_after_commit(ue.id)

    return JsonResponse({"ok": True, "transcription_started": True})

@api_login_required
@csrf_exempt
@require_POST
def speaking_exit(request, user_exam_id: int):
    ue = get_object_or_404(UserExam, id=user_exam_id, user=request.user, is_deleted=False)
    if ue.status in FINISHED_STATUSES:
        return JsonResponse({"ok": True, "redirect": "/team3/feedbacks/"})

    ue.remaining_seconds = _calc_remaining_seconds(ue)
    ue.is_paused = True
    ue.status = UserExamStatus.DRAFT
    ue.paused_at = timezone.now()
    ue.last_seen_at = timezone.now()
    ue.save(update_fields=["remaining_seconds", "is_paused", "status", "paused_at", "last_seen_at"])

    return JsonResponse({"ok": True, "redirect": "/team3/exams/"})

def transcribe_audio_file(audio_path: str) -> str:
    if not os.path.isfile(audio_path):
        return ""
    model = get_whisper_model()
    try:
        result = model.transcribe(audio_path, language="en", fp16=False)
        return result["text"].strip()
    except Exception as e:
        logger.error(f"Whisper transcription failed for {audio_path}: {e}")
        return ""

def parse_kv_lines(raw: str) -> Dict[int, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    out: Dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _Q_LINE_RE.match(line)
        if not m:
            continue
        out[int(m.group(1))] = (m.group(2) or "").strip()
    return out

def build_kv_lines(m: Dict[int, str]) -> str:
    if not m:
        return ""
    return "\n".join([f"Q{qn}{KEY_SEP}{(m.get(qn) or '').strip()}" for qn in sorted(m.keys())]).strip()


def _abs_from_rel(rel_path: str) -> str:
    root = "/app/team3/static/team3/public/"
    return os.path.join(root, rel_path)

def transcribe_user_exam_voices(user_exam_id: int) -> None:
    try:
        ue = (
            UserExam.objects
            .select_related("exam")
            .get(id=user_exam_id, is_deleted=False)
        )
    except UserExam.DoesNotExist:
        return

    if ue.status not in [UserExamStatus.SUBMITTED, UserExamStatus.REVIEWED]:
        return

    voice_map = parse_kv_lines(ue.response_voice_path or "")
    if not voice_map:
        return

    transcript_map = {}
    for qn, rel_path in voice_map.items():
        abs_path = _abs_from_rel(rel_path)
        if not abs_path or not os.path.isfile(abs_path):
            transcript_map[qn] = ""
            continue

        txt = transcribe_audio_file(abs_path)
        transcript_map[qn] = txt

    ue.response_text = build_kv_lines(transcript_map)
    ue.last_seen_at = timezone.now()

    ue.save(update_fields=["response_text", "last_seen_at"])

    logger.info("Whisper transcription done | user_exam_id=%s", ue.id)

def start_transcription_thread_after_commit(user_exam_id: int) -> None:
    def _start():
        t = threading.Thread(
            target=transcribe_user_exam_voices,
            args=(user_exam_id,),
            daemon=True,
        )
        t.start()

    transaction.on_commit(_start)