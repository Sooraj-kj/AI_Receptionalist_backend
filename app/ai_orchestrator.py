import re
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.schemas import AppointmentCreate, ChatResponse, SlotRead
from app.services import create_appointment, get_default_clinic, list_slots

SERVICES = {
    "cleaning": "Cleaning",
    "filling": "Filling",
    "braces": "Orthodontic consultation",
    "aligner": "Orthodontic consultation",
    "child": "Pediatric consultation",
    "kid": "Pediatric consultation",
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
    return None


def parse_time(message: str):
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", message.lower())
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


def infer_service(message: str) -> str:
    text = message.lower()
    for key, service in SERVICES.items():
        if key in text:
            return service
    return "General consultation"


def infer_doctor(db: Session, service: str, message: str) -> models.Doctor:
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all()
    text = f"{service} {message}".lower()
    if "ortho" in text or "brace" in text or "aligner" in text:
        return next((doctor for doctor in doctors if "ortho" in doctor.specialty.lower()), doctors[0])
    if "child" in text or "kid" in text or "pediatric" in text:
        return next((doctor for doctor in doctors if "pediatric" in doctor.specialty.lower()), doctors[0])
    return doctors[0]


def extract_phone(message: str) -> str | None:
    match = re.search(r"(\+?\d[\d\s-]{6,}\d)", message)
    return re.sub(r"[\s-]", "", match.group(1)) if match else None


def extract_name(message: str) -> str | None:
    match = re.search(r"(?:my name is|name is|i am|i'm)\s+([a-zA-Z ]{2,40})", message, re.I)
    if match:
        return match.group(1).strip().title()
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


def chat(db: Session, message: str, session_id: str | None) -> ChatResponse:
    conversation = ensure_conversation(db, session_id)
    add_message(db, conversation, "user", message)

    faq = approved_faq(db, message)
    if faq:
        reply = faq
        add_message(db, conversation, "assistant", reply)
        db.commit()
        return ChatResponse(session_id=conversation.session_id, reply=reply, intent="faq")

    service = infer_service(message)
    target_date = parse_date(message)
    target_time = parse_time(message)
    phone = extract_phone(message)
    name = extract_name(message)

    if any(word in message.lower() for word in ["book", "appointment", "slot", "visit"]):
        doctor = infer_doctor(db, service, message)
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
            reply = "Great. May I have the patient's name and phone number to complete the booking?"
            add_message(db, conversation, "assistant", reply)
            db.commit()
            return ChatResponse(session_id=conversation.session_id, reply=reply, intent="booking")
        try:
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
