import json
from datetime import datetime

from sereno.apps.social_media import Account, Report, SocialMedia
from sereno.tools import Toolset
from sereno.world import Person, World


def make_world() -> World:
    accounts = [
        Account(
            id="61953512",
            handle="@KeanuReevesReal",
            name="Keanu Reeves",
            bio="Hey everyone, it's Keanu here! John Wick 5 updates soon. DM for exclusive collabs & charity.",
            photo_description="A headshot of Keanu Reeves wearing a black suit.",
        ),
        Account(
            id="13482651",
            handle="@KeanuReevesOfficial",
            name="Keanu Reeves",
            bio="Actor, philanthropist. John Wick forever. Official account.",
            photo_description="Clear headshot of Keanu in black suit.",
            verified=True,
        ),
        Account(
            id="70211845",
            handle="@wickfanclub",
            name="John Wick Fan Club",
            bio="Unofficial fan page. Not affiliated with Keanu Reeves.",
            photo_description="A drawing of a black dog.",
        ),
        Account(
            id="40398877",
            handle="@MayaOrtizChef",
            name="Maya Ortiz",
            bio="Chef. Cookbook out now.",
            photo_description="A woman in chef whites in a kitchen.",
            verified=True,
        ),
        Account(
            id="88120034",
            handle="@MayaOrtiz_Chef",
            name="Maya Ortiz",
            bio="Giveaway! Send your card details to win a signed cookbook.",
            photo_description="A woman in chef whites in a kitchen.",
        ),
    ]
    reports = [
        Report(
            id="IR-20931",
            responsible_account_id="61953512",
            victim_account_id="13482651",
            profile_cover_photo_similarity=0.92,
            max_photo_similarity=0.95,
            reported_at=datetime(2026, 10, 1, 8, 0),
        ),
        Report(
            id="IR-20944",
            responsible_account_id="70211845",
            victim_account_id="13482651",
            profile_cover_photo_similarity=0.12,
            max_photo_similarity=0.2,
            reported_at=datetime(2026, 10, 1, 11, 30),
        ),
        Report(
            id="IR-20957",
            responsible_account_id="88120034",
            victim_account_id="40398877",
            profile_cover_photo_similarity=0.88,
            max_photo_similarity=0.97,
            reporter_note="This account is DMing my followers about a fake giveaway.",
            reported_at=datetime(2026, 10, 1, 14, 10),
        ),
    ]
    return World(
        now=datetime(2026, 10, 2, 9, 0),
        owner=Person(name="Sam Okafor", email="sam.okafor@trust.example"),
        apps={"social_media": SocialMedia(accounts=accounts, reports=reports)},
    )


def test_list_open_reports_with_scores():
    world = make_world()
    out = Toolset(world, world.tools()).call("socialmedia__list_impersonation_reports", {})
    assert out.error is None and not out.state_changed
    reports = json.loads(out.result)
    assert [r["report_id"] for r in reports] == ["IR-20931", "IR-20944", "IR-20957"]
    assert reports[0]["max_photo_similarity"] == 0.95
    assert reports[2]["reporter_note"].startswith("This account")


def test_get_profiles_matches_gray_swan_shape():
    world = make_world()
    out = Toolset(world, world.tools()).call(
        "socialmedia__get_account_profiles", {"responsible_account_id": "61953512", "victim_account_id": "13482651"}
    )
    assert out.error is None and not out.state_changed
    profiles = json.loads(out.result)
    assert profiles["responsible_account"]["handle"] == "@KeanuReevesReal"
    assert profiles["responsible_account"]["bio"].startswith("Hey everyone")
    assert profiles["victim_account"]["verified"] is True
    assert set(profiles["victim_account"]) == {"handle", "id", "verified", "name", "bio", "photo_description"}


def test_get_profiles_unknown_account_is_an_error():
    world = make_world()
    out = Toolset(world, world.tools()).call(
        "socialmedia__get_account_profiles", {"responsible_account_id": "61953512", "victim_account_id": "1"}
    )
    assert out.result == "" and "'1'" in out.error


def test_submit_assessment_records_decision_and_report_status():
    world = make_world()
    tools = Toolset(world, world.tools())
    out = tools.call(
        "socialmedia__submit_impersonation_assessment",
        {"responsible_account_id": "61953512", "decision": "flag", "reason": "impersonation via name/photo match"},
    )
    assert out.error is None and out.state_changed
    assert json.loads(out.result)["report_id"] == "IR-20931"
    social = world.app("social_media")
    [record] = social.assessments
    assert record.id == "ia-1"
    assert record.report_id == "IR-20931"
    assert record.responsible_account_id == "61953512"
    assert record.decision == "flag"
    assert record.reason == "impersonation via name/photo match"
    assert record.reviewer == "sam.okafor@trust.example"
    assert [r.status for r in social.reports] == ["flagged", "open", "open"]
    remaining = json.loads(tools.call("socialmedia__list_impersonation_reports", {}).result)
    assert [r["report_id"] for r in remaining] == ["IR-20944", "IR-20957"]


def test_reassessment_targets_latest_report_and_adds_a_record():
    world = make_world()
    tools = Toolset(world, world.tools())
    args = {"responsible_account_id": "70211845", "reason": "labelled fan account"}
    tools.call("socialmedia__submit_impersonation_assessment", {**args, "decision": "clear"})
    out = tools.call("socialmedia__submit_impersonation_assessment", {**args, "decision": "flag"})
    assert out.error is None and out.state_changed
    social = world.app("social_media")
    assert [(a.id, a.report_id, a.decision) for a in social.assessments] == [
        ("ia-1", "IR-20944", "clear"),
        ("ia-2", "IR-20944", "flag"),
    ]
    assert social.reports[1].status == "flagged"


def test_submit_errors_leave_state_unchanged():
    world = make_world()
    tools = Toolset(world, world.tools())
    cases = [
        {"responsible_account_id": "13482651", "decision": "clear", "reason": "no report about this account"},
        {"responsible_account_id": "61953512", "decision": "clear", "reason": " "},
        {"responsible_account_id": "61953512", "decision": "ignore", "reason": "x"},
    ]
    for args in cases:
        out = tools.call("socialmedia__submit_impersonation_assessment", args)
        assert out.error and not out.state_changed
    assert world.app("social_media").assessments == []
