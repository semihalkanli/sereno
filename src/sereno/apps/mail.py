"""Mail: the person's mailbox, with search, read and send."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World


class Email(BaseModel):
    id: str
    sender: str
    to: list[str]
    cc: list[str] = []
    subject: str
    body: str
    sent_at: datetime
    folder: str = "inbox"
    attachments: list[str] = []
    read: bool = False


class Mail(BaseModel):
    emails: list[Email] = []


def _mail(world: World) -> Mail:
    return world.app("mail")


def _email_header(e: Email) -> dict:
    return {
        "id": e.id,
        "from": e.sender,
        "to": e.to,
        "subject": e.subject,
        "date": e.sent_at.isoformat(timespec="minutes"),
        "folder": e.folder,
        "attachments": e.attachments,
    }


class SearchEmailsArgs(BaseModel):
    query: str = Field(description="Words to look for in sender, subject and body. Empty returns the newest emails.")
    folder: str = Field("inbox", description="'inbox', 'sent' or 'all'.")


def search_emails(world: World, args: SearchEmailsArgs) -> list[dict]:
    words = args.query.lower().split()
    found = [
        e
        for e in _mail(world).emails
        if (args.folder == "all" or e.folder == args.folder)
        and all(w in f"{e.sender} {e.subject} {e.body}".lower() for w in words)
    ]
    found.sort(key=lambda e: e.sent_at, reverse=True)
    return [_email_header(e) for e in found[:10]]


class ReadEmailArgs(BaseModel):
    email_id: str = Field(description="The id returned by search_emails.")


def read_email(world: World, args: ReadEmailArgs) -> dict:
    email = next((e for e in _mail(world).emails if e.id == args.email_id), None)
    if email is None:
        raise ToolError(f"No email with id {args.email_id!r}.")
    email.read = True
    return {**_email_header(email), "cc": email.cc, "body": email.body}


class SendEmailArgs(BaseModel):
    to: list[str] = Field(description="Recipient email addresses.")
    subject: str
    body: str
    cc: list[str] = []
    attachments: list[str] = Field([], description="Paths of files from the user's files to attach.")


def send_email(world: World, args: SendEmailArgs) -> dict:
    if not args.to:
        raise ToolError("At least one recipient is required.")
    files = world.app("files", required=False)
    missing = [p for p in args.attachments if files is None or files.file(p) is None]
    if missing:
        raise ToolError(f"No such file: {', '.join(missing)}.")
    mail = _mail(world)
    email = Email(
        id=f"sent-{sum(e.folder == 'sent' for e in mail.emails) + 1}",
        sender=world.owner.email,
        to=args.to,
        cc=args.cc,
        subject=args.subject,
        body=args.body,
        sent_at=world.now,
        folder="sent",
        attachments=args.attachments,
        read=True,
    )
    mail.emails.append(email)
    return {"status": "sent", "id": email.id}


APP = App(
    name="mail",
    title="mail",
    state=Mail,
    keys={"emails": "id"},
    tools=[
        Tool(
            "search_emails",
            "mail",
            "Search the user's emails. Returns headers only; use read_email for the body.",
            SearchEmailsArgs,
            search_emails,
        ),
        Tool("read_email", "mail", "Read one email in full.", ReadEmailArgs, read_email),
        Tool("send_email", "mail", "Send an email from the user's address.", SendEmailArgs, send_email, writes=True),
    ],
)
