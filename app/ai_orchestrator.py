import re
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.gemini_client import GeminiAnalysis, analyze_with_gemini
from app.schemas import AppointmentCreate, ChatResponse, SlotRead
from app.services import create_appointment, find_active_slot_hold, get_default_clinic, hold_slot_for_session, list_slots

SERVICES = {
    "cleaning": "Cleaning",
    "filling": "Filling",
    "ortho": "Orthodontic consultation",
    "orthodontic": "Orthodontic consultation",
    "braces": "Orthodontic consultation",
    "aligner": "Orthodontic consultation",
    "child": "Pediatric consultation",
    "kid": "Pediatric consultation",
    "pediatric": "Pediatric consultation",
    "general": "General consultation",
    "consult": "General consultation",
}


def ensure_conversation(db: Session, session_id: str | None) -> models.Conversation:
    clinic = get_default_clinic(db)
    sid = session_id or uuid.uuid4().hex
    conversation = db.scalar(select(models.Conversation).where(models.Conversation.session_id == sid))
    if conversation is None:
        conversation = models.Conversation(clinic_id=clinic.id, session_id=sid)
        db.add(conversation)
        db.flush()
    return conversation


def add_message(db: Session, conversation: models.Conversation, role: str, content: str) -> None:
    db.add(models.Message(conversation_id=conversation.id, role=role, content=content))


def recent_history(db: Session, conversation: models.Conversation) -> list[dict[str, str]]:
    messages = db.scalars(
        select(models.Message)
        .where(models.Message.conversation_id == conversation.id)
        .order_by(models.Message.created_at.desc())
        .limit(10)
    ).all()
    return [{"role": msg.role, "content": msg.content} for msg in reversed(messages)]


def clinic_context(db: Session) -> dict:
    clinic = get_default_clinic(db)
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
    return {
        "clinic": {
            "name": clinic.name,
            "phone": clinic.phone,
            "email": clinic.email,
            "address": clinic.address,
            "opening_hours": clinic.opening_hours,
            "timezone": clinic.timezone,
        },
        "doctors": [{"name": doctor.name, "specialty": doctor.specialty} for doctor in doctors],
        "services": sorted(set(SERVICES.values())),
    }


def parse_date(message: str) -> date | None:
    text = message.lower()
    today = date.today()
    if "tomorrow" in text:
        return today + timedelta(days=1)
    if "today" in text:
        return today
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if match:
        return date.fromisoformat(match.group(1))
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    for weekday, index in weekdays.items():
        if weekday in text:
            days_ahead = (index - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return today + timedelta(days=days_ahead)
    return None


def parse_time(message: str):
    match = re.search(r"(?<![\d-])(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b(?![\d-])", message.lower())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridian = match.group(3)
    if meridian == "pm" and hour < 12:
        hour += 12
    if meridian == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and minute in (0, 30):
        return datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M").time()
    return None


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_clock(value: str | None) -> time | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None
    return parsed if parsed.minute in (0, 30) else None


def infer_service(message: str) -> str:
    text = message.lower()
    for key, service in SERVICES.items():
        if key in text:
            return service
    return "General consultation"


def match_doctor(db: Session, message: str, doctor_name: str | None = None) -> models.Doctor | None:
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
    candidates = [doctor_name, message]
    for candidate in candidates:
        if not candidate:
            continue
        wanted = candidate.lower()
        for doctor in doctors:
            doctor_name_lower = doctor.name.lower()
            name_parts = [part for part in re.split(r"[^a-z]+", doctor_name_lower) if len(part) >= 3 and part != "dr"]
            if doctor_name_lower in wanted or wanted in doctor_name_lower:
                return doctor
            if any(re.search(rf"\b{re.escape(part)}\b", wanted) for part in name_parts):
                return doctor
    return None


def infer_doctor(db: Session, service: str, message: str, doctor_name: str | None = None) -> models.Doctor:
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
    matched = match_doctor(db, message, doctor_name)
    if matched:
        return matched
    text = f"{service} {message}".lower()
    if "ortho" in text or "brace" in text or "aligner" in text:
        return next((doctor for doctor in doctors if "ortho" in doctor.specialty.lower()), doctors[0])
    if "child" in text or "kid" in text or "pediatric" in text:
        return next((doctor for doctor in doctors if "pediatric" in doctor.specialty.lower()), doctors[0])
    return doctors[0]


def last_booking_context(
    db: Session,
    conversation: models.Conversation,
) -> tuple[models.Doctor | None, date | None, time | None, str | None, str | None, str | None]:
    messages = db.scalars(
        select(models.Message)
        .where(models.Message.conversation_id == conversation.id)
        .order_by(models.Message.created_at.desc())
        .limit(12)
    ).all()
    messages = list(reversed(messages))
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
    doctor = None
    target_date = None
    target_time = None
    name = None
    phone = None
    service = None
    for index, msg in enumerate(messages):
        parsed_date = parse_date(msg.content)
        parsed_time = parse_time(msg.content)
        parsed_phone = extract_phone(msg.content)
        parsed_name = extract_name(msg.content)
        inferred_service = infer_service(msg.content)
        if inferred_service != "General consultation":
            service = inferred_service
            doctor = infer_doctor(db, inferred_service, msg.content)
        matched_doctor = match_doctor(db, msg.content)
        if msg.role == "user" and matched_doctor:
            doctor = matched_doctor
        if msg.role == "assistant" and matched_doctor and " which time would you prefer" in msg.content.lower():
            doctor = matched_doctor
        if msg.role == "user":
            target_date = parsed_date or target_date
            target_time = parsed_time or target_time
            phone = parsed_phone or phone
            name = parsed_name or name
            if awaiting_contact_details(messages[:index]):
                name = parsed_name or extract_bare_name(msg.content) or name
    return doctor, target_date, target_time, name, phone, service


def extract_phone(message: str) -> str | None:
    for match in re.finditer(r"(\+?\d[\d\s-]{6,}\d)", message):
        raw = match.group(1).strip()
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw):
            continue
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 10:
            return f"+{digits}" if raw.startswith("+") else digits
    return None


def extract_name(message: str) -> str | None:
    match = re.search(r"(?:my name is|name is|i am|i'm)\s+([a-zA-Z ]{2,40})", message, re.I)
    if match:
        return match.group(1).strip().title()
    return None


def extract_bare_name(message: str) -> str | None:
    text = message.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,39}", text):
        return None
    lowered = text.lower()
    blocked = {
        "book",
        "appointment",
        "slot",
        "visit",
        "today",
        "tomorrow",
        "doctor",
        "open",
        "hours",
        "service",
        "ortho",
        "orthodontic",
        "braces",
        "aligner",
        "cleaning",
        "filling",
        "child",
        "kid",
        "pediatric",
        "general",
        "consult",
    }
    if any(word in lowered for word in blocked):
        return None
    words = [word for word in re.split(r"\s+", text) if word]
    if 1 <= len(words) <= 4:
        return text.title()
    return None


def awaiting_contact_details(previous_messages: list[models.Message]) -> bool:
    previous_assistant_messages = [msg for msg in previous_messages if msg.role == "assistant"]
    if not previous_assistant_messages:
        return False
    latest = previous_assistant_messages[-1].content.lower()
    return "patient" in latest and "name" in latest and "phone" in latest


def latest_assistant_reply(db: Session, conversation: models.Conversation) -> str | None:
    message = db.scalar(
        select(models.Message)
        .where(models.Message.conversation_id == conversation.id, models.Message.role == "assistant")
        .order_by(models.Message.created_at.desc(), models.Message.id.desc())
        .limit(1)
    )
    return message.content if message else None


def is_booking_confirmation(reply: str | None) -> bool:
    return bool(reply and "confirmed" in reply.lower() and "appointment reference" in reply.lower())


def approved_small_talk(message: str) -> str | None:
    text = message.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", text).strip()
    greeting_words = {
        "hi",
        "hii",
        "hiii",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
    }
    thanks_patterns = (
        r"(?:ok|okay|great|fine|got it)\s+(?:thank\s*you|thanks|thankyou|thx)(?:\s+(?:a\s+lot|so\s+much|very\s+much))?",
        r"thank\s*you(?:\s+(?:so\s+much|very\s+much))?",
        r"thanks(?:\s+(?:a\s+lot|so\s+much))?",
        r"thankyou",
        r"thx",
    )
    acknowledgement_words = {"ok", "okay", "great", "got it", "fine"}
    if normalized in greeting_words:
        return "Hello. I can answer clinic FAQs or help request an appointment."
    if any(re.fullmatch(pattern, normalized) for pattern in thanks_patterns) or normalized in acknowledgement_words:
        return "You are welcome. I can still answer clinic FAQs or help request another appointment."
    return None


def approved_faq_by_topic(db: Session, topic: str | None) -> str | None:
    if not topic:
        return None
    clinic = get_default_clinic(db)
    if topic == "hours":
        return f"{clinic.name} is open {clinic.opening_hours}"
    if topic == "address":
        return f"We are located at {clinic.address}. You can call {clinic.phone} for directions."
    if topic == "services":
        return "We can help with general consultation, cleaning, fillings, orthodontic consultations and pediatric dental visits."
    if topic == "doctors":
        doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
        return "Our demo doctors are " + ", ".join(f"{d.name} ({d.specialty})" for d in doctors) + "."
    return None


def approved_faq(db: Session, message: str) -> str | None:
    clinic = get_default_clinic(db)
    text = message.lower()
    if any(word in text for word in ["hour", "open", "timing"]):
        return f"{clinic.name} is open {clinic.opening_hours}"
    if any(word in text for word in ["address", "location", "where"]):
        return f"We are located at {clinic.address}. You can call {clinic.phone} for directions."
    if "service" in text or "treatment" in text:
        return "We can help with general consultation, cleaning, fillings, orthodontic consultations and pediatric dental visits."
    if "doctor" in text:
        doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
        return "Our demo doctors are " + ", ".join(f"{d.name} ({d.specialty})" for d in doctors) + "."
    return None


def fallback_analysis(message: str) -> GeminiAnalysis:
    lowered = message.lower()
    service = infer_service(message)
    intent = (
        "booking"
        if any(word in lowered for word in ["book", "appointment", "slot", "visit"])
        or service != "General consultation"
        else None
    )
    return GeminiAnalysis(
        intent=intent,
        service=service,
        date=parse_date(message).isoformat() if parse_date(message) else None,
        time=parse_time(message).strftime("%H:%M") if parse_time(message) else None,
        patient_name=extract_name(message),
        phone=extract_phone(message),
    )


def chat(db: Session, message: str, session_id: str | None) -> ChatResponse:
    conversation = ensure_conversation(db, session_id)
    history = recent_history(db, conversation)
    last_assistant_reply = latest_assistant_reply(db, conversation)
    add_message(db, conversation, "user", message)

    ai = analyze_with_gemini(message=message, history=history, clinic_context=clinic_context(db))
    analysis = ai or fallback_analysis(message)
    (
        previous_doctor,
        previous_date,
        previous_time,
        previous_name,
        previous_phone,
        previous_service,
    ) = last_booking_context(db, conversation)

    faq = approved_faq_by_topic(db, analysis.faq_topic) or approved_faq(db, message)
    if faq:
        reply = faq
        add_message(db, conversation, "assistant", reply)
        db.commit()
        return ChatResponse(session_id=conversation.session_id, reply=reply, intent="faq")
    small_talk = approved_small_talk(message)
    if small_talk:
        add_message(db, conversation, "assistant", small_talk)
        db.commit()
        return ChatResponse(session_id=conversation.session_id, reply=small_talk, intent="faq")

    message_service = infer_service(message)
    has_explicit_service = message_service != "General consultation"
    message_has_booking_cue = any(word in message.lower() for word in ["book", "appointment", "slot", "visit"])
    service = analysis.service or message_service
    if has_explicit_service:
        service = message_service
    if service == "General consultation" and previous_service:
        service = previous_service
    message_date = parse_iso_date(analysis.date) or parse_date(message)
    message_time = parse_clock(analysis.time) or parse_time(message)
    message_phone = analysis.phone or extract_phone(message)
    message_name = analysis.patient_name or extract_name(message)
    if has_explicit_service:
        target_date = message_date
        target_time = message_time
        phone = message_phone
        name = message_name
    elif is_booking_confirmation(last_assistant_reply) and not any(
        [message_has_booking_cue, message_date, message_time, message_phone, message_name]
    ):
        target_date = None
        target_time = None
        phone = None
        name = None
    else:
        target_date = message_date or previous_date
        target_time = message_time or previous_time
        phone = message_phone or previous_phone
        name = message_name or previous_name
    expecting_patient_details = awaiting_contact_details(
        db.scalars(
            select(models.Message)
            .where(models.Message.conversation_id == conversation.id)
            .order_by(models.Message.created_at)
        ).all()[:-1]
    )
    if expecting_patient_details and not name:
        name = extract_bare_name(message)
    is_booking = (
        analysis.intent == "booking"
        or message_has_booking_cue
        or has_explicit_service
        or ((previous_doctor or previous_date or previous_service) and any([target_date, target_time, phone, name]))
    )

    if is_booking:
        message_doctor = None if expecting_patient_details else match_doctor(db, message, analysis.doctor_name)
        doctor = message_doctor or (
            infer_doctor(db, service, message, analysis.doctor_name)
            if has_explicit_service
            else previous_doctor or infer_doctor(db, service, message, analysis.doctor_name)
        )
        if target_date is None:
            reply = f"Sure, I can help with a {service.lower()}. Which date would you prefer? You can type it as YYYY-MM-DD."
            add_message(db, conversation, "assistant", reply)
            db.commit()
            return ChatResponse(session_id=conversation.session_id, reply=reply, intent="booking")
        slots = list_slots(db, doctor.id, target_date)
        if target_time is None:
            shown = slots[:5]
            slot_text = ", ".join(slot.label for slot in shown) if shown else "no slots are available"
            reply = f"{doctor.name} has {slot_text} on {target_date}. Which time would you prefer?"
            add_message(db, conversation, "assistant", reply)
            db.commit()
            return ChatResponse(
                session_id=conversation.session_id,
                reply=reply,
                intent="booking",
                suggested_slots=shown,
            )
        if not name or not phone:
            try:
                hold_slot_for_session(
                    db,
                    doctor_id=doctor.id,
                    target_date=target_date,
                    target_time=target_time,
                    session_id=conversation.session_id,
                )
            except Exception:
                db.rollback()
                alternatives: list[SlotRead] = list_slots(db, doctor.id, target_date)[:5]
                slot_text = ", ".join(slot.label for slot in alternatives) or "another date"
                reply = f"That time is being booked by someone else. I can offer {slot_text} instead."
                add_message(db, conversation, "assistant", reply)
                db.commit()
                return ChatResponse(
                    session_id=conversation.session_id,
                    reply=reply,
                    intent="booking_conflict",
                    suggested_slots=alternatives,
                )
            reply = (
                f"Thanks, {name}. May I have the phone number to complete the booking?"
                if name
                else "Great. May I have the patient's name and phone number to complete the booking?"
            )
            add_message(db, conversation, "assistant", reply)
            db.commit()
            return ChatResponse(session_id=conversation.session_id, reply=reply, intent="booking")
        try:
            hold = find_active_slot_hold(
                db,
                doctor_id=doctor.id,
                target_date=target_date,
                target_time=target_time,
                session_id=conversation.session_id,
            )
            appt = create_appointment(
                db,
                AppointmentCreate(
                    doctor_id=doctor.id,
                    patient_name=name,
                    phone=phone,
                    service=service,
                    date=target_date,
                    time=target_time,
                    source="ai_receptionist",
                    slot_hold_id=hold.id if hold else None,
                ),
            )
            reply = f"Confirmed. Your appointment reference is {appt.reference} with {doctor.name} on {target_date} at {target_time.strftime('%I:%M %p').lstrip('0')}."
            add_message(db, conversation, "assistant", reply)
            db.commit()
            return ChatResponse(
                session_id=conversation.session_id,
                reply=reply,
                intent="booking_confirmed",
                appointment_reference=appt.reference,
            )
        except Exception:
            db.rollback()
            alternatives: list[SlotRead] = list_slots(db, doctor.id, target_date)[:5]
            slot_text = ", ".join(slot.label for slot in alternatives) or "another date"
            reply = f"That time is not available. I can offer {slot_text} instead."
            return ChatResponse(
                session_id=conversation.session_id,
                reply=reply,
                intent="booking_conflict",
                suggested_slots=alternatives,
            )

    clinic = get_default_clinic(db)
    lead = models.Lead(
        clinic_id=clinic.id,
        name=name,
        phone=phone,
        service=service,
        source="ai_receptionist",
        status="new",
        notes=f"Unanswered chat message: {message}",
    )
    conversation.needs_handoff = True
    conversation.handoff_reason = "AI could not answer from approved clinic information."
    db.add(lead)
    reply = "I do not want to guess on that. I have flagged this for the receptionist, and the clinic team can follow up."
    add_message(db, conversation, "assistant", reply)
    db.commit()
    return ChatResponse(session_id=conversation.session_id, reply=reply, intent="handoff", handoff=True)
