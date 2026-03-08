
from fastapi import APIRouter
from models import Alert
from db import SessionLocal

router = APIRouter()

@router.post("/")
def create_alert(device_id: int):
    db = SessionLocal()
    alert = Alert(device_id=device_id)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return {"message": "Alert created", "alert": alert}

@router.get("/")
def get_alerts():
    db = SessionLocal()
    alerts = db.query(Alert).all()
    return {"alerts": alerts}
