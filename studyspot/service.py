"""Core logic and storage. No MCP imports here: the three tools are thin
wrappers around these functions, and the orchestrator reuses the same two
functions the Data and Action tools expose, so behaviour cannot drift apart.

Every SQL statement is parameterised (`?` placeholders). No user value is ever
formatted into SQL text.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo

from .models import AvailableRoom, Booking, PlanStep, Room, SearchResult, StudyPlan

TZ = ZoneInfo("Asia/Beirut")
OPEN_MIN, CLOSE_MIN = 8 * 60, 22 * 60  # library hours 08:00-22:00
BOOKING_WINDOW_DAYS = 14
DAILY_QUOTA_MIN = 180

SEED_ROOMS: list[tuple[str, str, str, int, int, list[str]]] = [
    ("JAF-101", "Jafet Quiet Pod 1", "Jafet Library", 1, 2, ["quiet", "power_outlets"]),
    ("JAF-204", "Jafet Group Room 204", "Jafet Library", 2, 4, ["whiteboard", "power_outlets"]),
    ("JAF-210", "Jafet Group Room 210", "Jafet Library", 2, 6,
     ["whiteboard", "screen", "power_outlets", "accessible"]),
    ("JAF-305", "Jafet Seminar Room 305", "Jafet Library", 3, 8, ["whiteboard", "screen"]),
    ("ENG-110", "Engineering Lab Room 110", "Engineering Library", 1, 4,
     ["whiteboard", "screen", "accessible"]),
    ("ENG-120", "Engineering Lab Room 120", "Engineering Library", 1, 6, ["whiteboard", "power_outlets"]),
    ("ENG-215", "Engineering Project Room 215", "Engineering Library", 2, 10,
     ["whiteboard", "screen", "power_outlets", "accessible"]),
    ("SML-012", "Saab Quiet Carrel 012", "Saab Medical Library", 0, 2, ["quiet"]),
    ("SML-114", "Saab Study Room 114", "Saab Medical Library", 1, 4, ["quiet", "power_outlets", "accessible"]),
]


class ServiceError(Exception):
    """A predictable, user-facing failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" + (f" Hint: {self.hint}" if self.hint else "")


# ---------------------------------------------------------------------------
# Clock and time helpers
# ---------------------------------------------------------------------------


def now() -> datetime:
    """Current Beirut time. STUDYSPOT_NOW (ISO 8601) pins it for tests and demos."""
    pinned = os.environ.get("STUDYSPOT_NOW")
    if pinned:
        dt = datetime.fromisoformat(pinned)
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
    return datetime.now(TZ)


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ServiceError("INVALID_DATE", f"'{value}' is not a real calendar date.",
                           "Use YYYY-MM-DD, for example 2026-10-27.") from None


def validate_slot(day: str, start: str, duration: int) -> tuple[int, int]:
    """Check a requested slot against library rules; return (start_min, end_min)."""
    d = _parse_date(day)
    current = now()
    today = current.date()
    if d < today:
        raise ServiceError("DATE_IN_PAST", f"{day} has already passed.",
                           f"Today is {today.isoformat()}.")
    if d > today + timedelta(days=BOOKING_WINDOW_DAYS - 1):
        last = today + timedelta(days=BOOKING_WINDOW_DAYS - 1)
        raise ServiceError("OUTSIDE_BOOKING_WINDOW",
                           f"Rooms can only be booked up to {BOOKING_WINDOW_DAYS} days ahead.",
                           f"The last bookable date is {last.isoformat()}.")
    s = _to_min(start)
    e = s + duration
    if s < OPEN_MIN or e > CLOSE_MIN:
        raise ServiceError("OUTSIDE_OPENING_HOURS",
                           f"{start}-{_fmt(e)} is outside library hours (08:00-22:00).",
                           "Pick an earlier start or a shorter duration.")
    if d == today and s <= current.hour * 60 + current.minute:
        raise ServiceError("TIME_IN_PAST", f"{start} today has already passed.",
                           f"It is now {current.strftime('%H:%M')}.")
    return s, e


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    building  TEXT NOT NULL,
    floor     INTEGER NOT NULL,
    capacity  INTEGER NOT NULL CHECK (capacity > 0),
    features  TEXT NOT NULL            -- JSON array
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    room_id    TEXT NOT NULL REFERENCES rooms(room_id),
    day        TEXT NOT NULL,
    start_min  INTEGER NOT NULL,
    end_min    INTEGER NOT NULL CHECK (end_min > start_min),
    holder     TEXT NOT NULL,
    purpose    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bookings_room_day ON bookings(room_id, day);
CREATE INDEX IF NOT EXISTS ix_bookings_holder_day ON bookings(holder, day);
"""


def db_path() -> Path:
    return Path(os.environ.get("STUDYSPOT_DB", Path(__file__).resolve().parent.parent / "data" / "studyspot.db"))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3], r[4], json.dumps(r[5])) for r in SEED_ROOMS],
            )


def _room(row: sqlite3.Row) -> Room:
    return Room(room_id=row["room_id"], name=row["name"], building=row["building"],
                floor=row["floor"], capacity=row["capacity"], features=json.loads(row["features"]))


def list_rooms() -> list[Room]:
    with connect() as conn:
        return [_room(r) for r in conn.execute("SELECT * FROM rooms ORDER BY room_id")]


def get_room(room_id: str) -> Room:
    with connect() as conn:
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    if row is None:
        raise ServiceError("ROOM_NOT_FOUND", f"No room with id '{room_id}'.",
                           "Call search_rooms first and use a room_id it returns.")
    return _room(row)


# ---------------------------------------------------------------------------
# Data: search (read-only)
# ---------------------------------------------------------------------------


def search_rooms(day: str, start: str, duration: int, group_size: int,
                 features: Sequence[str] = (), building: str | None = None) -> SearchResult:
    s, e = validate_slot(day, start, duration)
    wanted = set(features)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.* FROM rooms r
            WHERE r.capacity >= ?
              AND (? IS NULL OR r.building = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM bookings b
                  WHERE b.room_id = r.room_id AND b.day = ?
                    AND b.start_min < ? AND ? < b.end_min)
            """,
            (group_size, building, building, day, e, s),
        ).fetchall()
    rooms = [
        AvailableRoom(**_room(r).model_dump(), fit_score=r["capacity"] - group_size)
        for r in rows
        if wanted <= set(json.loads(r["features"]))
    ]
    rooms.sort(key=lambda r: (r.fit_score, r.room_id))
    return SearchResult(date=day, start=start, end=_fmt(e), group_size=group_size,
                        rooms=rooms, count=len(rooms))


# ---------------------------------------------------------------------------
# Action: book (writes)
# ---------------------------------------------------------------------------


def book_room(room_id: str, day: str, start: str, duration: int, holder: str, purpose: str) -> Booking:
    s, e = validate_slot(day, start, duration)
    room = get_room(room_id)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")  # serialise writers: no double booking under races
        try:
            clash = conn.execute(
                "SELECT 1 FROM bookings WHERE room_id = ? AND day = ? AND start_min < ? AND ? < end_min",
                (room_id, day, e, s),
            ).fetchone()
            if clash:
                raise ServiceError("ROOM_UNAVAILABLE", f"{room_id} is already booked during {start}-{_fmt(e)}.",
                                   "Run search_rooms for this slot to see rooms that are still free.")
            own = conn.execute(
                "SELECT room_id, start_min, end_min FROM bookings "
                "WHERE holder = ? AND day = ? AND start_min < ? AND ? < end_min",
                (holder, day, e, s),
            ).fetchone()
            if own:
                raise ServiceError("HOLDER_CONFLICT",
                                   f"You already hold {own['room_id']} from {_fmt(own['start_min'])} "
                                   f"to {_fmt(own['end_min'])} on {day}.",
                                   "One person can hold only one room at a time.")
            used = conn.execute(
                "SELECT COALESCE(SUM(end_min - start_min), 0) FROM bookings WHERE holder = ? AND day = ?",
                (holder, day),
            ).fetchone()[0]
            if used + (e - s) > DAILY_QUOTA_MIN:
                raise ServiceError("QUOTA_EXCEEDED",
                                   f"This booking would bring you to {used + e - s} minutes on {day}; "
                                   f"the daily limit is {DAILY_QUOTA_MIN}.",
                                   f"You have {DAILY_QUOTA_MIN - used} minutes left that day.")
            booking_id = f"BK-{uuid.uuid4().hex[:8].upper()}"
            conn.execute(
                "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (booking_id, room_id, day, s, e, holder, purpose, now().isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return Booking(booking_id=booking_id, room_id=room_id, room_name=room.name, building=room.building,
                   date=day, start=start, end=_fmt(e), holder=holder, purpose=purpose, status="confirmed")


def get_booking(booking_id: str, holder: str) -> Booking:
    """Return a booking only to its owner; anyone else gets 'not found' (no existence leak)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT b.*, r.name, r.building FROM bookings b JOIN rooms r USING (room_id) "
            "WHERE b.booking_id = ? AND b.holder = ?",
            (booking_id, holder),
        ).fetchone()
    if row is None:
        raise ServiceError("BOOKING_NOT_FOUND", f"No booking '{booking_id}' for this user.")
    return Booking(booking_id=row["booking_id"], room_id=row["room_id"], room_name=row["name"],
                   building=row["building"], date=row["day"], start=_fmt(row["start_min"]),
                   end=_fmt(row["end_min"]), holder=row["holder"], purpose=row["purpose"], status="confirmed")


# ---------------------------------------------------------------------------
# Orchestration: search then book
# ---------------------------------------------------------------------------


def plan_study_session(day: str, earliest_start: str, latest_start: str, duration: int, group_size: int,
                       holder: str, purpose: str, features: Sequence[str] = (),
                       building: str | None = None) -> StudyPlan:
    lo, hi = _to_min(earliest_start), _to_min(latest_start)
    if lo > hi:
        raise ServiceError("INVALID_WINDOW", f"earliest_start {earliest_start} is after latest_start {latest_start}.",
                           "Swap the two times.")
    steps: list[PlanStep] = []
    checked = 0
    for start_min in range(lo, hi + 1, 30):
        start = _fmt(start_min)
        try:
            found = search_rooms(day, start, duration, group_size, features, building)
        except ServiceError as err:
            # A slot that runs past closing or is already over is skipped, not fatal.
            if err.code in {"OUTSIDE_OPENING_HOURS", "TIME_IN_PAST"}:
                continue
            raise
        checked += 1
        steps.append(PlanStep(tool="search_rooms", summary=f"{start}-{found.end}: {found.count} free room(s)"))
        for candidate in found.rooms:
            try:
                booking = book_room(candidate.room_id, day, start, duration, holder, purpose)
            except ServiceError as err:
                if err.code == "ROOM_UNAVAILABLE":  # lost a race; try the next room
                    steps.append(PlanStep(tool="book_room", summary=f"{candidate.room_id} taken meanwhile, retrying"))
                    continue
                raise
            steps.append(PlanStep(tool="book_room", summary=f"booked {booking.room_id} as {booking.booking_id}"))
            spare = candidate.fit_score
            reason = (f"Earliest start in your window with a free room; {candidate.room_id} seats "
                      f"{candidate.capacity}, the tightest fit for {group_size} "
                      f"({spare} spare seat{'s' if spare != 1 else ''}).")
            return StudyPlan(booking=booking, reason=reason, slots_checked=checked, steps=steps)
    raise ServiceError("NO_AVAILABILITY",
                       f"No room for {group_size} with {list(features) or 'no special features'} is free for "
                       f"{duration} min starting between {earliest_start} and {latest_start} on {day}.",
                       "Widen the time window, drop a feature, or try another day.")
