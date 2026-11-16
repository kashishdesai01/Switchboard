"""Tool definitions the model sees, and the system prompt that describes the clinic.

Every guarantee (verification, proxy policy, read-back before a write, full birth
dates) is enforced in ``AgentCall``'s tool code, not here. The descriptions only
tell the model how to use the tools well.
"""

from __future__ import annotations

from datetime import datetime

from switchboard.config import Clinic

DATE = r"^\d{4}-\d{2}-\d{2}$"
CLOCK = r"^\d{2}:\d{2}$"


def tool_definitions(clinic: Clinic) -> list[dict]:
    providers = sorted(clinic.providers)
    locations = sorted(clinic.locations)
    find_times: dict = {
        "visit_type": {
            "type": "string",
            "enum": ["follow-up", "first visit"],
            "description": "Use first visit for a new patient or a first appointment, otherwise follow-up.",
        },
        "day": {"type": "string", "pattern": DATE, "description": "A specific day, YYYY-MM-DD."},
        "provider": {"type": "string", "enum": providers, "description": "Provider key."},
        "period": {"type": "string", "enum": ["morning", "afternoon"]},
        "time": {
            "type": "string",
            "pattern": CLOCK,
            "description": "Exact time asked for, 24h HH:MM.",
        },
        "after": {"type": "string", "pattern": CLOCK, "description": "Earliest start, 24h HH:MM."},
        "before": {"type": "string", "pattern": CLOCK, "description": "Latest start, 24h HH:MM."},
        "more": {
            "type": "boolean",
            "description": "True to get further openings beyond the ones already offered.",
        },
        "appointment": {
            "type": "integer",
            "description": "When rescheduling: the appointment number from list_appointments being moved.",
        },
    }
    if locations:
        find_times["location"] = {
            "type": "string",
            "enum": locations,
            "description": "Location key.",
        }
    return [
        {
            "name": "verify_patient",
            "description": (
                "Look up the patient by full name and full date of birth. Required before any "
                "times, appointments or changes. Ask the caller for anything missing; never guess. "
                "for_self is false when the caller is acting for someone else; then "
                "permission_stated must be true only if the caller said they have the patient's "
                "permission. request is what the caller wants to do."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "full_name": {
                        "type": "string",
                        "description": "Patient's first and last name.",
                    },
                    "birth_date": {
                        "type": "string",
                        "pattern": DATE,
                        "description": "Patient's full birth date as YYYY-MM-DD.",
                    },
                    "for_self": {"type": "boolean"},
                    "permission_stated": {"type": "boolean"},
                    "request": {"type": "string", "enum": ["book", "reschedule", "cancel"]},
                },
                "required": ["full_name", "birth_date", "for_self", "permission_stated", "request"],
            },
        },
        {
            "name": "find_times",
            "description": (
                "Find open appointment times for the verified patient. Returns numbered options. "
                "Offer them briefly, at most three, and let the caller choose."
            ),
            "input_schema": {
                "type": "object",
                "properties": find_times,
                "required": ["visit_type"],
            },
        },
        {
            "name": "list_appointments",
            "description": "List the verified patient's upcoming appointments, numbered.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "propose_booking",
            "description": (
                "Hold an offered time and get the read-back for booking it. Say the read_back to "
                "the caller exactly and wait for their answer. Nothing is booked yet."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"option": {"type": "integer", "description": "Option number."}},
                "required": ["option"],
            },
        },
        {
            "name": "propose_reschedule",
            "description": (
                "Hold an offered time for moving an appointment and get the read-back. Say it "
                "exactly and wait for the caller's answer."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "appointment": {"type": "integer", "description": "Appointment number."},
                    "option": {"type": "integer", "description": "New time option number."},
                },
                "required": ["appointment", "option"],
            },
        },
        {
            "name": "propose_cancel",
            "description": "Get the read-back for cancelling an appointment. Say it exactly and wait.",
            "input_schema": {
                "type": "object",
                "properties": {"appointment": {"type": "integer"}},
                "required": ["appointment"],
            },
        },
        {
            "name": "confirm_change",
            "description": (
                "Make the proposed change. Only call it when the caller clearly said yes to the "
                "read-back in their latest reply. A hedged or conditional answer is not a yes."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "decline_change",
            "description": "The caller does not want the proposed change: release it, change nothing.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "transfer_to_staff",
            "description": (
                "Hand the call to the front desk: the caller asks for a person, or asks for "
                "something these tools can't do (medical advice, refills, billing, results)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
        {
            "name": "report_emergency",
            "description": "The caller describes a medical emergency happening now.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "end_call",
            "description": "The caller has nothing else and is saying goodbye.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def system_prompt(clinic: Clinic, now: datetime, greeting: str) -> str:
    today = now.astimezone(clinic.timezone)
    providers = ", ".join(f"{name} (key {key})" for key, name in clinic.providers.items())
    locations = (
        ", ".join(f"{name} (key {key})" for key, name in clinic.locations.items()) or "one location"
    )
    return f"""You are the phone assistant for {clinic.name}. You help callers book, reschedule or cancel appointments.
Today is {today:%A, %B} {today.day}, {today.year}. Clinic time zone: {clinic.timezone.key}.
Hours: {clinic.weekday_hours}. Locations: {locations}. Providers: {providers}.
Accepted insurance: {", ".join(clinic.accepted_payers)}. Staff can confirm other plans.

You already greeted the caller with: "{greeting}"

How to talk: your words are spoken aloud. Keep replies to one or two short sentences and ask one question at a time. No lists, markdown, or symbols. Say times the way the tools give them.

How to work:
- Find out what the caller wants and who the patient is. The caller is the patient unless they mention someone else (my uncle, my son, for her); don't ask who it's for otherwise. If they act for someone else, ask whether they have that person's permission.
- Get the patient's full name and full date of birth (day, month and year), then call verify_patient. Never guess missing parts.
- Once verified, call find_times (booking) or list_appointments (reschedule or cancel) in the same turn, using any day, time, provider or location the caller already gave. If they gave none, search for the soonest times rather than asking. Don't ask about the visit type: it's a follow-up unless the caller says it's a first visit, and a new patient always gets a first visit.
- Offer the options, and when the caller chooses, call the matching propose tool. When only one appointment fits a reschedule or cancel request, don't ask which one: the read_back is the confirmation.
- Read the read_back to the caller exactly as given and wait. If their answer isn't a clear yes or no, briefly answer or clarify and say the same read_back again. Only after the caller clearly says yes in their next reply, call confirm_change. If they say no, call decline_change. If they want something different, search again.
- Callers may correct themselves, answer in pieces or change their mind at any point. Use the latest information. A reply to a read-back that gives a different name, birth date, day or time is a correction, not a question: redo the steps with the corrected detail (verify_patient again for a name or birth date, then find_times and a propose tool) and read the new read_back. Don't ask the caller to choose between the old and new detail.
- Never invent appointments, times or patient details. Never reveal whether a record exists beyond what the tools say.
- If a tool returns an error, follow its instruction.
- After a change is made, tell the caller the result and ask if there is anything else."""
