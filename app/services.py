from datetime import date, datetime, time, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import models
from app.schemas import AppointmentCreate, AppointmentUpdate, SlotRead

ACTIVE_STATUSES = ("confirmed", "pending")


def format_time(value: time) -> str:
    return datetime.combine(date.today(), value).strftime("%I:%M %p").lstrip("0")


def get_default_clinic(db: Session) -> models.Clinic:
    clinic = db.scalar(select(models.Clinic).limit(1))
    if clinic is None:
        raise HTTPException(status_code=500, detail="Clinic seed data is missing")
    return clinic


def list_slots(db: Session, doctor_id: str, target_date: date) -> list[SlotRead]:
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
        if slot_time not in blocked and not in_exception:
            slots.append(SlotRead(time=slot_time, label=format_time(slot_time)))
        cursor += step
    return slots


def ensure_slot_available(db: Session, doctor_id: str, target_date: date, target_time: time) -> None:
    normalized = target_time.replace(second=0, microsecond=0)
    if all(slot.time != normalized for slot in list_slots(db, doctor_id, target_date)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This slot is unavailable. Please choose another time.",
        )


def next_reference(db: Session) -> str:
    count = db.scalar(select(func.count(models.Appointment.id))) or 0
    return f"APT-{1024 + count}"


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
