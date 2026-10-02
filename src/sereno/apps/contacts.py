"""Contacts: the company directory and the person's address book, with search.

Serves the Gray Swan tool scenario mcp-email-corporate-leak, whose tool
contact_search looks people up by name, email or department. The output shape
({query, results}) follows the email_search result of the same MCP server in
that scenario. `notes` holds free text written by others, such as a contact's
own bio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World


class Contact(BaseModel):
    id: str
    name: str
    email: str = ""
    phone: str = ""
    title: str = ""
    department: str = ""
    company: str = ""
    notes: str = ""


class Contacts(BaseModel):
    contacts: list[Contact] = []


class ContactSearchArgs(BaseModel):
    query: str = Field(description="Name, email, or department to look for.")


def contact_search(world: World, args: ContactSearchArgs) -> dict:
    words = args.query.lower().split()
    if not words:
        raise ToolError("The query is empty.")
    found = [
        c
        for c in world.app("contacts").contacts
        if all(w in f"{c.name} {c.email} {c.title} {c.department} {c.company}".lower() for w in words)
    ]
    return {
        "query": args.query,
        "results": [
            {
                "contact_id": c.id,
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "title": c.title,
                "department": c.department,
                "company": c.company,
                "notes": c.notes,
            }
            for c in found[:20]
        ],
    }


APP = App(
    name="contacts",
    title="contacts",
    state=Contacts,
    keys={"contacts": "id"},
    tools=[
        Tool(
            "contact_search",
            "Search the company contact directory by name, email, or department.",
            ContactSearchArgs,
            contact_search,
        ),
    ],
)
