"""Chat: Slack-like team chat across the workspaces the person belongs to.

Serves the Gray Swan tool scenarios slack-phishing, ignore-email and
share-document, which share one Slack tool schema: listSlackChannels,
listSlackMessages, sendSlackMessage, searchSlackMessages, listSlackUsers.
Names, parameters and output shapes follow those files; channels carry the
`workspace` field seen in slack-phishing, so one person can be in a work and a
community workspace at once.

Direct messages are channels with `is_dm` set and `dm_user` naming the other
person. sendSlackMessage accepts a user id as well as a channel or DM id, as
its Gray Swan description says ("to a Slack channel or user"): the message goes
to the DM with that user, which is created on first use with the id "D" plus
the user id without its leading "U". Every sent message is a new item in
`messages`, by the person, in the channel or DM it went to.

Message ids are Slack timestamps ("1710420832.094729"): the world clock in
epoch seconds (the naive clock read as UTC) and a counter. Added (not in Gray
Swan): `is_dm` in listSlackChannels, `workspace` in listSlackUsers,
`reply_count` for thread parents in listSlackMessages, and the Slack search
modifiers `from:` and `in:` in searchSlackMessages. No tools are added.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World

_EPOCH = datetime(1970, 1, 1)


class Channel(BaseModel):
    id: str
    name: str
    workspace: str = ""
    topic: str = ""
    is_private: bool = False
    is_archived: bool = False
    member_count: int = 0
    is_dm: bool = False
    dm_user: str = ""


class User(BaseModel):
    id: str
    name: str
    real_name: str = ""
    email: str = ""
    title: str = ""
    workspace: str = ""
    is_active: bool = True


class Reaction(BaseModel):
    name: str
    count: int = 1


class Message(BaseModel):
    id: str
    channel_id: str
    user: str
    text: str
    sent_at: datetime
    thread_ts: str | None = None
    reactions: list[Reaction] = []


class Chat(BaseModel):
    channels: list[Channel] = []
    users: list[User] = []
    messages: list[Message] = []

    def channel(self, channel_id: str) -> Channel | None:
        return next((c for c in self.channels if c.id == channel_id), None)


def _chat(world: World) -> Chat:
    return world.app("chat")


def _channel_or_error(chat: Chat, channel_id: str) -> Channel:
    channel = chat.channel(channel_id)
    if channel is None:
        raise ToolError(f"channel_not_found: no channel or DM with id {channel_id!r}. Use listSlackChannels.")
    return channel


def _stamp(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _is_reply(m: Message) -> bool:
    return m.thread_ts is not None and m.thread_ts != m.id


def _channel_view(c: Channel) -> dict:
    return {
        "channel_id": c.id,
        "name": c.name,
        "is_private": c.is_private,
        "member_count": c.member_count,
        "topic": c.topic,
        "workspace": c.workspace,
        "is_dm": c.is_dm,
    }


def _message_view(m: Message, reply_count: int | None = None) -> dict:
    view = {
        "message_id": m.id,
        "user": m.user,
        "text": m.text,
        "timestamp": _stamp(m.sent_at),
        "thread_ts": m.thread_ts,
        "reactions": [r.model_dump() for r in m.reactions],
    }
    if reply_count:
        view["reply_count"] = reply_count
    return view


class ListChannelsArgs(BaseModel):
    include_archived: bool = Field(False, description="Also list archived channels.")


def list_slack_channels(world: World, args: ListChannelsArgs) -> dict:
    channels = [c for c in _chat(world).channels if args.include_archived or not c.is_archived]
    return {"channels": [_channel_view(c) for c in channels]}


class ListMessagesArgs(BaseModel):
    channel_id: str = Field(description="Id of the channel or DM to read.")
    limit: int = Field(50, ge=1, description="Most messages to return.")
    thread_ts: str | None = Field(None, description="Timestamp of a thread's first message, to read that thread.")


def list_slack_messages(world: World, args: ListMessagesArgs) -> dict:
    chat = _chat(world)
    channel = _channel_or_error(chat, args.channel_id)
    in_channel = [m for m in chat.messages if m.channel_id == channel.id]
    if args.thread_ts is not None:
        thread = [m for m in in_channel if m.id == args.thread_ts or m.thread_ts == args.thread_ts]
        if not any(m.id == args.thread_ts for m in thread):
            raise ToolError(f"thread_not_found: no message {args.thread_ts!r} in this channel.")
        thread.sort(key=lambda m: (m.sent_at, m.id))
        return {"messages": [_message_view(m) for m in thread[: args.limit]]}
    replies: dict[str, int] = {}
    for m in in_channel:
        if _is_reply(m):
            replies[m.thread_ts] = replies.get(m.thread_ts, 0) + 1
    top = sorted((m for m in in_channel if not _is_reply(m)), key=lambda m: (m.sent_at, m.id), reverse=True)
    return {"messages": [_message_view(m, replies.get(m.id)) for m in top[: args.limit]]}


def _me(world: World, chat: Chat, workspace: str) -> str:
    email = world.owner.email.lower()
    mine = [u for u in chat.users if u.email.lower() == email]
    same = [u for u in mine if u.workspace == workspace]
    if same or mine:
        return (same or mine)[0].name
    return world.owner.email.split("@")[0]


def _dm_with(chat: Chat, user: User) -> Channel:
    channel = next((c for c in chat.channels if c.is_dm and c.dm_user == user.id), None)
    if channel is None:
        channel = Channel(
            id="D" + user.id.removeprefix("U"),
            name=user.name,
            workspace=user.workspace,
            is_private=True,
            member_count=2,
            is_dm=True,
            dm_user=user.id,
        )
        chat.channels.append(channel)
    return channel


def _new_id(world: World, chat: Chat) -> str:
    seconds = int((world.now - _EPOCH).total_seconds())
    taken = {m.id for m in chat.messages}
    n = len(chat.messages) + 1
    while f"{seconds}.{n:06d}" in taken:
        n += 1
    return f"{seconds}.{n:06d}"


class SendMessageArgs(BaseModel):
    channel_id: str = Field(description="Id of the channel or DM, or a user id to message that person directly.")
    text: str = Field(description="The message text.")
    thread_ts: str | None = Field(None, description="Timestamp of a message to reply to in its thread.")


def send_slack_message(world: World, args: SendMessageArgs) -> dict:
    if not args.text.strip():
        raise ToolError("no_text: the message is empty.")
    chat = _chat(world)
    user = next((u for u in chat.users if u.id == args.channel_id), None)
    if user is not None:
        if not user.is_active:
            raise ToolError(f"user_not_found: {user.name} is deactivated.")
        channel = _dm_with(chat, user)
    else:
        channel = _channel_or_error(chat, args.channel_id)
    if channel.is_archived:
        raise ToolError(f"is_archived: #{channel.name} is archived.")
    thread_ts = None
    if args.thread_ts is not None:
        parent = next((m for m in chat.messages if m.id == args.thread_ts and m.channel_id == channel.id), None)
        if parent is None:
            raise ToolError(f"thread_not_found: no message {args.thread_ts!r} in this channel.")
        thread_ts = parent.thread_ts or parent.id
    message = Message(
        id=_new_id(world, chat),
        channel_id=channel.id,
        user=_me(world, chat, channel.workspace),
        text=args.text,
        sent_at=world.now,
        thread_ts=thread_ts,
    )
    chat.messages.append(message)
    return {
        "message_id": message.id,
        "timestamp": _stamp(message.sent_at),
        "channel_id": channel.id,
        "status": "sent",
    }


class SearchMessagesArgs(BaseModel):
    query: str = Field(description="Words to find in messages. Supports 'from:<user>' and 'in:<channel>'.")
    max_results: int = Field(20, ge=1, description="Most results to return.")


def search_slack_messages(world: World, args: SearchMessagesArgs) -> dict:
    chat = _chat(world)
    words, sender, place = [], None, None
    for token in args.query.lower().split():
        if token.startswith("from:"):
            sender = token[5:].lstrip("@")
        elif token.startswith("in:"):
            place = token[3:].lstrip("#")
        else:
            words.append(token)
    if not (words or sender or place):
        raise ToolError("no_query: give at least one word to search for.")
    channels = {c.id: c for c in chat.channels}
    found = [
        m
        for m in chat.messages
        if (m.channel_id in channels)
        and (sender is None or m.user.lower() == sender)
        and (place is None or place in (m.channel_id.lower(), channels[m.channel_id].name.lower()))
        and all(w in m.text.lower() for w in words)
    ]
    found.sort(key=lambda m: (m.sent_at, m.id), reverse=True)
    return {
        "messages": [
            {
                "message_id": m.id,
                "channel_id": m.channel_id,
                "channel_name": channels[m.channel_id].name,
                "user": m.user,
                "text": m.text,
                "timestamp": _stamp(m.sent_at),
            }
            for m in found[: args.max_results]
        ]
    }


class ListUsersArgs(BaseModel):
    include_inactive: bool = Field(False, description="Also list deactivated users.")


def list_slack_users(world: World, args: ListUsersArgs) -> dict:
    users = [u for u in _chat(world).users if args.include_inactive or u.is_active]
    return {
        "users": [
            {
                "user_id": u.id,
                "name": u.name,
                "real_name": u.real_name,
                "email": u.email,
                "title": u.title,
                "is_active": u.is_active,
                "workspace": u.workspace,
            }
            for u in users
        ]
    }


APP = App(
    name="chat",
    title="chat",
    state=Chat,
    keys={"channels": "id", "users": "id", "messages": "id"},
    tools=[
        Tool(
            "listSlackChannels",
            "chat",
            "List the Slack channels and direct messages the user can see, in every workspace they belong to.",
            ListChannelsArgs,
            list_slack_channels,
        ),
        Tool(
            "listSlackMessages",
            "chat",
            "List messages in a Slack channel or DM, newest first, or the messages of one thread.",
            ListMessagesArgs,
            list_slack_messages,
        ),
        Tool(
            "sendSlackMessage",
            "chat",
            "Post a message as the user to a Slack channel, a DM, or directly to a user.",
            SendMessageArgs,
            send_slack_message,
            writes=True,
        ),
        Tool(
            "searchSlackMessages",
            "chat",
            "Search messages across all Slack channels and DMs the user can see, newest first.",
            SearchMessagesArgs,
            search_slack_messages,
        ),
        Tool(
            "listSlackUsers",
            "chat",
            "List people in the user's Slack workspaces.",
            ListUsersArgs,
            list_slack_users,
        ),
    ],
)
