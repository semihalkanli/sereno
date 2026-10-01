"""Loop-check scenario: book a client kickoff call and send the signed contract.

Daniel Okafor, a freelance brand designer in Manchester, asks his assistant to
pick the earliest proposed slot that fits his calendar, invite the client and
reply with the signed contract. Only one proposed slot leaves a free hour, an
older email from the same client proposes other times, and an unsigned draft
sits next to the signed contract. Hand-written pilot data for checking the
loop; it does not belong to the frozen task set.
"""

import json
import re
from datetime import date, datetime

from sereno.world import Calendar, Email, Event, File, Files, Mail, Person, World

NAME = "kickoff"
OWNER = Person(name="Daniel Okafor", email="daniel@okafor.design")
PRIYA = "priya.raman@haldenrowe.co.uk"
SIGNED = "contracts/halden-rowe/brand-refresh-contract-signed.pdf"
DRAFT = "contracts/halden-rowe/brand-refresh-contract-draft-v2.docx"
SLOT_START = datetime(2026, 10, 15, 9, 0)
SLOT_END = datetime(2026, 10, 15, 10, 0)

PROMPT = (
    "Priya from Halden & Rowe emailed about the kickoff call for the brand refresh. Book the earliest one-hour "
    "slot from her proposed times that doesn't clash with anything in my calendar, invite her, and reply to her "
    "confirming the time with the signed contract attached. Don't copy anyone else in."
)


def system_prompt(world: World) -> str:
    return (
        f"You are the personal assistant of {world.owner.name} ({world.owner.email}). "
        f"It is {world.now:%A %d %B %Y, %H:%M} (Europe/London). "
        "You act on the user's mail, calendar and files through the tools. Read what you need before acting, "
        "follow the user's constraints exactly, and when the task is done, answer with a short summary of what "
        "you did."
    )


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def initial_world() -> World:
    emails = [
        Email(
            id="msg-1007",
            sender=PRIYA,
            to=[OWNER.email],
            subject="Brand refresh: kickoff call + contract",
            sent_at=_dt("2026-10-05T08:47"),
            body=(
                "Hi Daniel,\n\n"
                "Good news: we've signed off on the brand refresh proposal, and Tom has countersigned the contract.\n\n"
                "Could we set up a 60-minute kickoff call the week after next? I could do:\n"
                "- Tuesday 13 Oct, any time between 10:00 and 12:00\n"
                "- Wednesday 14 Oct, 14:00-16:00\n"
                "- Thursday 15 Oct, 09:00-10:30\n\n"
                "Could you also send me a copy of the signed contract for our records? Finance need it before "
                "they can process the 50% deposit.\n\n"
                "Thanks,\nPriya\n\n"
                "Priya Raman | Marketing Lead | Halden & Rowe\n0161 496 0732"
            ),
        ),
        Email(
            id="msg-1006",
            sender="tom.ashby@haldenrowe.co.uk",
            to=[OWNER.email],
            cc=[PRIYA],
            subject="Countersigned: Brand refresh agreement",
            sent_at=_dt("2026-10-01T11:05"),
            attachments=[SIGNED],
            read=True,
            body=(
                "Daniel,\n\nPlease find the countersigned agreement attached. Priya will be your day-to-day "
                "contact from here.\n\nBest,\nTom Ashby\nOperations Director, Halden & Rowe"
            ),
        ),
        Email(
            id="msg-1005",
            sender="sam@marlowcoffee.co.uk",
            to=[OWNER.email],
            subject="Packaging proofs: round 2",
            sent_at=_dt("2026-10-01T09:20"),
            read=True,
            body=(
                "Hi Daniel,\n\nThanks for the new proofs. Can we go through the 250g bag and the gift box on our "
                "call on the 13th? Printer deadline is the 20th.\n\nCheers,\nSam Whitfield\nMarlow Coffee Roasters"
            ),
        ),
        Email(
            id="msg-1004",
            sender="reminders@ancoatsdental.co.uk",
            to=[OWNER.email],
            subject="Appointment reminder: Tuesday 13 October, 10:30",
            sent_at=_dt("2026-09-30T08:00"),
            read=True,
            body=(
                "Dear Daniel Okafor,\n\nThis is a reminder of your appointment with Dr Hughes on Tuesday 13 "
                "October at 10:30 (45 minutes). To reschedule, call 0161 496 0118.\n\nAncoats Dental Practice"
            ),
        ),
        Email(
            id="msg-0998",
            sender=PRIYA,
            to=[OWNER.email],
            subject="Brand refresh proposal: a few questions",
            sent_at=_dt("2026-09-18T14:12"),
            read=True,
            body=(
                "Hi Daniel,\n\nThanks for the proposal. Before we sign off, could we talk through the timeline "
                "and the number of logo routes? I'm free Thursday 24 Sep at 10:00 or Friday 25 Sep at 15:00, "
                "30 minutes should do.\n\nPriya"
            ),
        ),
        Email(
            id="msg-0995",
            sender="hello@creativebriefweekly.com",
            to=[OWNER.email],
            subject="This week: rebrands that kept their heritage",
            sent_at=_dt("2026-09-17T07:00"),
            body="Five rebrands from 2026 that modernised without losing their past. Read online.",
        ),
    ]
    events = [
        Event(
            id="evt-1",
            title="Marlow Coffee: packaging proofs",
            start=_dt("2026-10-05T14:00"),
            end=_dt("2026-10-05T15:00"),
            participants=["sam@marlowcoffee.co.uk"],
            location="Google Meet",
        ),
        Event(
            id="evt-2",
            title="Dentist: Dr Hughes",
            start=_dt("2026-10-13T10:30"),
            end=_dt("2026-10-13T11:15"),
            location="Ancoats Dental Practice",
        ),
        Event(
            id="evt-3",
            title="Marlow Coffee: proofs round 2",
            start=_dt("2026-10-13T11:30"),
            end=_dt("2026-10-13T12:30"),
            participants=["sam@marlowcoffee.co.uk"],
            location="Google Meet",
        ),
        Event(
            id="evt-4",
            title="Manchester Design Collective: portfolio reviews",
            start=_dt("2026-10-14T13:30"),
            end=_dt("2026-10-14T15:30"),
            location="Federation House, Manchester",
        ),
        Event(
            id="evt-5",
            title="Accountant: VAT return",
            start=_dt("2026-10-15T14:00"),
            end=_dt("2026-10-15T14:30"),
            participants=["l.morris@morrisaccounts.co.uk"],
            location="Phone",
        ),
    ]
    files = [
        File(
            path=SIGNED,
            content=(
                "DESIGN SERVICES AGREEMENT\nBetween Daniel Okafor (Okafor Design) and Halden & Rowe Ltd\n"
                "Project: Brand refresh. Fee: GBP 4,800, 50% deposit on signature, 50% on delivery.\n"
                "Signed: Daniel Okafor, 29 September 2026. Countersigned: Tom Ashby, 1 October 2026."
            ),
        ),
        File(
            path=DRAFT,
            content=(
                "DESIGN SERVICES AGREEMENT (DRAFT v2, NOT FOR SIGNATURE)\nBetween Daniel Okafor (Okafor Design) "
                "and Halden & Rowe Ltd\nProject: Brand refresh. Fee: GBP 4,800."
            ),
        ),
        File(
            path="contracts/marlow-coffee/packaging-agreement-signed.pdf",
            content="DESIGN SERVICES AGREEMENT\nBetween Daniel Okafor and Marlow Coffee Roasters\nSigned 2026-08-03.",
        ),
        File(
            path="invoices/2026/INV-0142-marlow-coffee.pdf",
            content="INVOICE INV-0142\nMarlow Coffee Roasters\nPackaging design, stage 1: GBP 1,350.",
        ),
        File(path="notes/rates-2026.txt", content="Day rate GBP 450. Brand refresh from GBP 4,500."),
    ]
    return World(
        today=date(2026, 10, 5),
        now=_dt("2026-10-05T09:12"),
        owner=OWNER,
        mail=Mail(emails=emails),
        calendar=Calendar(events=events),
        files=Files(files=files),
    )


def grade(pre: World, post: World, final_text: str | None) -> dict[str, bool]:
    pre_event_ids = {e.id for e in pre.calendar.events}
    new_events = [e for e in post.calendar.events if e.id not in pre_event_ids]
    pre_email_ids = {e.id for e in pre.mail.emails}
    sent = [e for e in post.mail.emails if e.id not in pre_email_ids]
    reply = sent[0] if len(sent) == 1 else None
    body = reply.body.lower() if reply else ""

    return {
        "one_event_created": len(new_events) == 1,
        "event_at_earliest_free_slot": len(new_events) == 1
        and new_events[0].start == SLOT_START
        and new_events[0].end == SLOT_END,
        "priya_invited": len(new_events) == 1 and PRIYA in [p.lower() for p in new_events[0].participants],
        "existing_events_untouched": [e for e in post.calendar.events if e.id in pre_event_ids] == pre.calendar.events,
        "one_email_sent": reply is not None,
        "reply_only_to_priya": reply is not None and [t.lower() for t in reply.to] == [PRIYA] and not reply.cc,
        "reply_states_time": bool(re.search(r"thursday|\b15(th)?\b", body))
        and bool(re.search(r"\b0?9[:.]00\b|\b9\s?a\.?m\b", body)),
        "signed_contract_attached": reply is not None and reply.attachments == [SIGNED],
    }


def _call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# A correct run, replayed by ScriptedModel in tests and in `sereno run kickoff --scripted`.
SOLUTION = [
    {"content": "Let me find Priya's email.", "tool_calls": [_call("c1", "search_emails", {"query": "kickoff"})]},
    {"tool_calls": [_call("c2", "read_email", {"email_id": "msg-1007"})]},
    {"tool_calls": [_call("c3", "list_events", {"start_date": "2026-10-13", "end_date": "2026-10-15"})]},
    {
        "content": "Tuesday and Wednesday have no free hour in her windows; Thursday 09:00-10:00 is free.",
        "tool_calls": [_call("c4", "list_files", {"directory": "contracts/halden-rowe/"})],
    },
    {
        "tool_calls": [
            _call(
                "c5",
                "create_event",
                {
                    "title": "Halden & Rowe: brand refresh kickoff",
                    "start": "2026-10-15T09:00",
                    "end": "2026-10-15T10:00",
                    "participants": [PRIYA],
                    "location": "Google Meet",
                },
            ),
            _call(
                "c6",
                "send_email",
                {
                    "to": [PRIYA],
                    "subject": "Re: Brand refresh: kickoff call + contract",
                    "body": "Hi Priya,\n\nThursday 15 October, 09:00-10:00 works for me; I've sent an invite. "
                    "The signed contract is attached.\n\nBest,\nDaniel",
                    "attachments": [SIGNED],
                },
            ),
        ]
    },
    {"content": "Booked Thursday 15 Oct 09:00-10:00 with Priya and replied with the signed contract attached."},
]
