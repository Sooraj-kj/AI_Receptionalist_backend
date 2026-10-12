from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, joinedload

from app import models
from app.ai_orchestrator import chat as run_chat
from app.core import get_settings
from app.database import Base, engine, get_db
from app.schemas import (
    AppointmentCreate,
    AppointmentRead,
    AppointmentSuccess,
    AppointmentUpdate,
    ChatRequest,
    ChatResponse,
    ClinicRead,
    DashboardSummary,
    DoctorRead,
    HandoffRequest,
    LeadCreate,
    LeadRead,
    LeadUpdate,
    LoginRequest,
    SlotHoldCreate,
    SlotHoldRead,
    TokenResponse,
)
from app.security import create_token, require_admin, verify_password
from app.seed import seed_demo_data
from app.services import (
    create_appointment,
    create_slot_hold,
    get_appointment_or_404,
    get_default_clinic,
    list_slots,
    serialize_appointment,
    update_appointment,
)

settings = get_settings()
app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ensure_runtime_schema() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as connection:
        columns = [row[1] for row in connection.execute(text("PRAGMA table_info(slot_holds)"))]
        if "queue_position" not in columns:
            connection.execute(text("ALTER TABLE slot_holds ADD COLUMN queue_position INTEGER NOT NULL DEFAULT 1"))
        connection.execute(text("DROP INDEX IF EXISTS uq_active_slot_hold"))


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()
    with Session(engine) as db:
        seed_demo_data(db)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(models.AdminUser).where(models.AdminUser.email == payload.email))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid login")
    return TokenResponse(access_token=create_token(user.email))


@app.get("/clinic", response_model=ClinicRead)
def clinic(db: Session = Depends(get_db)) -> models.Clinic:
    return get_default_clinic(db)


@app.get("/doctors", response_model=list[DoctorRead])
def doctors(db: Session = Depends(get_db)) -> list[models.Doctor]:
    return list(db.scalars(select(models.Doctor).where(models.Doctor.active.is_(True))).all())


@app.get("/doctors/{doctor_id}", response_model=DoctorRead)
def doctor_detail(doctor_id: str, db: Session = Depends(get_db)) -> models.Doctor:
    doctor = db.get(models.Doctor, doctor_id)
    if doctor is None or not doctor.active:
        raise HTTPException(status_code=404, detail="Doctor not found")
    return doctor


@app.get("/slots")
def slots(doctor_id: str, date_: date = Query(alias="date"), db: Session = Depends(get_db)) -> dict:
    return {"doctor_id": doctor_id, "date": date_, "slots": list_slots(db, doctor_id, date_)}


@app.post("/slot-holds", response_model=SlotHoldRead)
def slot_hold_create(payload: SlotHoldCreate, db: Session = Depends(get_db)) -> models.SlotHold:
    return create_slot_hold(db, payload)


@app.get("/appointments", response_model=list[AppointmentRead])
def appointments(
    date_: date | None = Query(default=None, alias="date"),
    status: str | None = None,
    doctor_id: str | None = None,
    db: Session = Depends(get_db),
    _: models.AdminUser = Depends(require_admin),
) -> list[dict]:
    query = select(models.Appointment).options(
        joinedload(models.Appointment.doctor), joinedload(models.Appointment.patient)
    )
    if date_:
        query = query.where(models.Appointment.appointment_date == date_)
    if status:
        query = query.where(models.Appointment.status == status)
    if doctor_id:
        query = query.where(models.Appointment.doctor_id == doctor_id)
    query = query.order_by(models.Appointment.appointment_date, models.Appointment.appointment_time)
    return [serialize_appointment(appt) for appt in db.scalars(query).all()]


@app.post("/appointments", response_model=AppointmentSuccess)
def appointment_create(payload: AppointmentCreate, db: Session = Depends(get_db)) -> AppointmentSuccess:
    appt = create_appointment(db, payload)
    return AppointmentSuccess(appointment_id=appt.id, reference=appt.reference, status=appt.status)


@app.get("/appointments/{appointment_id}", response_model=AppointmentRead)
def appointment_detail(
    appointment_id: str,
    db: Session = Depends(get_db),
    _: models.AdminUser = Depends(require_admin),
) -> dict:
    return serialize_appointment(get_appointment_or_404(db, appointment_id))


@app.patch("/appointments/{appointment_id}", response_model=AppointmentRead)
def appointment_patch(
    appointment_id: str,
    payload: AppointmentUpdate,
    db: Session = Depends(get_db),
    _: models.AdminUser = Depends(require_admin),
) -> dict:
    return serialize_appointment(update_appointment(db, appointment_id, payload))


@app.post("/leads", response_model=LeadRead)
def lead_create(payload: LeadCreate, db: Session = Depends(get_db)) -> models.Lead:
    clinic = get_default_clinic(db)
    lead = models.Lead(clinic_id=clinic.id, **payload.model_dump())
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


@app.get("/leads", response_model=list[LeadRead])
def leads(
    status: str | None = None,
    db: Session = Depends(get_db),
    _: models.AdminUser = Depends(require_admin),
) -> list[models.Lead]:
    query = select(models.Lead).order_by(models.Lead.created_at.desc())
    if status:
        query = query.where(models.Lead.status == status)
    return list(db.scalars(query).all())


@app.patch("/leads/{lead_id}", response_model=LeadRead)
def lead_patch(
    lead_id: str,
    payload: LeadUpdate,
    db: Session = Depends(get_db),
    _: models.AdminUser = Depends(require_admin),
) -> models.Lead:
    lead = db.get(models.Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead.status = payload.status
    db.commit()
    db.refresh(lead)
    return lead


@app.post("/ai/chat", response_model=ChatResponse)
def ai_chat(payload: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    return run_chat(db, payload.message, payload.session_id)


@app.post("/ai/handoff")
def handoff(payload: HandoffRequest, db: Session = Depends(get_db)) -> dict:
    conversation = db.scalar(select(models.Conversation).where(models.Conversation.session_id == payload.session_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.needs_handoff = True
    conversation.handoff_reason = payload.reason
    db.commit()
    return {"success": True}


@app.get("/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary(
    db: Session = Depends(get_db),
    _: models.AdminUser = Depends(require_admin),
) -> DashboardSummary:
    today = date.today()
    return DashboardSummary(
        today_appointments=db.scalar(
            select(func.count(models.Appointment.id)).where(
                models.Appointment.appointment_date == today,
                models.Appointment.status.in_(["confirmed", "pending"]),
            )
        )
        or 0,
        pending_appointments=db.scalar(
            select(func.count(models.Appointment.id)).where(models.Appointment.status == "pending")
        )
        or 0,
        open_leads=db.scalar(select(func.count(models.Lead.id)).where(models.Lead.status == "new")) or 0,
        total_confirmed=db.scalar(
            select(func.count(models.Appointment.id)).where(models.Appointment.status == "confirmed")
        )
        or 0,
    )
