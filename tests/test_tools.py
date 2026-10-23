"""Functional tests for the three tools and the resources, through the MCP protocol."""

import json

import pytest

from studyspot import service
from studyspot.service import ServiceError

from .conftest import TOMORROW, USER, booking_count, call, error_text

SLOT = dict(date=TOMORROW, start="14:00", duration_minutes=60)


# ---------------------------------------------------------------- Data tool


async def test_search_returns_tightest_fit_first(client):
    r = await call(client, "search_rooms", **SLOT, group_size=4)
    out = r.structured_content
    assert not r.is_error and out["count"] == len(out["rooms"]) == 7
    fits = [room["fit_score"] for room in out["rooms"]]
    assert fits == sorted(fits) and fits[0] == 0
    assert all(room["capacity"] >= 4 for room in out["rooms"])


async def test_search_filters_features_and_building(client):
    r = await call(client, "search_rooms", **SLOT, group_size=2, features=["quiet"],
                   building="Saab Medical Library")
    ids = [room["room_id"] for room in r.structured_content["rooms"]]
    assert ids == ["SML-012", "SML-114"]


async def test_search_excludes_overlapping_bookings(client):
    service.book_room("ENG-110", TOMORROW, "13:30", 60, "someone", "lab prep")  # overlaps 14:00-14:30
    service.book_room("JAF-204", TOMORROW, "15:00", 60, "other", "back to back")  # touches, no overlap
    ids = {room["room_id"] for room in (await call(client, "search_rooms", **SLOT, group_size=4)).structured_content["rooms"]}
    assert "ENG-110" not in ids and "JAF-204" in ids


async def test_search_is_read_only(client):
    before = booking_count()
    await call(client, "search_rooms", **SLOT, group_size=4)
    assert booking_count() == before


async def test_search_empty_result_is_not_an_error(client):
    r = await call(client, "search_rooms", **SLOT, group_size=12)
    assert not r.is_error and r.structured_content["count"] == 0


@pytest.mark.parametrize("args, code", [
    (dict(date="2026-10-01", start="14:00"), "DATE_IN_PAST"),
    (dict(date="2026-10-22", start="09:00"), "TIME_IN_PAST"),
    (dict(date="2026-11-10", start="14:00"), "OUTSIDE_BOOKING_WINDOW"),
    (dict(date="2026-02-30", start="14:00"), "INVALID_DATE"),
    (dict(date=TOMORROW, start="21:30"), "OUTSIDE_OPENING_HOURS"),
])
async def test_search_predictable_failures(client, args, code):
    r = await call(client, "search_rooms", **args, duration_minutes=60, group_size=2)
    assert error_text(r).startswith(code)
    assert "Hint:" in error_text(r)


@pytest.mark.parametrize("bad", [
    dict(start="14:15"),            # not on a half hour
    dict(start="07:30"),            # before opening
    dict(duration_minutes=45),      # not a multiple of 30
    dict(duration_minutes=240),     # too long
    dict(group_size=0),
    dict(group_size="4"),           # strict: no string-to-int coercion
    dict(features=["sauna"]),       # not in the enum
    dict(building="Main Gate"),
    dict(date="23/10/2026"),
])
async def test_schema_rejects_bad_arguments(client, bad):
    args = dict(**SLOT, group_size=2) | bad
    assert (await call(client, "search_rooms", **args)).is_error


# ---------------------------------------------------------------- Action tool


async def test_book_room_creates_booking_for_signed_in_user(client):
    r = await call(client, "book_room", room_id="JAF-204", **SLOT, purpose="EECE 503P project")
    b = r.structured_content
    assert not r.is_error and b["status"] == "confirmed" and b["holder"] == USER
    assert b["end"] == "15:00" and b["booking_id"].startswith("BK-")
    back = json.loads((await client.read_resource(f"studyspot://bookings/{b['booking_id']}"))[0].text)
    assert back == b


async def test_holder_cannot_be_chosen_by_the_model(client):
    r = await call(client, "book_room", room_id="JAF-204", **SLOT, purpose="study", holder="someone.else")
    assert r.is_error and booking_count() == 0


async def test_double_booking_is_refused(client):
    service.book_room("JAF-204", TOMORROW, "14:30", 30, "other", "taken")
    assert error_text(await call(client, "book_room", room_id="JAF-204", **SLOT, purpose="study")).startswith("ROOM_UNAVAILABLE")


async def test_unknown_room(client):
    assert error_text(await call(client, "book_room", room_id="XYZ-999", **SLOT, purpose="study")).startswith("ROOM_NOT_FOUND")


async def test_one_room_at_a_time_per_student(client):
    await call(client, "book_room", room_id="JAF-204", **SLOT, purpose="study")
    r = await call(client, "book_room", room_id="ENG-110", **SLOT, purpose="study")
    assert error_text(r).startswith("HOLDER_CONFLICT")


async def test_daily_quota(client):
    await call(client, "book_room", room_id="JAF-204", date=TOMORROW, start="10:00", duration_minutes=180, purpose="long")
    r = await call(client, "book_room", room_id="JAF-204", date=TOMORROW, start="14:00", duration_minutes=30, purpose="more")
    assert error_text(r).startswith("QUOTA_EXCEEDED")


def test_concurrent_bookings_cannot_both_win():
    import threading
    wins, errors = [], []

    def attempt(i):
        try:
            wins.append(service.book_room("JAF-204", TOMORROW, "14:00", 60, f"user{i}", "race"))
        except ServiceError as e:
            errors.append(e.code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1 and set(errors) == {"ROOM_UNAVAILABLE"}


# ---------------------------------------------------------------- Orchestration tool

PLAN = dict(date=TOMORROW, earliest_start="14:00", latest_start="16:00", duration_minutes=60, purpose="group study")


async def test_plan_books_earliest_tightest_fit(client):
    r = await call(client, "plan_study_session", **PLAN, group_size=5)
    p = r.structured_content
    assert not r.is_error
    assert p["booking"]["start"] == "14:00" and p["booking"]["room_id"] == "ENG-120"  # 6 seats, lowest id of the 6s
    assert [s["tool"] for s in p["steps"]] == ["search_rooms", "book_room"]
    assert booking_count() == 1


async def test_plan_moves_to_later_slot_when_full(client):
    for room, who in [("ENG-215", "a"), ("JAF-305", "b")]:
        service.book_room(room, TOMORROW, "14:00", 60, who, "busy")
    r = await call(client, "plan_study_session", **PLAN, group_size=8)
    p = r.structured_content
    assert p["booking"]["start"] == "15:00" and p["slots_checked"] == 3  # 14:00, 14:30 full; 15:00 free


async def test_plan_respects_features(client):
    r = await call(client, "plan_study_session", **PLAN, group_size=2, features=["quiet", "accessible"])
    assert r.structured_content["booking"]["room_id"] == "SML-114"


async def test_plan_retries_next_room_after_losing_a_race(client, monkeypatch):
    real = service.book_room
    calls = {"n": 0}

    def flaky(room_id, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ServiceError("ROOM_UNAVAILABLE", f"{room_id} taken")
        return real(room_id, *a, **k)

    monkeypatch.setattr(service, "book_room", flaky)
    p = (await call(client, "plan_study_session", **PLAN, group_size=4)).structured_content
    assert [s["tool"] for s in p["steps"]] == ["search_rooms", "book_room", "book_room"]
    assert p["booking"]["room_id"] == "JAF-204"  # second-tightest after ENG-110


async def test_plan_no_availability(client):
    assert error_text(await call(client, "plan_study_session", **PLAN, group_size=11)).startswith("NO_AVAILABILITY")
    assert booking_count() == 0


async def test_plan_invalid_window(client):
    r = await call(client, "plan_study_session", **(PLAN | dict(earliest_start="17:00")), group_size=2)
    assert error_text(r).startswith("INVALID_WINDOW")


async def test_plan_skips_slots_that_run_past_closing(client):
    r = await call(client, "plan_study_session", date=TOMORROW, earliest_start="20:30", latest_start="21:30",
                   duration_minutes=90, group_size=2, purpose="late study")
    assert r.structured_content["booking"]["start"] == "20:30"


async def test_plan_propagates_quota_error(client):
    service.book_room("JAF-101", TOMORROW, "09:00", 180, USER, "morning")
    r = await call(client, "plan_study_session", **PLAN, group_size=2)
    assert error_text(r).startswith("QUOTA_EXCEEDED")


# ---------------------------------------------------------------- Resources


async def test_resources(client):
    rooms = json.loads((await client.read_resource("studyspot://rooms"))[0].text)
    assert len(rooms) == 9
    room = json.loads((await client.read_resource("studyspot://rooms/JAF-210"))[0].text)
    assert room["capacity"] == 6
    assert "180 minutes" in (await client.read_resource("studyspot://policies"))[0].text
