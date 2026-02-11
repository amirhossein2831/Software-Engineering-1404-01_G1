from django.http import JsonResponse
from django.shortcuts import render
from core.auth import api_login_required
from team3.models import ExamPack, Exam, ExamSystem, ExamSection, UserExam, UserExamStatus
from django.http import Http404
from typing import Dict, List

TEAM_NAME = "team3"

KEY_SEP = "|||"

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
    # 1) all finished user_exams for this user
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
        UserExam.objects.select_related("feedback", "exam", "exam__pack")
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
    has_feedback = bool(user_exam.feedback_id and user_exam.feedback and not user_exam.feedback.is_deleted)

    items = build_items(user_exam)

    context = {
        "user_exam": user_exam,
        "exam": exam,
        "pack_title": getattr(exam.pack, "title", ""),
        "section": exam.section,
        "has_feedback": has_feedback,
        "items": items,
    }
    return render(request, "team3/feedback_detail.html", context)


# ---------- Helpers ----------
def parse_feedback_map(description: str) -> Dict[int, str]:

    if not description:
        return {}

    result: Dict[int, str] = {}

    for line in description.splitlines():
        line = line.strip()
        if not line or KEY_SEP not in line:
            continue

        left, fb = line.split(KEY_SEP, 1)
        left = left.strip().upper()   # Q1
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

    # If user already uses "Q1|||..." format, parse it
    has_structured = any(line.strip().upper().startswith("Q") and KEY_SEP in line for line in raw.splitlines())
    if not has_structured:
        return {}  # means "no structured answers"

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

    # questions from DB
    qs = list(
        exam.questions.filter(is_deleted=False).order_by("number").values("number", "description")
    )
    question_count = len(qs)

    # feedback map
    feedback_map: Dict[int, str] = {}
    if user_exam.feedback_id and user_exam.feedback and not user_exam.feedback.is_deleted:
        feedback_map = parse_feedback_map(user_exam.feedback.description)

    # answers map (optional structured)
    raw_answer_text = user_exam.response_text or ""
    answers_map = split_answers_by_question_count(raw_answer_text, question_count)

    # fallback answer if not structured:
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

