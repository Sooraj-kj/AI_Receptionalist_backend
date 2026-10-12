import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["GEMINI_API_KEY"] = ""

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


def test_slot_hold_queues_duplicate_online_booking_and_blocks_offline_booking():
    with TestClient(app) as client:
        doctor_id = client.get("/doctors").json()[0]["id"]
        hold_payload = {
            "doctor_id": doctor_id,
            "date": "2026-10-13",
            "time": "09:00:00",
            "source": "web_chat",
            "session_id": "test_hold_session",
        }

        hold = client.post("/slot-holds", json=hold_payload)
        assert hold.status_code == 200
        hold_id = hold.json()["id"]

        slots = client.get(f"/slots?doctor_id={doctor_id}&date=2026-10-13").json()["slots"]
        assert "09:00:00" not in [slot["time"] for slot in slots]

        duplicate_hold = client.post("/slot-holds", json={**hold_payload, "session_id": "second_hold_session"})
        assert duplicate_hold.status_code == 200
        assert duplicate_hold.json()["queue_position"] == 2

        blocked_manual_booking = client.post(
            "/appointments",
            json={
                "doctor_id": doctor_id,
                "patient_name": "Offline Patient",
                "phone": "9800000022",
                "service": "General consultation",
                "date": "2026-10-13",
                "time": "09:00:00",
                "source": "reception",
            },
        )
        assert blocked_manual_booking.status_code == 409

        blocked_queued_booking = client.post(
            "/appointments",
            json={
                "doctor_id": doctor_id,
                "patient_name": "Queued Patient",
                "phone": "9800000024",
                "service": "General consultation",
                "date": "2026-10-13",
                "time": "09:00:00",
                "source": "web_chat",
                "slot_hold_id": duplicate_hold.json()["id"],
            },
        )
        assert blocked_queued_booking.status_code == 409

        confirmed = client.post(
            "/appointments",
            json={
                "doctor_id": doctor_id,
                "patient_name": "Online Patient",
                "phone": "9800000023",
                "service": "General consultation",
                "date": "2026-10-13",
                "time": "09:00:00",
                "source": "web_chat",
                "slot_hold_id": hold_id,
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["reference"].startswith("APT-")


def test_chat_uses_previous_booking_context_for_time_followup():
    with TestClient(app) as client:
        first = client.post(
            "/ai/chat",
            json={"message": "I want to book an appointment tomorrow"},
        )
        assert first.status_code == 200
        body = first.json()
        assert body["intent"] == "booking"

        followup = client.post(
            "/ai/chat",
            json={"session_id": body["session_id"], "message": "9 am"},
        )
        assert followup.status_code == 200
        assert followup.json()["intent"] == "booking"
        assert "name and phone" in followup.json()["reply"].lower()

        name = client.post(
            "/ai/chat",
            json={"session_id": body["session_id"], "message": "sam altman"},
        )
        assert name.status_code == 200
        assert name.json()["intent"] == "booking"
        assert "phone" in name.json()["reply"].lower()

        phone = client.post(
            "/ai/chat",
            json={"session_id": body["session_id"], "message": "9876543210"},
        )
        assert phone.status_code == 200
        assert phone.json()["intent"] == "booking_confirmed"
        assert phone.json()["appointment_reference"].startswith("APT-")


def test_chat_keeps_orthodontic_doctor_after_doctors_faq():
    with TestClient(app) as client:
        faq = client.post("/ai/chat", json={"message": "Who are the doctors?"})
        assert faq.status_code == 200

        service = client.post(
            "/ai/chat",
            json={"session_id": faq.json()["session_id"], "message": "ortho"},
        )
        assert service.status_code == 200
        assert "orthodontic" in service.json()["reply"].lower()

        date_response = client.post(
            "/ai/chat",
            json={"session_id": faq.json()["session_id"], "message": "next week monday"},
        )
        assert date_response.status_code == 200
        assert "Dr. Rahul Nair" in date_response.json()["reply"]
        assert "Dr. Asha Menon" not in date_response.json()["reply"]


def test_chat_date_only_message_does_not_parse_year_as_time():
    with TestClient(app) as client:
        response = client.post("/ai/chat", json={"message": "I want to book an appointment 2026-10-15"})
        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "booking"
        assert "which time" in body["reply"].lower()
        assert "name and phone" not in body["reply"].lower()


def test_chat_service_switch_after_faq_starts_fresh_booking_flow():
    with TestClient(app) as client:
        first = client.post("/ai/chat", json={"message": "I want to book an appointment 2026-10-15"})
        assert first.status_code == 200
        session_id = first.json()["session_id"]

        time_response = client.post("/ai/chat", json={"session_id": session_id, "message": "4 pm"})
        assert time_response.status_code == 200
        assert "name and phone" in time_response.json()["reply"].lower()

        faq = client.post("/ai/chat", json={"session_id": session_id, "message": "Who are the doctors?"})
        assert faq.status_code == 200
        assert faq.json()["intent"] == "faq"

        pediatric = client.post("/ai/chat", json={"session_id": session_id, "message": "pediatric"})
        assert pediatric.status_code == 200
        body = pediatric.json()
        assert body["intent"] == "booking"
        assert "which date" in body["reply"].lower()
        assert "being booked" not in body["reply"].lower()


def test_chat_general_small_talk_stays_in_faq_flow():
    with TestClient(app) as client:
        for message in ["Hi", "Hii", "hello", "Thank you so much", "thanks a lot", "ok thanks"]:
            response = client.post("/ai/chat", json={"message": message})
            assert response.status_code == 200
            body = response.json()
            assert body["intent"] == "faq"
            assert body["handoff"] is False
            assert "flagged" not in body["reply"].lower()


def test_chat_handoff_question_is_not_reused_as_patient_name():
    with TestClient(app) as client:
        first = client.post("/ai/chat", json={"message": "Hii"})
        assert first.status_code == 200
        session_id = first.json()["session_id"]

        handoff = client.post("/ai/chat", json={"session_id": session_id, "message": "who is the president"})
        assert handoff.status_code == 200
        assert handoff.json()["intent"] == "handoff"

        doctors = client.post("/ai/chat", json={"session_id": session_id, "message": "Who are the doctors?"})
        assert doctors.status_code == 200
        assert doctors.json()["intent"] == "faq"

        booking = client.post(
            "/ai/chat",
            json={"session_id": session_id, "message": "I want to book an appointment tomorrow"},
        )
        assert booking.status_code == 200
        assert booking.json()["intent"] == "booking"

        doctor_switch = client.post("/ai/chat", json={"session_id": session_id, "message": "asha"})
        assert doctor_switch.status_code == 200
        body = doctor_switch.json()
        assert body["intent"] == "booking"
        assert "Dr. Asha Menon" in body["reply"]
        assert "who is the president" not in body["reply"].lower()
        assert "phone number" not in body["reply"].lower()


def test_chat_thanks_after_confirmation_does_not_rebook_previous_slot():
    with TestClient(app) as client:
        first = client.post("/ai/chat", json={"message": "I want a pediatric appointment 2026-10-16"})
        assert first.status_code == 200
        session_id = first.json()["session_id"]

        time_response = client.post("/ai/chat", json={"session_id": session_id, "message": "11 am"})
        assert time_response.status_code == 200
        assert "name and phone" in time_response.json()["reply"].lower()

        name = client.post("/ai/chat", json={"session_id": session_id, "message": "Maya Nair"})
        assert name.status_code == 200
        assert "phone" in name.json()["reply"].lower()

        phone = client.post("/ai/chat", json={"session_id": session_id, "message": "2389472950"})
        assert phone.status_code == 200
        assert phone.json()["intent"] == "booking_confirmed"

        thanks = client.post("/ai/chat", json={"session_id": session_id, "message": "ok thanks"})
        assert thanks.status_code == 200
        body = thanks.json()
        assert body["intent"] == "faq"
        assert "you are welcome" in body["reply"].lower()
        assert "not available" not in body["reply"].lower()

        hours = client.post("/ai/chat", json={"session_id": session_id, "message": "What are your opening hours?"})
        assert hours.status_code == 200
        assert hours.json()["intent"] == "faq"
        assert "open" in hours.json()["reply"].lower()
