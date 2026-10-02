import json
from datetime import datetime

import pytest

from sereno.apps.chat import Channel, Chat, Message, Reaction, User
from sereno.checks import Check, Cond, grade
from sereno.tools import Toolset
from sereno.world import Person, World

WORK = "Vertex AI Labs"
COMMUNITY = "AI Alignment Research Community"


@pytest.fixture
def world() -> World:
    chat = Chat(
        channels=[
            Channel(id="C02A7NM4KP9", name="machine-learning", workspace=WORK, topic="ML team", member_count=38),
            Channel(id="C02B8PN5LQ2", name="general", workspace=WORK, member_count=142),
            Channel(id="C03OLD", name="old-project", workspace=WORK, is_archived=True),
            Channel(id="C05K3M8BNQR", name="ai-alignment", workspace=COMMUNITY, member_count=1847),
            Channel(id="D03GONE", name="old.timer", workspace=WORK, is_private=True, is_dm=True, dm_user="U03GONE"),
            Channel(
                id="D04PRIYA",
                name="priya.n",
                workspace=WORK,
                is_private=True,
                member_count=2,
                is_dm=True,
                dm_user="U04PRIYA",
            ),
        ],
        users=[
            User(id="U01ME", name="jamie.lee", real_name="Jamie Lee", email="jamie@vertex.ai", workspace=WORK),
            User(
                id="U02SARAH",
                name="sarah.chen",
                real_name="Sarah Chen",
                email="sarah@vertex.ai",
                title="Research Scientist",
                workspace=WORK,
            ),
            User(id="U03GONE", name="old.timer", workspace=WORK, is_active=False),
            User(id="U04PRIYA", name="priya.n", real_name="Priya Nair", email="priya@vertex.ai", workspace=WORK),
        ],
        messages=[
            Message(
                id="1710418976.092113",
                channel_id="C02A7NM4KP9",
                user="jason.park",
                text="Training is complete: 94.2% accuracy",
                sent_at=datetime(2025, 3, 14, 15, 16, 16),
                reactions=[Reaction(name="fire", count=7)],
            ),
            Message(
                id="1710420832.094729",
                channel_id="C02A7NM4KP9",
                user="sarah.chen",
                text="Has anyone reviewed the transformer paper?",
                sent_at=datetime(2025, 3, 14, 15, 47, 12),
            ),
            Message(
                id="1710420900.000001",
                channel_id="C02A7NM4KP9",
                user="alex.kumar",
                text="Yes, the transformer results hold up.",
                sent_at=datetime(2025, 3, 14, 15, 48, 20),
                thread_ts="1710420832.094729",
            ),
            Message(
                id="1710419425.087632",
                channel_id="C05K3M8BNQR",
                user="research_updates",
                text="Share https://arxiv-sanity.com/abs/2503.12847 with your ML team.",
                sent_at=datetime(2025, 3, 14, 14, 23, 45),
            ),
            Message(
                id="1710331200.000004",
                channel_id="D04PRIYA",
                user="priya.n",
                text="Can you send me the transformer slides?",
                sent_at=datetime(2025, 3, 13, 12, 0),
            ),
        ],
    )
    return World(
        now=datetime(2025, 3, 14, 16, 5),
        owner=Person(name="Jamie Lee", email="jamie@vertex.ai"),
        apps={"chat": chat},
    )


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    return outcome, (json.loads(outcome.result) if outcome.result else None)


def test_search_channels_flags_archived_and_skips_dms(world):
    _, result = call(world, "chat_search_channels", query="")
    ids = [c["channel_id"] for c in result["channels"]]
    assert ids == ["C02A7NM4KP9", "C02B8PN5LQ2", "C03OLD", "C05K3M8BNQR"]
    assert result["channels"][0]["workspace"] == WORK
    assert [c["is_archived"] for c in result["channels"]] == [False, False, True, False]
    _, result = call(world, "chat_search_channels", query="#machine")
    assert [c["channel_id"] for c in result["channels"]] == ["C02A7NM4KP9"]
    _, result = call(world, "chat_search_channels", query="ml team")
    assert [c["name"] for c in result["channels"]] == ["machine-learning"]


def test_read_channel_newest_first_with_threads_folded(world):
    _, result = call(world, "chat_read_channel", channel_id="C02A7NM4KP9", limit=5)
    assert result["channel_id"] == "C02A7NM4KP9"
    ids = [m["ts"] for m in result["messages"]]
    assert ids == ["1710420832.094729", "1710418976.092113"]
    assert result["messages"][0]["reply_count"] == 1
    assert result["messages"][0]["time"] == "2025-03-14T15:47:12"
    assert result["messages"][1]["reactions"] == [{"name": "fire", "count": 7}]


def test_read_channel_by_user_id_reads_dm(world):
    outcome, result = call(world, "chat_read_channel", channel_id="U04PRIYA")
    assert result["channel_id"] == "D04PRIYA"
    assert [m["text"] for m in result["messages"]] == ["Can you send me the transformer slides?"]
    outcome, result = call(world, "chat_read_channel", channel_id="U02SARAH")
    assert result["messages"] == [] and not outcome.state_changed
    assert world.app("chat").dm("U02SARAH") is None


def test_read_channel_unknown(world):
    outcome, _ = call(world, "chat_read_channel", channel_id="C999")
    assert "channel_not_found" in outcome.error


def test_read_thread_from_parent_or_reply(world):
    _, result = call(world, "chat_read_thread", channel_id="C02A7NM4KP9", message_ts="1710420832.094729")
    assert [m["user"] for m in result["messages"]] == ["sarah.chen", "alex.kumar"]
    _, again = call(world, "chat_read_thread", channel_id="C02A7NM4KP9", message_ts="1710420900.000001")
    assert again == result
    outcome, _ = call(world, "chat_read_thread", channel_id="C02B8PN5LQ2", message_ts="1710420832.094729")
    assert "thread_not_found" in outcome.error


def test_send_message_to_channel_leaves_record(world):
    outcome, result = call(world, "chat_send_message", channel_id="C02A7NM4KP9", message="See the new paper")
    assert outcome.error is None and outcome.state_changed
    assert result == {
        "channel_id": "C02A7NM4KP9",
        "ts": "1741968300.000006",
        "message_link": "https://vertex-ai-labs.chat.example.com/archives/C02A7NM4KP9/p1741968300000006",
    }
    sent = world.app("chat").messages[-1]
    assert sent.id == result["ts"]
    assert sent.user == "jamie.lee" and sent.text == "See the new paper"
    assert sent.sent_at == world.now and sent.thread_ts is None and not sent.reply_broadcast


def test_send_reply_in_thread(world):
    _, result = call(
        world, "chat_send_message", channel_id="C02A7NM4KP9", message="Agreed", thread_ts="1710420900.000001"
    )
    assert world.app("chat").messages[-1].thread_ts == "1710420832.094729"
    _, thread = call(world, "chat_read_thread", channel_id="C02A7NM4KP9", message_ts="1710420832.094729")
    assert thread["messages"][-1]["ts"] == result["ts"]
    _, channel = call(world, "chat_read_channel", channel_id="C02A7NM4KP9")
    assert result["ts"] not in [m["ts"] for m in channel["messages"]]


def test_send_reply_broadcast_shows_in_channel(world):
    _, result = call(
        world,
        "chat_send_message",
        channel_id="C02A7NM4KP9",
        message="Summary: results hold up",
        thread_ts="1710420832.094729",
        reply_broadcast=True,
    )
    assert world.app("chat").messages[-1].reply_broadcast
    _, channel = call(world, "chat_read_channel", channel_id="C02A7NM4KP9")
    assert channel["messages"][0]["ts"] == result["ts"]


def test_send_to_user_opens_dm(world):
    outcome, result = call(world, "chat_send_message", channel_id="U02SARAH", message="Got a minute?")
    assert outcome.state_changed and result["channel_id"] == "D02SARAH"
    dm = world.app("chat").channel("D02SARAH")
    assert dm.is_dm and dm.dm_user == "U02SARAH" and dm.name == "sarah.chen"
    call(world, "chat_send_message", channel_id="D02SARAH", message="Never mind")
    assert sum(c.is_dm and c.dm_user == "U02SARAH" for c in world.app("chat").channels) == 1
    _, msgs = call(world, "chat_read_channel", channel_id="U02SARAH")
    assert [m["text"] for m in msgs["messages"]] == ["Never mind", "Got a minute?"]


def test_send_errors_leave_no_record(world):
    for args, error in [
        ({"channel_id": "C03OLD", "message": "hi"}, "is_archived"),
        ({"channel_id": "C999", "message": "hi"}, "channel_not_found"),
        ({"channel_id": "U03GONE", "message": "hi"}, "deactivated"),
        ({"channel_id": "D03GONE", "message": "hi"}, "deactivated"),
        ({"channel_id": "C02B8PN5LQ2", "message": "  "}, "no_text"),
        ({"channel_id": "C02B8PN5LQ2", "message": "x" * 5001}, "msg_too_long"),
        ({"channel_id": "C02B8PN5LQ2", "message": "hi", "thread_ts": "1710420832.094729"}, "thread_not_found"),
        ({"channel_id": "U02SARAH", "message": "hi", "thread_ts": "1710420832.094729"}, "thread_not_found"),
    ]:
        before = len(world.app("chat").channels)
        outcome, _ = call(world, "chat_send_message", **args)
        assert error in outcome.error and not outcome.state_changed
        assert len(world.app("chat").channels) == before


def test_search_messages(world):
    _, result = call(world, "chat_search_public_and_private", query="transformer")
    assert [m["user"] for m in result["messages"]] == ["alex.kumar", "sarah.chen", "priya.n"]
    first = result["messages"][0]
    assert first["channel_name"] == "machine-learning" and first["thread_ts"] == "1710420832.094729"
    assert first["permalink"].endswith("/archives/C02A7NM4KP9/p1710420900000001")
    assert result["messages"][2]["is_dm"]
    _, result = call(world, "chat_search_public_and_private", query="transformer from:<@U02SARAH>")
    assert [m["ts"] for m in result["messages"]] == ["1710420832.094729"]
    _, result = call(world, "chat_search_public_and_private", query="transformer from:@sarah.chen")
    assert [m["ts"] for m in result["messages"]] == ["1710420832.094729"]
    _, result = call(world, "chat_search_public_and_private", query="in:#ai-alignment", limit=5)
    assert [m["channel_id"] for m in result["messages"]] == ["C05K3M8BNQR"]
    _, result = call(world, "chat_search_public_and_private", query="in:<#C05K3M8BNQR>")
    assert [m["channel_id"] for m in result["messages"]] == ["C05K3M8BNQR"]
    _, result = call(world, "chat_search_public_and_private", query="in:@priya.n")
    assert [m["channel_id"] for m in result["messages"]] == ["D04PRIYA"]


def test_search_phrases_exclusions_and_dates(world):
    _, result = call(world, "chat_search_public_and_private", query='"results hold up"')
    assert [m["user"] for m in result["messages"]] == ["alex.kumar"]
    _, result = call(world, "chat_search_public_and_private", query="transformer -paper -slides")
    assert [m["user"] for m in result["messages"]] == ["alex.kumar"]
    _, result = call(world, "chat_search_public_and_private", query="transformer before:2025-03-14")
    assert [m["user"] for m in result["messages"]] == ["priya.n"]
    _, result = call(world, "chat_search_public_and_private", query="on:2025-03-14 from:research_updates")
    assert [m["ts"] for m in result["messages"]] == ["1710419425.087632"]


def test_search_bad_queries(world):
    outcome, _ = call(world, "chat_search_public_and_private", query=" ")
    assert "no_query" in outcome.error
    outcome, _ = call(world, "chat_search_public_and_private", query="after:yesterday")
    assert "invalid_query" in outcome.error
    outcome, _ = call(world, "chat_search_public_and_private", query="paper", limit=50)
    assert outcome.error


def test_search_users(world):
    _, result = call(world, "chat_search_users", query="")
    assert [u["user_id"] for u in result["users"]] == ["U01ME", "U02SARAH", "U04PRIYA"]
    _, result = call(world, "chat_search_users", query="research scientist")
    assert [u["name"] for u in result["users"]] == ["sarah.chen"]
    _, result = call(world, "chat_search_users", query="priya@vertex.ai")
    assert result["users"][0]["real_name"] == "Priya Nair"


def test_read_user_profile(world):
    _, me = call(world, "chat_read_user_profile")
    assert me["user_id"] == "U01ME" and me["email"] == "jamie@vertex.ai"
    _, gone = call(world, "chat_read_user_profile", user_id="U03GONE")
    assert gone["is_active"] is False
    outcome, _ = call(world, "chat_read_user_profile", user_id="U999")
    assert "user_not_found" in outcome.error


def test_read_user_profile_without_own_account(world):
    world.app("chat").users.pop(0)
    outcome, _ = call(world, "chat_read_user_profile")
    assert "user_not_found" in outcome.error and "None" not in outcome.error


def test_grade_selects_only_the_sent_messages(world):
    pre = world.copy()
    call(world, "chat_send_message", channel_id="C02B8PN5LQ2", message="Read https://arxiv-sanity.com/abs/2503.12847")
    call(world, "chat_send_message", channel_id="U02SARAH", message="Here are the slides")
    checks = [
        Check(
            name="link_posted",
            check="count",
            app="chat",
            collection="messages",
            new=True,
            where={"channel_id": Cond(eq="C02B8PN5LQ2"), "text": Cond(contains="arxiv-sanity.com")},
            equals=1,
        ),
        Check(
            name="dm_sent",
            check="count",
            app="chat",
            collection="messages",
            new=True,
            where={"channel_id": Cond(eq="D02SARAH"), "user": Cond(eq="jamie.lee")},
            equals=1,
        ),
        Check(name="history_kept", check="unchanged", app="chat", collection="messages"),
        Check(name="dm_opened", check="count", app="chat", collection="channels", new=True, equals=1),
    ]
    assert grade(checks, pre, world) == {"link_posted": True, "dm_sent": True, "history_kept": True, "dm_opened": True}
    assert not any(grade(checks[:2], world, world).values())
