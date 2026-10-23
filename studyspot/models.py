"""Typed inputs and outputs shared by the server, the service layer and the tests.

Every value that crosses the MCP boundary is declared here, so the JSON schema
the agent sees is generated from one source of truth.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Constrained scalar types (validated before any tool code runs)
# ---------------------------------------------------------------------------

DateStr = Annotated[
    str,
    Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Calendar date in ISO format YYYY-MM-DD (Beirut local time).",
        examples=["2026-10-27"],
    ),
]

TimeStr = Annotated[
    str,
    Field(
        pattern=r"^(0[89]|1\d|2[01]):(00|30)$",
        description="Start time HH:MM on a half-hour boundary, between 08:00 and 21:30.",
        examples=["14:00"],
    ),
]

Duration = Annotated[
    int,
    Field(ge=30, le=180, multiple_of=30, description="Length in minutes: 30, 60, ..., 180."),
]

GroupSize = Annotated[int, Field(ge=1, le=12, description="Number of people, 1 to 12.")]

RoomId = Annotated[
    str,
    Field(pattern=r"^[A-Z]{2,4}-\d{3}$", description="Room identifier, e.g. 'JAF-204'."),
]

BookingId = Annotated[
    str,
    Field(pattern=r"^BK-[0-9A-F]{8}$", description="Booking identifier, e.g. 'BK-1A2B3C4D'."),
]

Purpose = Annotated[
    str,
    Field(
        min_length=3,
        max_length=120,
        description="Short plain-text reason for the booking, shown only to its owner.",
        examples=["EECE 503P project meeting"],
    ),
]

Feature = Literal["whiteboard", "screen", "power_outlets", "quiet", "accessible"]
Building = Literal["Jafet Library", "Engineering Library", "Saab Medical Library"]


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Room(_Out):
    room_id: str
    name: str
    building: str
    floor: int
    capacity: int
    features: list[str]


class AvailableRoom(Room):
    fit_score: int = Field(
        description="Empty seats left for this group (capacity - group size). Lower is a tighter fit."
    )


class SearchResult(_Out):
    """Output of the Data tool."""

    date: str
    start: str
    end: str
    group_size: int
    rooms: list[AvailableRoom] = Field(description="Free rooms, tightest fit first.")
    count: int


class Booking(_Out):
    """Output of the Action tool."""

    booking_id: str
    room_id: str
    room_name: str
    building: str
    date: str
    start: str
    end: str
    holder: str
    purpose: str
    status: Literal["confirmed"]


class PlanStep(_Out):
    tool: Literal["search_rooms", "book_room"]
    summary: str


class StudyPlan(_Out):
    """Output of the Orchestration tool."""

    booking: Booking
    reason: str = Field(description="Why this room and time were chosen.")
    slots_checked: int
    steps: list[PlanStep] = Field(description="The Data/Action calls made, in order.")
