"""The deterministic, stateful world one person lives in, and the tools over it.

The world holds the person's mail, calendar and files. Tools read and change it;
nothing here calls a model. A scenario builds the initial world, and graders
compare the world before and after a session.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field

from sereno.tools import Tool, ToolError


class Person(BaseModel):
    name: str
    email: str


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


class Event(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    participants: list[str] = []
    location: str = ""
    description: str = ""


class File(BaseModel):
    path: str
    content: str


class Mail(BaseModel):
    emails: list[Email] = []


class Calendar(BaseModel):
    events: list[Event] = []


class Files(BaseModel):
    files: list[File] = []


class World(BaseModel):
    today: date
    now: datetime
    owner: Person
    mail: Mail = Mail()
    calendar: Calendar = Calendar()
    files: Files = Files()

    def snapshot(self) -> dict:
        return self.model_dump(mode="json")

    def file(self, path: str) -> File | None:
        return next((f for f in self.files.files if f.path == path), None)


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


def _event_view(e: Event) -> dict:
    return {
        "id": e.id,
        "title": e.title,
        "start": e.start.isoformat(timespec="minutes"),
        "end": e.end.isoformat(timespec="minutes"),
        "participants": e.participants,
        "location": e.location,
    }


class SearchEmailsArgs(BaseModel):
    query: str = Field(description="Words to look for in sender, subject and body. Empty returns the newest emails.")
    folder: str = Field("inbox", description="'inbox', 'sent' or 'all'.")


def search_emails(world: World, args: SearchEmailsArgs) -> list[dict]:
    words = args.query.lower().split()
    found = [
        e
        for e in world.mail.emails
        if (args.folder == "all" or e.folder == args.folder)
        and all(w in f"{e.sender} {e.subject} {e.body}".lower() for w in words)
    ]
    found.sort(key=lambda e: e.sent_at, reverse=True)
    return [_email_header(e) for e in found[:10]]


class ReadEmailArgs(BaseModel):
    email_id: str = Field(description="The id returned by search_emails.")


def read_email(world: World, args: ReadEmailArgs) -> dict:
    email = next((e for e in world.mail.emails if e.id == args.email_id), None)
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
    missing = [p for p in args.attachments if world.file(p) is None]
    if missing:
        raise ToolError(f"No such file: {', '.join(missing)}.")
    email = Email(
        id=f"sent-{sum(e.folder == 'sent' for e in world.mail.emails) + 1}",
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
    world.mail.emails.append(email)
    return {"status": "sent", "id": email.id}


class ListEventsArgs(BaseModel):
    start_date: date = Field(description="First day to list, YYYY-MM-DD.")
    end_date: date = Field(description="Last day to list (inclusive), YYYY-MM-DD.")


def list_events(world: World, args: ListEventsArgs) -> list[dict]:
    if args.end_date < args.start_date:
        raise ToolError("end_date is before start_date.")
    events = [e for e in world.calendar.events if args.start_date <= e.start.date() <= args.end_date]
    events.sort(key=lambda e: e.start)
    return [_event_view(e) for e in events]


class CreateEventArgs(BaseModel):
    title: str
    start: datetime = Field(description="Start, YYYY-MM-DDTHH:MM.")
    end: datetime = Field(description="End, YYYY-MM-DDTHH:MM.")
    participants: list[str] = Field([], description="Email addresses to invite.")
    location: str = ""
    description: str = ""


def create_event(world: World, args: CreateEventArgs) -> dict:
    if args.end <= args.start:
        raise ToolError("end must be after start.")
    event = Event(id=f"evt-{len(world.calendar.events) + 1}", **args.model_dump())
    world.calendar.events.append(event)
    return {"status": "created", **_event_view(event)}


class ListFilesArgs(BaseModel):
    directory: str = Field("", description="Directory prefix, for example 'contracts/'. Empty lists everything.")


def list_files(world: World, args: ListFilesArgs) -> list[str]:
    return sorted(f.path for f in world.files.files if f.path.startswith(args.directory))


class ReadFileArgs(BaseModel):
    path: str


def read_file(world: World, args: ReadFileArgs) -> str:
    file = world.file(args.path)
    if file is None:
        raise ToolError(f"No such file: {args.path}.")
    return file.content


TOOLS = [
    Tool(
        "search_emails",
        "mail",
        "Search the user's emails. Returns headers only; use read_email for the body.",
        SearchEmailsArgs,
        search_emails,
    ),
    Tool("read_email", "mail", "Read one email in full.", ReadEmailArgs, read_email),
    Tool("send_email", "mail", "Send an email from the user's address.", SendEmailArgs, send_email, writes=True),
    Tool("list_events", "calendar", "List the user's calendar events between two dates.", ListEventsArgs, list_events),
    Tool(
        "create_event",
        "calendar",
        "Create an event in the user's calendar and invite participants.",
        CreateEventArgs,
        create_event,
        writes=True,
    ),
    Tool("list_files", "files", "List the paths of the user's files.", ListFilesArgs, list_files),
    Tool("read_file", "files", "Read a text file from the user's files.", ReadFileArgs, read_file),
]
