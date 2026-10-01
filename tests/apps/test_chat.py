import json
from datetime import datetime

import pytest

from sereno.apps.chat import Channel, Chat, Message, Reaction, User
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
        ],
        users=[
            User(id="U01ME", name="jamie.lee", real_name="Jamie Lee", email="jamie@vertex.ai", workspace=WORK),
            User(id="U02SARAH", name="sarah.chen", real_name="Sarah Chen", email="sarah@vertex.ai", workspace=WORK),
            User(id="U03GONE", name="old.timer", workspace=WORK, is_active=False),
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


def test_list_channels_hides_archived_unless_asked(world):
    _, result = call(world, "listSlackChannels")
    ids = [c["channel_id"] for c in result["channels"]]
    assert "C03OLD" not in ids and "C05K3M8BNQR" in ids
    assert result["channels"][0]["workspace"] == WORK
    _, result = call(world, "listSlackChannels", include_archived=True)
    assert "C03OLD" in [c["channel_id"] for c in result["channels"]]


def test_list_messages_newest_first_with_threads_folded(world):
    _, result = call(world, "listSlackMessages", channel_id="C02A7NM4KP9", limit=5)
    ids = [m["message_id"] for m in result["messages"]]
    assert ids == ["1710420832.094729", "1710418976.092113"]
    assert result["messages"][0]["reply_count"] == 1
    assert result["messages"][1]["reactions"] == [{"name": "fire", "count": 7}]


def test_list_messages_of_thread(world):
    _, result = call(world, "listSlackMessages", channel_id="C02A7NM4KP9", thread_ts="1710420832.094729")
    assert [m["user"] for m in result["messages"]] == ["sarah.chen", "alex.kumar"]


def test_list_messages_unknown_channel(world):
    outcome, _ = call(world, "listSlackMessages", channel_id="C999")
    assert "channel_not_found" in outcome.error


def test_send_message_to_channel_leaves_record(world):
    outcome, result = call(world, "sendSlackMessage", channel_id="C02A7NM4KP9", text="See the new paper")
    assert outcome.error is None and outcome.state_changed
    assert result["status"] == "sent" and result["channel_id"] == "C02A7NM4KP9"
    sent = world.app("chat").messages[-1]
    assert sent.id == result["message_id"] == "1741968300.000005"
    assert sent.user == "jamie.lee" and sent.text == "See the new paper"
    assert sent.sent_at == world.now and sent.thread_ts is None


def test_send_reply_in_thread(world):
    _, result = call(world, "sendSlackMessage", channel_id="C02A7NM4KP9", text="Agreed", thread_ts="1710420900.000001")
    assert world.app("chat").messages[-1].thread_ts == "1710420832.094729"
    _, thread = call(world, "listSlackMessages", channel_id="C02A7NM4KP9", thread_ts="1710420832.094729")
    assert thread["messages"][-1]["message_id"] == result["message_id"]


def test_send_to_user_opens_dm(world):
    outcome, result = call(world, "sendSlackMessage", channel_id="U02SARAH", text="Got a minute?")
    assert outcome.state_changed and result["channel_id"] == "D02SARAH"
    dm = world.app("chat").channel("D02SARAH")
    assert dm.is_dm and dm.dm_user == "U02SARAH" and dm.name == "sarah.chen"
    call(world, "sendSlackMessage", channel_id="D02SARAH", text="Never mind")
    assert sum(c.is_dm for c in world.app("chat").channels) == 1
    _, msgs = call(world, "listSlackMessages", channel_id="D02SARAH")
    assert [m["text"] for m in msgs["messages"]] == ["Never mind", "Got a minute?"]


def test_send_errors_leave_no_record(world):
    for args, error in [
        ({"channel_id": "C03OLD", "text": "hi"}, "is_archived"),
        ({"channel_id": "C999", "text": "hi"}, "channel_not_found"),
        ({"channel_id": "U03GONE", "text": "hi"}, "deactivated"),
        ({"channel_id": "C02B8PN5LQ2", "text": "  "}, "no_text"),
        ({"channel_id": "C02B8PN5LQ2", "text": "hi", "thread_ts": "1710420832.094729"}, "thread_not_found"),
    ]:
        outcome, _ = call(world, "sendSlackMessage", **args)
        assert error in outcome.error and not outcome.state_changed


def test_search_messages(world):
    _, result = call(world, "searchSlackMessages", query="transformer")
    assert [m["user"] for m in result["messages"]] == ["alex.kumar", "sarah.chen"]
    assert result["messages"][0]["channel_name"] == "machine-learning"
    _, result = call(world, "searchSlackMessages", query="transformer from:@sarah.chen")
    assert [m["message_id"] for m in result["messages"]] == ["1710420832.094729"]
    _, result = call(world, "searchSlackMessages", query="in:#ai-alignment", max_results=5)
    assert [m["channel_id"] for m in result["messages"]] == ["C05K3M8BNQR"]


def test_search_empty_query(world):
    outcome, _ = call(world, "searchSlackMessages", query=" ")
    assert "no_query" in outcome.error


def test_list_users(world):
    _, result = call(world, "listSlackUsers")
    assert [u["user_id"] for u in result["users"]] == ["U01ME", "U02SARAH"]
    _, result = call(world, "listSlackUsers", include_inactive=True)
    assert len(result["users"]) == 3
