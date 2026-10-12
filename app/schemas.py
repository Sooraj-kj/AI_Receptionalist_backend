from datetime import date as Date
from datetime import datetime, time as Time

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ClinicRead(BaseModel):
    id: str
    name: str
    phone: str
    email: str
    address: str
    opening_hours: str
    timezone: str

    model_config = ConfigDict(from_attributes=True)


class DoctorRead(BaseModel):
    id: str
    name: str
    specialty: str
    bio: str
    active: bool

    model_config = ConfigDict(from_attributes=True)


class SlotRead(BaseModel):
    time: Time
    label: str


class AppointmentCreate(BaseModel):
    doctor_id: str
    patient_name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=7, max_length=32)
    service: str = Field(default="General consultation", max_length=160)
    date: Date
    time: Time
    source: str = "manual"
    slot_hold_id: str | None = None


class AppointmentUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(confirmed|pending|cancelled|completed|no_show)$")
    date: Date | None = None
    time: Time | None = None
    doctor_id: str | None = None
    service: str | None = None


class AppointmentRead(BaseModel):
    id: str
    reference: str
    service: str
    appointment_date: Date
    appointment_time: Time
    status: str
    source: str
    created_at: datetime
    doctor: DoctorRead
    patient_name: str
    patient_phone: str


class AppointmentSuccess(BaseModel):
    success: bool = True
    appointment_id: str
    reference: str
    status: str


class SlotHoldCreate(BaseModel):
    doctor_id: str
    date: Date
    time: Time
    source: str = "web_chat"
    session_id: str | None = None


class SlotHoldRead(BaseModel):
    id: str
    doctor_id: str
    appointment_date: Date
    appointment_time: Time
    status: str
    source: str
    session_id: str | None
    queue_position: int
    expires_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LeadCreate(BaseModel):
    name: str | None = None
    phone: str | None = None
    service: str = "General enquiry"
    source: str = "ai_receptionist"
    notes: str = ""


class LeadUpdate(BaseModel):
    status: str = Field(pattern="^(new|contacted|converted|closed)$")


class LeadRead(BaseModel):
    id: str
    name: str | None
    phone: str | None
    service: str
    source: str
    status: str
    notes: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    intent: str
    handoff: bool = False
    appointment_reference: str | None = None
    suggested_slots: list[SlotRead] = []


class HandoffRequest(BaseModel):
    session_id: str
    reason: str


class DashboardSummary(BaseModel):
    today_appointments: int
    pending_appointments: int
    open_leads: int
    total_confirmed: int
