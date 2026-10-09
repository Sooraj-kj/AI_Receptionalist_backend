# Clienxo AI Receptionist Backend

FastAPI backend for the Clienxo demo. It seeds a fictional SmileCare Dental Clinic, exposes booking APIs, rejects duplicate active bookings with a database-level partial unique index, and includes a deterministic AI receptionist endpoint that only uses approved clinic data and backend booking tools.

## Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

API docs: http://127.0.0.1:8000/docs

Demo login:

- Email: `reception@clienxo.demo`
- Password: `demo12345`
