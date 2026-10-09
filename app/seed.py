from datetime import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import get_settings
from app.models import AdminUser, Clinic, Doctor, DoctorSchedule
from app.security import hash_password


def seed_demo_data(db: Session) -> None:
    settings = get_settings()
    if db.scalar(select(Clinic).limit(1)):
        return

    clinic = Clinic(
        id="clinic_smilecare",
        name="SmileCare Dental Clinic",
        phone="+91 98765 43210",
        email="hello@smilecare.example",
        address="2nd Floor, Clienxo Plaza, MG Road, Kochi, Kerala",
        opening_hours="Monday to Saturday, 9:00 AM to 6:00 PM. Closed on Sundays.",
        timezone="Asia/Calcutta",
    )
    db.add(clinic)

    doctors = [
        Doctor(
            id="doc_asha",
            clinic_id=clinic.id,
            name="Dr. Asha Menon",
            specialty="General dentistry",
            bio="General consultation, cleaning, fillings and preventive dental care.",
        ),
        Doctor(
            id="doc_rahul",
            clinic_id=clinic.id,
            name="Dr. Rahul Nair",
            specialty="Orthodontics",
            bio="Braces, aligners and smile correction consultations.",
        ),
        Doctor(
            id="doc_meera",
            clinic_id=clinic.id,
            name="Dr. Meera Iyer",
            specialty="Pediatric dentistry",
            bio="Gentle dental visits for children and family dental guidance.",
        ),
    ]
    db.add_all(doctors)
    db.flush()

    for doctor in doctors:
        for day in range(0, 6):
            db.add(
                DoctorSchedule(
                    doctor_id=doctor.id,
                    day_of_week=day,
                    start_time=time(9, 0),
                    end_time=time(17, 0),
                    slot_duration_minutes=30,
                )
            )

    db.add(
        AdminUser(
            email=settings.demo_admin_email,
            password_hash=hash_password(settings.demo_admin_password),
            is_active=True,
        )
    )
    db.commit()
