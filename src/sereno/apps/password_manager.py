"""Password manager: the person's vault of logins, notes, cards and identities.

Gray Swan has no tool surface for this app (its scenario keepass-config-trigger-
export drives a desktop vault through bash and screen clicks), and no Claude
plugin or connector exists, so the tools follow the vendor's official MCP
server (bitwarden/mcp-server 3db0397, src/tools/cli.ts): names, parameters and
the JSON the vault's command line prints. Names carry this app's prefix in the
MCP style, as the bare names (list, get, delete) are not unique across apps.

Kept: the personal vault tools. Left out: lock, unlock, sync and status (the
vault is open for the whole run), the organization and device approval tools
(one person, no organization), file Sends and attachments, and `get exposed`
(it asks an outside breach service). The vault is unlocked, so the agent can
read stored passwords, as with the real server.

Ids are deterministic UUIDs. `generate` draws from a generator seeded by the
world clock and a counter, so a repeated run gets the same passwords; the
passphrase word list is a short stand-in for the vendor's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.apps._common import has_words, iso_seconds
from sereno.tools import NoArgs, Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World

ItemType = Literal[1, 2, 3, 4]
TYPE_NAMES = {1: "login", 2: "secureNote", 3: "card", 4: "identity"}


class Uri(BaseModel):
    uri: str
    match: int | None = None


class Login(BaseModel):
    username: str | None = None
    password: str | None = None
    uris: list[Uri] = []
    totp: str | None = None


class SecureNote(BaseModel):
    type: int = 0


class Card(BaseModel):
    cardholderName: str | None = None
    number: str | None = None
    brand: str | None = None
    expMonth: str | None = None
    expYear: str | None = None
    code: str | None = None


class Identity(BaseModel):
    title: str | None = None
    firstName: str | None = None
    middleName: str | None = None
    lastName: str | None = None
    address1: str | None = None
    address2: str | None = None
    address3: str | None = None
    city: str | None = None
    state: str | None = None
    postalCode: str | None = None
    country: str | None = None
    company: str | None = None
    email: str | None = None
    phone: str | None = None
    ssn: str | None = None
    username: str | None = None
    passportNumber: str | None = None
    licenseNumber: str | None = None


class Item(BaseModel):
    id: str
    type: ItemType
    name: str
    notes: str | None = None
    folderId: str | None = None
    favorite: bool = False
    login: Login | None = None
    secureNote: SecureNote | None = None
    card: Card | None = None
    identity: Identity | None = None
    creationDate: datetime | None = None
    revisionDate: datetime | None = None
    deletedDate: datetime | None = None


class Folder(BaseModel):
    id: str
    name: str


class Send(BaseModel):
    id: str
    accessId: str
    name: str
    notes: str | None = None
    text: str
    hidden: bool = False
    password: str | None = None
    maxAccessCount: int | None = None
    accessCount: int = 0
    expirationDate: datetime | None = None
    deletionDate: datetime | None = None
    revisionDate: datetime | None = None
    disabled: bool = False


class PasswordManager(BaseModel):
    items: list[Item] = []
    folders: list[Folder] = []
    sends: list[Send] = []
    send_url: str = "https://vault.example.com/#/send/"
    """Base of a Send's public link; the access id follows it."""
    generated: int = 0
    """How many values `generate` has made, for its seed."""


def _vault(world: World) -> PasswordManager:
    return world.app("password_manager")


def _new_id(kind: str, taken: list[str]) -> str:
    n = len(taken) + 1
    while (made := str(uuid.uuid5(uuid.NAMESPACE_URL, f"sereno:{kind}:{n}"))) in taken:
        n += 1
    return made


def _item_view(item: Item) -> dict:
    view = {
        "object": "item",
        "id": item.id,
        "organizationId": None,
        "folderId": item.folderId,
        "type": item.type,
        "reprompt": 0,
        "name": item.name,
        "notes": item.notes,
        "favorite": item.favorite,
    }
    part = TYPE_NAMES[item.type]
    view[part] = getattr(item, part).model_dump() if getattr(item, part) else None
    view.update(
        {
            "collectionIds": [],
            "revisionDate": iso_seconds(item.revisionDate),
            "creationDate": iso_seconds(item.creationDate),
            "deletedDate": iso_seconds(item.deletedDate),
        }
    )
    return view


def _send_view(vault: PasswordManager, send: Send) -> dict:
    return {
        "object": "send",
        "id": send.id,
        "accessId": send.accessId,
        "accessUrl": f"{vault.send_url}{send.accessId}",
        "name": send.name,
        "notes": send.notes,
        "type": 0,
        "text": {"text": send.text, "hidden": send.hidden},
        "maxAccessCount": send.maxAccessCount,
        "accessCount": send.accessCount,
        "revisionDate": iso_seconds(send.revisionDate),
        "deletionDate": iso_seconds(send.deletionDate),
        "expirationDate": iso_seconds(send.expirationDate),
        "passwordSet": send.password is not None,
        "disabled": send.disabled,
        "hideEmail": False,
    }


def _null_filter(value: str | None, wanted: str) -> bool:
    """The command line's filter: an id, or the literals "null" and "notnull"."""
    if wanted == "null":
        return value is None
    if wanted == "notnull":
        return value is not None
    return value == wanted


def _item_text(item: Item) -> list[str]:
    fields = [item.name]
    if item.login:
        fields += [item.login.username or "", *(u.uri for u in item.login.uris)]
    return fields


def _find_item(vault: PasswordManager, key: str) -> Item:
    """An item by id, or by search term when exactly one live item matches."""
    for item in vault.items:
        if item.id == key:
            return item
    words = key.lower().split()
    found = [i for i in vault.items if i.deletedDate is None and words and has_words(words, *_item_text(i))]
    if not found:
        raise ToolError("Not found.")
    if len(found) > 1:
        ids = "\n".join(i.id for i in found)
        raise ToolError(f"More than one result was found. Try getting a specific object by `id` instead.\n{ids}")
    return found[0]


def _find_folder(vault: PasswordManager, key: str) -> Folder:
    for folder in vault.folders:
        if folder.id == key or folder.name.lower() == key.lower():
            return folder
    raise ToolError("Not found.")


def _date(text: str | None, field: str) -> datetime | None:
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.removesuffix("Z")).replace(tzinfo=None)
    except ValueError:
        raise ToolError(f"{field} {text!r} is not ISO 8601.") from None


def _totp(secret: str, now: datetime) -> str:
    """RFC 6238 code (SHA-1, 30 seconds, 6 digits) for a base32 secret or an otpauth:// link.

    The world clock is read as UTC, so the code does not depend on the machine's timezone.
    """
    if secret.startswith("otpauth://"):
        secret = parse_qs(urlsplit(secret).query).get("secret", [""])[0]
    key = secret.replace(" ", "").upper()
    try:
        raw = base64.b32decode(key + "=" * (-len(key) % 8))
    except ValueError:
        raise ToolError("Invalid TOTP secret.") from None
    counter = int(now.replace(tzinfo=UTC).timestamp()) // 30
    digest = hmac.new(raw, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


class ListArgs(BaseModel):
    type: Literal["items", "folders"] = Field(description="Type of items to list (items, folders)")
    search: str | None = Field(None, description="Optional search term to filter results")
    url: str | None = Field(
        None, description='Filter items by URL (items only, supports "null" and "notnull" literals)'
    )
    folderid: str | None = Field(
        None, description='Filter items by folder ID (items only, supports "null" and "notnull" literals)'
    )
    trash: bool | None = Field(None, description="Filter for items in trash (items only)")


def password_manager_list(world: World, args: ListArgs) -> list[dict]:
    vault = _vault(world)
    words = (args.search or "").lower().split()
    if args.type == "folders":
        folders = [f for f in vault.folders if has_words(words, f.name)]
        found: list[dict] = [{"object": "folder", "id": f.id, "name": f.name} for f in folders]
        if has_words(words, "No Folder"):
            found.append({"object": "folder", "id": None, "name": "No Folder"})
        return found
    items = [i for i in vault.items if (i.deletedDate is not None) == bool(args.trash)]
    if words:
        items = [i for i in items if has_words(words, *_item_text(i))]
    if args.folderid is not None:
        items = [i for i in items if _null_filter(i.folderId, args.folderid)]
    if args.url is not None:
        if args.url in ("null", "notnull"):
            items = [i for i in items if _null_filter((i.login and i.login.uris) or None, args.url)]
        else:
            items = [i for i in items if i.login and any(args.url.lower() in u.uri.lower() for u in i.login.uris)]
    return [_item_view(i) for i in items]


class GetArgs(BaseModel):
    object: Literal["item", "username", "password", "uri", "totp", "notes", "folder"] = Field(
        description="Type of object to retrieve"
    )
    id: str = Field(description="ID or search term for the object")


def password_manager_get(world: World, args: GetArgs) -> dict | str:
    vault = _vault(world)
    if args.object == "folder":
        folder = _find_folder(vault, args.id)
        return {"object": "folder", "id": folder.id, "name": folder.name}
    item = _find_item(vault, args.id)
    if args.object == "item":
        return _item_view(item)
    if args.object == "notes":
        if not item.notes:
            raise ToolError("No notes available for this item.")
        return item.notes
    login = item.login
    if login is None:
        raise ToolError(f"No {args.object} available for this item.")
    if args.object == "username":
        value = login.username
    elif args.object == "password":
        value = login.password
    elif args.object == "uri":
        value = login.uris[0].uri if login.uris else None
    else:
        value = _totp(login.totp, world.now) if login.totp else None
    if not value:
        raise ToolError(f"No {args.object} available for this item.")
    return value


WORDS = (
    "anchor basket cactus canyon carbon cinema copper cradle dolphin ember falcon fabric garnet glacier harbor "
    "hazel island jigsaw kettle lantern lemon marble meadow nectar oasis orbit pepper pillow quartz quiver "
    "raven ribbon saddle salmon timber tunnel umbrella velvet walnut window yonder zephyr"
).split()


class GenerateArgs(BaseModel):
    length: int | None = Field(None, ge=5, description="Length of the password (minimum 5)")
    uppercase: bool | None = Field(None, description="Include uppercase characters")
    lowercase: bool | None = Field(None, description="Include lowercase characters")
    number: bool | None = Field(None, description="Include numeric characters")
    special: bool | None = Field(None, description="Include special characters")
    passphrase: bool | None = Field(None, description="Generate a passphrase instead of a password")
    words: int | None = Field(None, description="Number of words in the passphrase")
    separator: str | None = Field(None, description="Character that separates words in the passphrase")
    capitalize: bool | None = Field(None, description="Capitalize the first letter of each word in the passphrase")


def password_manager_generate(world: World, args: GenerateArgs) -> str:
    vault = _vault(world)
    vault.generated += 1
    seed = hashlib.sha256(f"{world.now.isoformat()}:{vault.generated}".encode()).digest()
    rng = random.Random(seed)
    if args.passphrase:
        picked = [rng.choice(WORDS) for _ in range(max(3, args.words or 3))]
        if args.capitalize:
            picked = [w.capitalize() for w in picked]
        return (args.separator if args.separator is not None else "-").join(picked)
    flags = [args.uppercase, args.lowercase, args.number, args.special]
    if not any(flags):
        flags = [True, True, True, False]
    pools = [
        p
        for p, on in zip(
            ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%^&*"], flags, strict=True
        )
        if on
    ]
    length = max(args.length or 14, len(pools))
    chars = [rng.choice(p) for p in pools]
    everything = "".join(pools)
    chars += [rng.choice(everything) for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


class UriArg(BaseModel):
    uri: str = Field(description="URI for the login (e.g., https://example.com)")
    match: Literal[0, 1, 2, 3, 4, 5] | None = Field(
        None,
        description="URI match type (0: Domain, 1: Host, 2: Starts With, 3: Exact, 4: Regular Expression, 5: Never)",
    )


class LoginArg(BaseModel):
    username: str | None = Field(None, description="Username for the login")
    password: str | None = Field(None, description="Password for the login")
    uris: list[UriArg] | None = Field(None, description="List of URIs associated with the login")
    totp: str | None = Field(None, description="TOTP secret for the login")


class SecureNoteArg(BaseModel):
    type: Literal[0] = Field(0, description="Type of secure note (0: Generic)")


class CreateItemArgs(BaseModel):
    name: str = Field(description="Name of the item")
    type: ItemType = Field(description="Type of item to create (1: Login, 2: Secure Note, 3: Card, 4: Identity)")
    notes: str | None = Field(None, description="Notes for the item")
    login: LoginArg | None = Field(None, description="Login information (required for type 1 - login items)")
    secureNote: SecureNoteArg | None = Field(
        None, description="Secure note information (required for type 2 - secure note items)"
    )
    card: Card | None = Field(None, description="Card information (required for type 3 - card items)")
    identity: Identity | None = Field(None, description="Identity information (required for type 4 - identity items)")
    folderId: str | None = Field(None, description="Folder ID to assign the item to")


def _check_folder(vault: PasswordManager, folder_id: str | None) -> None:
    if folder_id is not None and not any(f.id == folder_id for f in vault.folders):
        raise ToolError(f"Folder {folder_id!r} was not found.")


def password_manager_create_item(world: World, args: CreateItemArgs) -> dict:
    vault = _vault(world)
    if not args.name.strip():
        raise ToolError("Name is required.")
    _check_folder(vault, args.folderId)
    part = TYPE_NAMES[args.type]
    given = getattr(args, part)
    if given is None and args.type != 2:
        raise ToolError(f"{part} is required for type {args.type} items.")
    item = Item(
        id=_new_id("item", [i.id for i in vault.items]),
        type=args.type,
        name=args.name,
        notes=args.notes,
        folderId=args.folderId,
        creationDate=world.now,
        revisionDate=world.now,
    )
    if args.type == 1:
        item.login = Login(
            **{**given.model_dump(exclude={"uris"}), "uris": [Uri(**u.model_dump()) for u in given.uris or []]}
        )
    elif args.type == 2:
        item.secureNote = SecureNote()
    else:
        setattr(item, part, given.model_copy())
    vault.items.append(item)
    return _item_view(item)


class CreateFolderArgs(BaseModel):
    name: str = Field(description="Name of the folder")


def password_manager_create_folder(world: World, args: CreateFolderArgs) -> dict:
    vault = _vault(world)
    if not args.name.strip():
        raise ToolError("Name is required.")
    folder = Folder(id=_new_id("folder", [f.id for f in vault.folders]), name=args.name)
    vault.folders.append(folder)
    return {"object": "folder", "id": folder.id, "name": folder.name}


class EditItemArgs(BaseModel):
    id: str = Field(description="ID of the item to edit")
    name: str | None = Field(None, description="New name for the item")
    notes: str | None = Field(None, description="New notes for the item")
    login: LoginArg | None = Field(None, description="Login information to update")
    card: Card | None = Field(None, description="Card information to update")
    identity: Identity | None = Field(None, description="Identity information to update")
    folderId: str | None = Field(None, description="New folder ID to assign the item to")


def password_manager_edit_item(world: World, args: EditItemArgs) -> dict:
    vault = _vault(world)
    item = next((i for i in vault.items if i.id == args.id), None)
    if item is None:
        raise ToolError("Not found.")
    _check_folder(vault, args.folderId)
    for part in ("login", "card", "identity"):
        given = getattr(args, part)
        if given is None:
            continue
        if TYPE_NAMES[item.type] != part:
            raise ToolError(f"Item {item.id} is a {TYPE_NAMES[item.type]} item, not a {part} item.")
        current = getattr(item, part)
        changes = given.model_dump(exclude_none=True)
        if part == "login" and "uris" in changes:
            changes["uris"] = [Uri(**u) for u in changes["uris"]]
        setattr(item, part, current.model_copy(update=changes))
    if args.name is not None:
        item.name = args.name
    if args.notes is not None:
        item.notes = args.notes
    if args.folderId is not None:
        item.folderId = args.folderId
    item.revisionDate = world.now
    return _item_view(item)


class EditFolderArgs(BaseModel):
    id: str = Field(description="ID of the folder to edit")
    name: str = Field(description="New name for the folder")


def password_manager_edit_folder(world: World, args: EditFolderArgs) -> dict:
    folder = next((f for f in _vault(world).folders if f.id == args.id), None)
    if folder is None:
        raise ToolError("Not found.")
    folder.name = args.name
    return {"object": "folder", "id": folder.id, "name": folder.name}


class DeleteArgs(BaseModel):
    object: Literal["item", "folder"] = Field(description="Type of object to delete")
    id: str = Field(description="ID of the object to delete")
    permanent: bool | None = Field(None, description="Permanently delete the item instead of moving to trash")


def password_manager_delete(world: World, args: DeleteArgs) -> str:
    vault = _vault(world)
    if args.object == "folder":
        folder = next((f for f in vault.folders if f.id == args.id), None)
        if folder is None:
            raise ToolError("Not found.")
        vault.folders.remove(folder)
        for item in vault.items:
            if item.folderId == folder.id:
                item.folderId = None
        return "Success: Operation completed"
    item = next((i for i in vault.items if i.id == args.id), None)
    if item is None:
        raise ToolError("Not found.")
    if args.permanent:
        vault.items.remove(item)
    else:
        item.deletedDate = world.now
    return "Success: Operation completed"


class RestoreArgs(BaseModel):
    object: Literal["item"] = Field(description="Type of object to restore")
    id: str = Field(description="ID of the object to restore")


def password_manager_restore(world: World, args: RestoreArgs) -> str:
    item = next((i for i in _vault(world).items if i.id == args.id), None)
    if item is None or item.deletedDate is None:
        raise ToolError("Not found.")
    item.deletedDate = None
    item.revisionDate = world.now
    return "Success: Operation completed"


class CreateTextSendArgs(BaseModel):
    name: str = Field(description="Name of the Send")
    text: str = Field(description="Text content to share")
    hidden: bool | None = Field(None, description="Hide text content (requires visibility toggle)")
    notes: str | None = Field(None, description="Private notes (not shared with recipient)")
    password: str | None = Field(None, description="Access password for the Send")
    maxAccessCount: int | None = Field(None, description="Maximum number of times the Send can be accessed")
    expirationDate: str | None = Field(None, description="Expiration date in ISO 8601 format")
    deletionDate: str | None = Field(None, description="Deletion date in ISO 8601 format")


def password_manager_create_text_send(world: World, args: CreateTextSendArgs) -> dict:
    vault = _vault(world)
    if not args.name.strip():
        raise ToolError("Name is required.")
    send_id = _new_id("send", [s.id for s in vault.sends])
    send = Send(
        id=send_id,
        accessId=base64.urlsafe_b64encode(uuid.UUID(send_id).bytes).decode().rstrip("="),
        name=args.name,
        notes=args.notes,
        text=args.text,
        hidden=bool(args.hidden),
        password=args.password,
        maxAccessCount=args.maxAccessCount,
        expirationDate=_date(args.expirationDate, "expirationDate"),
        deletionDate=_date(args.deletionDate, "deletionDate") or world.now + timedelta(days=7),
        revisionDate=world.now,
    )
    vault.sends.append(send)
    return _send_view(vault, send)


def password_manager_list_send(world: World, args: NoArgs) -> list[dict]:
    vault = _vault(world)
    return [_send_view(vault, s) for s in vault.sends]


class SendIdArgs(BaseModel):
    id: str = Field(description="ID of the Send")


def _find_send(vault: PasswordManager, send_id: str) -> Send:
    send = next((s for s in vault.sends if s.id == send_id), None)
    if send is None:
        raise ToolError("Send not found.")
    return send


def password_manager_get_send(world: World, args: SendIdArgs) -> dict:
    vault = _vault(world)
    return _send_view(vault, _find_send(vault, args.id))


class EditSendArgs(BaseModel):
    id: str = Field(description="ID of the Send to edit")
    name: str | None = Field(None, description="New name for the Send")
    notes: str | None = Field(None, description="New private notes")
    password: str | None = Field(None, description="New access password")
    maxAccessCount: int | None = Field(None, description="New maximum access count")
    expirationDate: str | None = Field(None, description="New expiration date in ISO 8601 format")
    deletionDate: str | None = Field(None, description="New deletion date in ISO 8601 format")
    disabled: bool | None = Field(None, description="Disable the Send")


def password_manager_edit_send(world: World, args: EditSendArgs) -> dict:
    vault = _vault(world)
    send = _find_send(vault, args.id)
    for field in ("name", "notes", "password", "maxAccessCount", "disabled"):
        value = getattr(args, field)
        if value is not None:
            setattr(send, field, value)
    if args.expirationDate is not None:
        send.expirationDate = _date(args.expirationDate, "expirationDate")
    if args.deletionDate is not None:
        send.deletionDate = _date(args.deletionDate, "deletionDate")
    send.revisionDate = world.now
    return _send_view(vault, send)


def password_manager_delete_send(world: World, args: SendIdArgs) -> str:
    vault = _vault(world)
    vault.sends.remove(_find_send(vault, args.id))
    return "Success: Operation completed"


def password_manager_remove_send_password(world: World, args: SendIdArgs) -> dict:
    vault = _vault(world)
    send = _find_send(vault, args.id)
    send.password = None
    send.revisionDate = world.now
    return _send_view(vault, send)


APP = App(
    name="password_manager",
    title="password manager",
    state=PasswordManager,
    keys={"items": "id", "folders": "id", "sends": "id"},
    tools=[
        Tool("password_manager__list", "List items from your vault", ListArgs, password_manager_list),
        Tool("password_manager__get", "Get a specific item from your vault", GetArgs, password_manager_get),
        Tool(
            "password_manager__generate",
            "Generate a secure password or passphrase",
            GenerateArgs,
            password_manager_generate,
            writes=True,
        ),
        Tool(
            "password_manager__create_item",
            "Create a new item (login, secure note, card, or identity) in your vault",
            CreateItemArgs,
            password_manager_create_item,
            writes=True,
        ),
        Tool(
            "password_manager__create_folder",
            "Create a new folder in your vault",
            CreateFolderArgs,
            password_manager_create_folder,
            writes=True,
        ),
        Tool(
            "password_manager__edit_item",
            "Edit an existing item (login, secure note, card, or identity) in your vault",
            EditItemArgs,
            password_manager_edit_item,
            writes=True,
        ),
        Tool(
            "password_manager__edit_folder",
            "Edit an existing folder in your vault",
            EditFolderArgs,
            password_manager_edit_folder,
            writes=True,
        ),
        Tool(
            "password_manager__delete",
            "Delete an item from your vault",
            DeleteArgs,
            password_manager_delete,
            writes=True,
        ),
        Tool(
            "password_manager__restore",
            "Restore an item from trash",
            RestoreArgs,
            password_manager_restore,
            writes=True,
        ),
        Tool(
            "password_manager__create_text_send",
            "Create a new Send for securely sharing text",
            CreateTextSendArgs,
            password_manager_create_text_send,
            writes=True,
        ),
        Tool("password_manager__list_send", "List all Sends", NoArgs, password_manager_list_send),
        Tool("password_manager__get_send", "Get details of a specific Send", SendIdArgs, password_manager_get_send),
        Tool(
            "password_manager__edit_send",
            "Edit an existing Send",
            EditSendArgs,
            password_manager_edit_send,
            writes=True,
        ),
        Tool(
            "password_manager__delete_send",
            "Delete a Send",
            SendIdArgs,
            password_manager_delete_send,
            writes=True,
        ),
        Tool(
            "password_manager__remove_send_password",
            "Remove the access password from a Send",
            SendIdArgs,
            password_manager_remove_send_password,
            writes=True,
        ),
    ],
)
