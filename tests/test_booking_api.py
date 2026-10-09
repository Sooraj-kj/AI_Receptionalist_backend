import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient

from app.main import app


def test_booking_rejects_duplicate_slot():
    with TestClient(app) as client:
        doctors = client.get("/doctors").json()
        doctor_id = doctors[0]["id"]

        slots_response = client.get(f"/slots?doctor_id={doctor_id}&date=2026-10-12")
        assert slots_response.status_code == 200
        slot = slots_response.json()["slots"][0]["time"]

        payload = {
            "doctor_id": doctor_id,
            "patient_name": "Rahul Kumar",
            "phone": "9800000000",
            "service": "General consultation",
            "date": "2026-10-12",
            "time": slot,
            "source": "website",
        }
        first = client.post("/appointments", json=payload)
        assert first.status_code == 200
        assert first.json()["reference"].startswith("APT-")

        second = client.post("/appointments", json={**payload, "patient_name": "Anita Rao", "phone": "9800000001"})
        assert second.status_code == 409


def test_demo_login_and_dashboard_summary():
    with TestClient(app) as client:
        login = client.post(
            "/auth/login",
            json={"email": "reception@clienxo.demo", "password": "demo12345"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        summary = client.get("/dashboard/summary", headers={"Authorization": f"Bearer {token}"})
        assert summary.status_code == 200
        assert "today_appointments" in summary.json()
