import logging
import os
from typing import Dict, List
import whisper
from django.db import transaction
from django.http import Http404
from django.http import JsonResponse
from django.shortcuts import render
from openai import OpenAI
from core.auth import api_login_required
from team3.models import ExamPack, Exam, ExamSystem, ExamSection, UserExam, UserExamStatus, Feedback

logger = logging.getLogger(__name__)

TEAM_NAME = "team3"

KEY_SEP = "|||"

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

    has_feedback = (
            bool(user_exam.feedback_id)
            and user_exam.feedback is not None
            and (not user_exam.feedback.is_deleted)
            and bool((user_exam.feedback.description or "").strip())
    )

    if not has_feedback:
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

        "debug": {
            "exam_id": str(exam_id),
            "user_exam_id": user_exam.id,
            "attempt_no": user_exam.attempt_no,
            "status": user_exam.status,
            "has_feedback": has_feedback,
            "feedback_id": user_exam.feedback_id,
            "feedback_desc_len": len((user_exam.feedback.description or "") if user_exam.feedback else ""),
            "response_len": len(user_exam.response_text or ""),
            "items_len": len(items),
        }
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


@api_login_required
def check_voice_file_exists(request):
    exam_id = request.GET.get("exam_id")

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

    exists, abs_path, reason = voice_file_exists_for_user_exam(user_exam)
    transcript = ""
    if exists:
        transcript = transcribe_audio_file(abs_path)

    logger.info(
        "VOICE CHECK | user=%s exam_id=%s user_exam_id=%s exists=%s reason=%s path=%s",
        request.user.id, exam_id, user_exam.id, exists, reason, abs_path
    )

    return JsonResponse({
        "ok": True,
        "exam_id": int(exam_id),
        "user_exam_id": user_exam.id,
        "exists": exists,
        "reason": reason,
        "rel_path": user_exam.response_voice_path or "",
        "abs_path": abs_path,
        "transcript": transcript,   # ✨ NEW FIELD
    })

def voice_file_exists_for_user_exam(user_exam):
    rel_path = (user_exam.response_voice_path or "").strip()
    if not rel_path:
        return False, "", "empty_response_voice_path"

    root = "/app/team3/static/team3/public/"
    abs_path = os.path.join(root, rel_path)
    last_checked = abs_path
    if os.path.isfile(abs_path):
        return True, abs_path, "found"

    return False, last_checked, "not_found"

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

@api_login_required
def speaking(request):
    return render(request, "team3/speaking.html")
