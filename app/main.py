"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# tz_aware so every datetime coming back from MongoDB is an aware UTC instant
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Instants: BSON datetime (UTC) in MongoDB <-> epoch milliseconds in the API
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    # R1: truncate to whole seconds
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc.get("created_at"))
    return doc


def serialize_log(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    history = doc.get("history") or []
    for entry in history:
        entry["at"] = dt_to_ms(entry.get("at"))
        for field in ("punch_in", "punch_out"):
            change = entry.get("changes", {}).get(field)
            if change:
                change["from"] = dt_to_ms(change.get("from"))
                change["to"] = dt_to_ms(change.get("to"))
    doc["history"] = history
    return doc


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return hhmm(emp["shift_end"]) <= hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    # R1: IST calendar date of the punch-in; for an overnight shift a punch-in
    # earlier than shift_end belongs to the shift that started the day before
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    d = date.fromisoformat(date_str)
    start = datetime.combine(d, hhmm(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(d, hhmm(emp["shift_end"]), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    # R2: late only if strictly more than 10:00 after shift start; floor minutes since shift start
    seconds = (punch_in - shift_start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    # R3: floor minutes after shift end, counted only when >= 30
    minutes = int((punch_out - shift_end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # R4: seconds / 3600, 2 decimals half-up
    hours = Decimal((punch_out - punch_in).total_seconds()) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(emp: dict, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    # Everything the API computes for a record (R2-R5), from the final punch times
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        return out
    start, end = shift_bounds(date_str, emp)
    out["late_minutes"] = compute_late_minutes(punch_in, start)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = out["work_hours"] < 4.5  # R5
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# StrictInt rejects floats/strings/bools; bounds reject seconds-looking values
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


def check_date(s: str) -> str:
    if not re.fullmatch(DATE_RE, s):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(s)  # rejects 2026-13-45
    return s


def query_date(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    try:
        return check_date(s)
    except ValueError as e:
        raise HTTPException(422, str(e))


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        return check_date(v)

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    # derived fields sent by a client are ignored (pydantic drops unknown keys)
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides the winner under concurrency
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc()
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(emp, d, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_log(doc)


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    date_from, date_to = query_date(date_from), query_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_log(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def check_punch_window(punch_in: datetime, punch_out: datetime):
    if punch_out <= punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc()
    # most recent record with a punch-in at or before ts; date order == punch_in order for one employee
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_window(rec["punch_in"], ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # only one concurrent punch-out can match
        {"$set": {"punch_out": ts, **derived_fields(emp, rec["date"], rec["punch_in"], ts)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return serialize_log(updated)


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
FIELD_DEFAULTS = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date = query_date(date)
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{status} cannot have punch times")
        punch_in = punch_out = None
    else:
        punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if punch_in is None:
            raise HTTPException(422, f"{status} requires a punch_in")
        if attendance_date(punch_in, emp) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if punch_out is not None:
            check_punch_window(punch_in, punch_out)

    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out, **derived_fields(emp, date, punch_in, punch_out)}
    changes = {
        f: {"from": rec.get(f, FIELD_DEFAULTS.get(f)), "to": new[f]}
        for f in TRACKED_FIELDS
        if rec.get(f, FIELD_DEFAULTS.get(f)) != new[f]
    }
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        # optimistic lock on the values this entry's "from" side was computed against
        {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently, retry")
    return serialize_log(updated)


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
