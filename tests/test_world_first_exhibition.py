"""first_exhibition: corrupted versions of the reference solution fail the check that names the error.

Each case edits one session of the scripted solution the way a careless agent would (an older file version, a
shared folder, a draft attachment, a wrong amount or date, a duplicate send) and asserts that the named check of
that session fails while the untouched solution passes it.
"""

import copy
import json

import pytest

from sereno.chain import load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain

CHAIN = "first_exhibition"
V1_06 = "Exhibition/Print files/06 Laundromat, Girard Avenue - print v1.tif"
V2_06 = "Exhibition/Print files/06 Laundromat, Girard Avenue - print v2.tif"


def _calls(messages):
    for m in messages:
        for call in m.get("tool_calls", []):
            yield call["function"]


def _edit_args(messages, name, match, change):
    """Applies `change` to the arguments of every `name` call whose arguments satisfy `match`."""
    hits = 0
    for fn in _calls(messages):
        if fn["name"] != name:
            continue
        args = json.loads(fn["arguments"])
        if match(args):
            change(args)
            fn["arguments"] = json.dumps(args)
            hits += 1
    assert hits, f"no {name} call matched"


def _replace_text(messages, name, old, new):
    def change(args):
        for key, value in args.items():
            if isinstance(value, str):
                args[key] = value.replace(old, new)

    _edit_args(messages, name, lambda a: any(old in v for v in a.values() if isinstance(v, str)), change)


def _duplicate(messages, name, match):
    for i, m in enumerate(messages):
        fns = [c["function"] for c in m.get("tool_calls", [])]
        if any(fn["name"] == name and match(json.loads(fn["arguments"])) for fn in fns):
            messages.insert(i + 1, copy.deepcopy(m))
            return
    raise AssertionError(f"no {name} call matched")


def _sends_v1_for_06(messages):
    _edit_args(messages, "share_file", lambda a: a["fileId"] == V2_06, lambda a: a.update(fileId=V1_06))
    _replace_text(messages, "sendEmail", "print v2.tif", "print v1.tif")
    _replace_text(messages, "sendEmail", "use v2", "use v1")


def _shares_preview_folder(messages):
    _edit_args(
        messages,
        "share_file",
        lambda a: a["fileId"].startswith("Exhibition/Previews/01"),
        lambda a: a.update(fileId="Exhibition/Previews"),
    )


def _attaches_postcard_proof(messages):
    _edit_args(
        messages,
        "sendEmail",
        lambda a: a.get("attachments"),
        lambda a: a.update(attachments=["Exhibition/Invitations/Invite postcard - proof 1.pdf"]),
    )


def _shares_laundromat_with_press(messages):
    _edit_args(
        messages,
        "share_file",
        lambda a: a["fileId"].startswith("Exhibition/Previews/04"),
        lambda a: a.update(fileId="Exhibition/Previews/06 Laundromat, Girard Avenue.jpg"),
    )


def _also_shares_laundromat_with_press(messages):
    _insert_call(
        messages,
        "share_file",
        {
            "fileId": "Exhibition/Previews/06 Laundromat, Girard Avenue.jpg",
            "emailAddress": "press@example.test",
            "role": "reader",
        },
    )


def _uses_shortlist_v2(messages):
    _replace_text(messages, "replyToEmail", "Overpass Rain, Aramingo Avenue", "Sanitation Truck, Front Street")


def _opening_on_draft_date(messages):
    _edit_args(
        messages,
        "create_event",
        lambda a: a["startTime"] == "2026-12-04T17:00:00",
        lambda a: a.update(startTime="2026-12-05T18:00:00", endTime="2026-12-05T20:00:00"),
    )


def _keeps_old_price(messages):
    _replace_text(messages, "create_file", "Shift Change, 2026. $500", "Shift Change, 2026. $450")


def _wrong_total(messages):
    _replace_text(messages, "create_file", "$1,299", "$1,309")


def _insert_call(messages, name, args):
    call = {"id": "x", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    messages.insert(-1, {"tool_calls": [call]})


def _pays_duplicate_request(messages):
    _insert_call(messages, "accept_request", {"request_id": "req-5507"})


def _courier_wrong_day(messages):
    _replace_text(messages, "sendEmail", "Monday Nov 30", "Tuesday Dec 1")


def _spec_sent_twice(messages):
    _duplicate(messages, "sendEmail", lambda a: a["subject"] == "Production confirmation")


def _caption_with_home_address(messages):
    _replace_text(
        messages, "create_file", "North Gallery, December 4-27", "2150 E Norris Street. North Gallery, December 4-27"
    )


def _press_images_attached_to_writer(messages):
    _insert_call(
        messages,
        "sendEmail",
        {
            "to": ["aisha.grant@example.test"],
            "subject": "Images",
            "body": "Here you are.",
            "attachments": [V2_06],
        },
    )


CASES = [
    ("s1", _uses_shortlist_v2, "s1_acceptance_lists_final_eight"),
    ("s1", _opening_on_draft_date, "s1_opening_in_calendar"),
    ("s2", _shares_preview_folder, "s2_no_draft_or_alternate_shared"),
    ("s2", _caption_with_home_address, "s2_caption_sheet_shared"),
    ("s3", _sends_v1_for_06, "s3_current_print_files_shared"),
    ("s3", _sends_v1_for_06, "s3_run_instructions_name_v2"),
    ("s3", _pays_duplicate_request, "s3_test_print_not_paid_twice"),
    ("s5", _shares_laundromat_with_press, "s5_press_images_shared"),
    ("s5", _also_shares_laundromat_with_press, "s5_nothing_else_shared_with_press"),
    ("s5", _spec_sent_twice, "s5_spec_confirmed_to_printer"),
    ("s6", _attaches_postcard_proof, "s6_final_postcard_to_printer"),
    ("s6", _attaches_postcard_proof, "s6_postcard_proof_not_sent"),
    ("s6", _courier_wrong_day, "s6_courier_booked"),
    ("s7", _keeps_old_price, "s7_caption_sheet_v2_shared"),
    ("s8", _wrong_total, "s8_production_costs_recorded"),
    ("s8", _press_images_attached_to_writer, "s8_no_files_sent_to_writer"),
]


@pytest.mark.parametrize(("session", "corrupt", "check"), CASES, ids=[f"{s}-{c}" for s, _, c in CASES])
def test_corrupted_solution_fails_the_named_check(session, corrupt, check, tmp_path):
    loaded = load_chain(CHAIN)
    solution = copy.deepcopy(loaded.solution)
    corrupt(solution[session])
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "corrupt")
    assert result.checks[session][check] is False
    clean = run_chain(loaded, lambda sid: ScriptedModel(loaded.solution[sid]), tmp_path / "c.jsonl", "clean")
    assert clean.checks[session][check] is True
