from datetime import date, datetime, time, timedelta
import secrets

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import models
from app.core import get_settings
from app.schemas import AppointmentCreate, AppointmentUpdate, SlotHoldCreate, SlotRead

ACTIVE_STATUSES = ("confirmed", "pending")
ACTIVE_HOLD_STATUS = "active"


def format_time(value: time) -> str:
    return datetime.combine(date.today(), value).strftime("%I:%M %p").lstrip("0")


def get_default_clinic(db: Session) -> models.Clinic:
    clinic = db.scalar(select(models.Clinic).limit(1))
    if clinic is None:
        raise HTTPException(status_code=500, detail="Clinic seed data is missing")
    return clinic


def utc_now() -> datetime:
    return datetime.utcnow()


def expire_slot_holds(db: Session) -> None:
    expired = db.scalars(
        select(models.SlotHold).where(
            models.SlotHold.status == ACTIVE_HOLD_STATUS,
            models.SlotHold.expires_at <= utc_now(),
        )
    ).all()
    for hold in expired:
        hold.status = "expired"
    if expired:
        db.flush()


def list_slots(db: Session, doctor_id: str, target_date: date) -> list[SlotRead]:
    expire_slot_holds(db)
    doctor = db.get(models.Doctor, doctor_id)
    if doctor is None or not doctor.active:
        raise HTTPException(status_code=404, detail="Doctor not found")

    schedule = db.scalar(
        select(models.DoctorSchedule)
        .where(
            models.DoctorSchedule.doctor_id == doctor_id,
            models.DoctorSchedule.day_of_week == target_date.weekday(),
        )
        .limit(1)
    )
    if schedule is None:
        return []

    blocked = set(
        db.scalars(
            select(models.Appointment.appointment_time).where(
                models.Appointment.doctor_id == doctor_id,
                models.Appointment.appointment_date == target_date,
                models.Appointment.status.in_(ACTIVE_STATUSES),
            )
        ).all()
    )
    held = set(
        db.scalars(
            select(models.SlotHold.appointment_time).where(
                models.SlotHold.doctor_id == doctor_id,
                models.SlotHold.appointment_date == target_date,
                models.SlotHold.status == ACTIVE_HOLD_STATUS,
                models.SlotHold.expires_at > utc_now(),
            )
        ).all()
    )
    unavailable = db.scalars(
        select(models.ScheduleException).where(
            models.ScheduleException.doctor_id == doctor_id,
            models.ScheduleException.date == target_date,
            models.ScheduleException.is_available.is_(False),
        )
    ).all()

    slots: list[SlotRead] = []
    cursor = datetime.combine(target_date, schedule.start_time)
    end = datetime.combine(target_date, schedule.end_time)
    step = timedelta(minutes=schedule.slot_duration_minutes)
    while cursor + step <= end:
        slot_time = cursor.time().replace(second=0, microsecond=0)
        in_exception = any(exc.start_time <= slot_time < exc.end_time for exc in unavailable)
        if slot_time not in blocked and slot_time not in held and not in_exception:
            slots.append(SlotRead(time=slot_time, label=format_time(slot_time)))
        cursor += step
    return slots


def ensure_slot_open_for_hold(db: Session, doctor_id: str, target_date: date, target_time: time) -> None:
    normalized = target_time.replace(second=0, microsecond=0)
    if db.scalar(
        select(models.Appointment.id)
        .where(
            models.Appointment.doctor_id == doctor_id,
            models.Appointment.appointment_date == target_date,
            models.Appointment.appointment_time == normalized,
            models.Appointment.status.in_(ACTIVE_STATUSES),
        )
        .limit(1)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This slot is already booked. Please choose another time.",
        )

    doctor = db.get(models.Doctor, doctor_id)
    if doctor is None or not doctor.active:
        raise HTTPException(status_code=404, detail="Doctor not found")
    schedule = db.scalar(
        select(models.DoctorSchedule)
        .where(
            models.DoctorSchedule.doctor_id == doctor_id,
            models.DoctorSchedule.day_of_week == target_date.weekday(),
        )
        .limit(1)
    )
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This doctor is not available on that date.")

    start = datetime.combine(target_date, schedule.start_time)
    end = datetime.combine(target_date, schedule.end_time)
    selected = datetime.combine(target_date, normalized)
    step = timedelta(minutes=schedule.slot_duration_minutes)
    if selected < start or selected + step > end or (selected - start).total_seconds() % step.total_seconds() != 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This slot is outside the doctor's schedule.")

    unavailable = db.scalars(
        select(models.ScheduleException).where(
            models.ScheduleException.doctor_id == doctor_id,
            models.ScheduleException.date == target_date,
            models.ScheduleException.is_available.is_(False),
        )
    ).all()
    if any(exc.start_time <= normalized < exc.end_time for exc in unavailable):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This slot is unavailable.")


def ensure_slot_available(db: Session, doctor_id: str, target_date: date, target_time: time) -> None:
    normalized = target_time.replace(second=0, microsecond=0)
    if all(slot.time != normalized for slot in list_slots(db, doctor_id, target_date)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This slot is unavailable. Please choose another time.",
        )


def next_reference(db: Session) -> str:
    return f"APT-{secrets.randbelow(900000) + 100000}"


def get_active_slot_hold(db: Session, hold_id: str) -> models.SlotHold:
    expire_slot_holds(db)
    hold = db.get(models.SlotHold, hold_id)
    if hold is None or hold.status != ACTIVE_HOLD_STATUS or hold.expires_at <= utc_now():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This slot hold has expired.")
    return hold


def find_active_slot_hold(
    db: Session,
    *,
    doctor_id: str,
    target_date: date,
    target_time: time,
    session_id: str | None = None,
) -> models.SlotHold | None:
    expire_slot_holds(db)
    query = select(models.SlotHold).where(
        models.SlotHold.doctor_id == doctor_id,
        models.SlotHold.appointment_date == target_date,
        models.SlotHold.appointment_time == target_time.replace(second=0, microsecond=0),
        models.SlotHold.status == ACTIVE_HOLD_STATUS,
        models.SlotHold.expires_at > utc_now(),
    )
    if session_id:
        query = query.where(models.SlotHold.session_id == session_id)
    return db.scalar(query.limit(1))


def active_slot_holds(
    db: Session,
    *,
    doctor_id: str,
    target_date: date,
    target_time: time,
) -> list[models.SlotHold]:
    expire_slot_holds(db)
    return list(
        db.scalars(
            select(models.SlotHold)
            .where(
                models.SlotHold.doctor_id == doctor_id,
                models.SlotHold.appointment_date == target_date,
                models.SlotHold.appointment_time == target_time.replace(second=0, microsecond=0),
                models.SlotHold.status == ACTIVE_HOLD_STATUS,
                models.SlotHold.expires_at > utc_now(),
            )
            .order_by(models.SlotHold.queue_position, models.SlotHold.created_at, models.SlotHold.id)
        ).all()
    )


def ensure_hold_is_front_of_queue(db: Session, hold: models.SlotHold) -> None:
    holds = active_slot_holds(
        db,
        doctor_id=hold.doctor_id,
        target_date=hold.appointment_date,
        target_time=hold.appointment_time,
    )
    if not holds or holds[0].id != hold.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another patient is ahead in the queue for this slot.",
        )


def release_other_slot_holds(db: Session, hold: models.SlotHold) -> None:
    other_holds = db.scalars(
        select(models.SlotHold).where(
            models.SlotHold.doctor_id == hold.doctor_id,
            models.SlotHold.appointment_date == hold.appointment_date,
            models.SlotHold.appointment_time == hold.appointment_time,
            models.SlotHold.status == ACTIVE_HOLD_STATUS,
            models.SlotHold.id != hold.id,
        )
    ).all()
    for other_hold in other_holds:
        other_hold.status = "released"


def create_slot_hold(db: Session, payload: SlotHoldCreate) -> models.SlotHold:
    clinic = get_default_clinic(db)
    doctor = db.get(models.Doctor, payload.doctor_id)
    if doctor is None or not doctor.active:
        raise HTTPException(status_code=404, detail="Doctor not found")
    normalized_time = payload.time.replace(second=0, microsecond=0)
    existing = (
        find_active_slot_hold(
            db,
            doctor_id=payload.doctor_id,
            target_date=payload.date,
            target_time=normalized_time,
            session_id=payload.session_id,
        )
        if payload.session_id
        else None
    )
    if existing:
        return existing
    ensure_slot_open_for_hold(db, payload.doctor_id, payload.date, normalized_time)
    queue_position = (db.scalar(
        select(func.max(models.SlotHold.queue_position)).where(
            models.SlotHold.doctor_id == payload.doctor_id,
            models.SlotHold.appointment_date == payload.date,
            models.SlotHold.appointment_time == normalized_time,
            models.SlotHold.status == ACTIVE_HOLD_STATUS,
            models.SlotHold.expires_at > utc_now(),
        )
    ) or 0) + 1

    hold = models.SlotHold(
        clinic_id=clinic.id,
        doctor_id=payload.doctor_id,
        appointment_date=payload.date,
        appointment_time=normalized_time,
        source=payload.source,
        session_id=payload.session_id,
        queue_position=queue_position,
        expires_at=utc_now() + timedelta(minutes=get_settings().slot_hold_minutes),
    )
    db.add(hold)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That slot is being held by another booking. Please choose another time.",
        ) from exc
    db.refresh(hold)
    return hold


def hold_slot_for_session(
    db: Session,
    *,
    doctor_id: str,
    target_date: date,
    target_time: time,
    session_id: str,
    source: str = "ai_receptionist",
) -> models.SlotHold:
    existing = find_active_slot_hold(
        db,
        doctor_id=doctor_id,
        target_date=target_date,
        target_time=target_time,
        session_id=session_id,
    )
    if existing:
        return existing
    return create_slot_hold(
        db,
        SlotHoldCreate(
            doctor_id=doctor_id,
            date=target_date,
            time=target_time,
            source=source,
            session_id=session_id,
        ),
    )


def find_or_create_patient(db: Session, name: str, phone: str) -> models.Patient:
    patient = db.scalar(select(models.Patient).where(models.Patient.phone == phone).limit(1))
    if patient:
        patient.name = name
        return patient
    patient = models.Patient(name=name, phone=phone)
    db.add(patient)
    db.flush()
    return patient


def create_appointment(db: Session, payload: AppointmentCreate) -> models.Appointment:
    clinic = get_default_clinic(db)
    doctor = db.get(models.Doctor, payload.doctor_id)
    if doctor is None or not doctor.active:
        raise HTTPException(status_code=404, detail="Doctor not found")
    hold = get_active_slot_hold(db, payload.slot_hold_id) if payload.slot_hold_id else None
    if hold:
        if (
            hold.doctor_id != payload.doctor_id
            or hold.appointment_date != payload.date
            or hold.appointment_time != payload.time.replace(second=0, microsecond=0)
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slot hold does not match appointment.")
        ensure_hold_is_front_of_queue(db, hold)
    else:
        ensure_slot_available(db, payload.doctor_id, payload.date, payload.time)

    patient = find_or_create_patient(db, payload.patient_name, payload.phone)
    appointment = models.Appointment(
        reference=next_reference(db),
        clinic_id=clinic.id,
        doctor_id=payload.doctor_id,
        patient_id=patient.id,
        service=payload.service,
        appointment_date=payload.date,
        appointment_time=payload.time.replace(second=0, microsecond=0),
        status="confirmed",
        source=payload.source,
    )
    db.add(appointment)
    if hold:
        hold.status = "confirmed"
        release_other_slot_holds(db, hold)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That appointment time was just booked. Please choose another slot.",
        ) from exc
    db.refresh(appointment)
    return appointment


def serialize_appointment(appt: models.Appointment) -> dict:
    return {
        "id": appt.id,
        "reference": appt.reference,
        "service": appt.service,
        "appointment_date": appt.appointment_date,
        "appointment_time": appt.appointment_time,
        "status": appt.status,
        "source": appt.source,
        "created_at": appt.created_at,
        "doctor": appt.doctor,
        "patient_name": appt.patient.name,
        "patient_phone": appt.patient.phone,
    }


def get_appointment_or_404(db: Session, appointment_id: str) -> models.Appointment:
    appt = db.scalar(
        select(models.Appointment)
        .options(joinedload(models.Appointment.doctor), joinedload(models.Appointment.patient))
        .where((models.Appointment.id == appointment_id) | (models.Appointment.reference == appointment_id))
    )
    if appt is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return appt


def update_appointment(db: Session, appointment_id: str, payload: AppointmentUpdate) -> models.Appointment:
    appt = get_appointment_or_404(db, appointment_id)
    if payload.doctor_id or payload.date or payload.time:
        doctor_id = payload.doctor_id or appt.doctor_id
        target_date = payload.date or appt.appointment_date
        target_time = (payload.time or appt.appointment_time).replace(second=0, microsecond=0)
        original_status = appt.status
        appt.status = "cancelled"
        db.flush()
        ensure_slot_available(db, doctor_id, target_date, target_time)
        appt.doctor_id = doctor_id
        appt.appointment_date = target_date
        appt.appointment_time = target_time
        appt.status = payload.status or original_status
    if payload.service:
        appt.service = payload.service
    if payload.status:
        appt.status = payload.status
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Requested appointment slot is unavailable") from exc
    return get_appointment_or_404(db, appointment_id)
