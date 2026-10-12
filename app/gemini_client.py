import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.core import get_settings


@dataclass
class GeminiAnalysis:
    intent: str | None = None
    service: str | None = None
    date: str | None = None
    time: str | None = None
    doctor_name: str | None = None
    patient_name: str | None = None
    phone: str | None = None
    faq_topic: str | None = None


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def analyze_with_gemini(
    *,
    message: str,
    history: list[dict[str, str]],
    clinic_context: dict[str, Any],
) -> GeminiAnalysis | None:
    settings = get_settings()
    if not settings.gemini_api_key:
        return None

    prompt = f"""
You are the NLU module for a dental clinic AI receptionist.
Return only valid JSON. Do not answer the patient directly.

Today is {date.today().isoformat()}.

Clinic data:
{json.dumps(clinic_context, default=str)}

Recent conversation:
{json.dumps(history[-10:], default=str)}

Current patient message:
{message}

Extract structured intent for the backend tools.
Allowed intent values: faq, booking, handoff.
Use booking for appointment requests and short follow-ups that choose a date, time, doctor, name, or phone.
Infer a follow-up like "9 am" from the recent conversation if the assistant just offered slots.
Use null for unknown fields.

JSON schema:
{{
  "intent": "faq | booking | handoff",
  "service": "string or null",
  "date": "YYYY-MM-DD or null",
  "time": "HH:MM 24-hour or null",
  "doctor_name": "string or null",
  "patient_name": "string or null",
  "phone": "string or null",
  "faq_topic": "hours | address | services | doctors | null"
}}
""".strip()

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = httpx.post(
            url,
            params={"key": settings.gemini_api_key},
            json=payload,
            timeout=settings.gemini_timeout_seconds,
        )
        response.raise_for_status()
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None

    data = _extract_json(raw_text)
    if not data:
        return None
    return GeminiAnalysis(
        intent=data.get("intent"),
        service=data.get("service"),
        date=data.get("date"),
        time=data.get("time"),
        doctor_name=data.get("doctor_name"),
        patient_name=data.get("patient_name"),
        phone=data.get("phone"),
        faq_topic=data.get("faq_topic"),
    )
