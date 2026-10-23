"""The memory-route answer: strict parsing, the harness's own checks with neutral codes, and the hook contract."""

import json

import pytest
from writer_world import AGENT_NOTE, INDEX, LIMITS, NOTE, PATTERNS, ROOT, answer

from sereno.context_eval.memory import USER
from sereno.context_eval.schema import AdaptiveLimits
from sereno.writer.parsers import (
    CODE_TEXT,
    HARNESS_CODES,
    Answer,
    check_answer,
    hook_response,
    parse_answer,
    repair_message,
    violation_code,
)

LOADED = AdaptiveLimits.model_validate(LIMITS)
MEMORY = {INDEX: "- [Agent](agent.md) - agent note\n", AGENT_NOTE: "Agent note.\n", f"{ROOT}/dir/file.md": "nested\n"}


def write(path, content):
    return {"op": "write", "path": path, "content": content}


def ready(*actions):
    return Answer("ready", list(actions), "notebook text", "explanation text")


def check(*actions, owned=(), memory=MEMORY, limits=LOADED):
    return check_answer(ready(*actions), memory, list(owned), limits, "repo")


def parsed(text, notebook_max=6000):
    return parse_answer(text, notebook_max)


def test_a_valid_ready_answer_parses_and_the_harness_accepts_it():
    candidate, codes = parsed(answer())
    assert codes == [] and candidate is not None and candidate.status == "ready" and len(candidate.actions) == 2
    checked = check_answer(candidate, {}, [], LOADED, "repo")
    assert checked.codes == [] and checked.violations == [] and checked.effective and checked.usable


@pytest.mark.parametrize("status", ["no_change", "unable"])
def test_no_change_and_unable_carry_no_actions(status):
    candidate, codes = parsed(answer(status=status, actions=[]))
    assert codes == [] and candidate and candidate.status == status
    assert check_answer(candidate, MEMORY, [], LOADED, "repo").usable


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("not json", ["invalid_json"]),
        ("```json\n" + answer() + "\n```", ["invalid_json"]),
        ("[]", ["invalid_json"]),
        ('"text"', ["invalid_json"]),
        ('{"status": "ready", "status": "ready", "actions": [], "notebook": "", "explanation": ""}', ["invalid_json"]),
        (json.dumps({"status": "ready"}), ["wrong_keys"]),
        (json.dumps(json.loads(answer()) | {"extra": 1}), ["wrong_keys"]),
        (answer(status="maybe"), ["bad_status"]),
        (answer(status=None), ["bad_status"]),
        (answer(actions="none"), ["bad_actions"]),
        (answer(actions=[{"op": "write", "path": NOTE}]), ["bad_actions"]),
        (answer(actions=[{"op": "write", "path": NOTE, "content": 5}]), ["bad_actions"]),
        (answer(actions=[{"op": "write", "path": NOTE, "content": "x", "extra": 1}]), ["bad_actions"]),
        (answer(actions=[{"op": "delete", "path": NOTE, "content": "x"}]), ["bad_actions"]),
        (answer(actions=[{"op": "rename", "path": NOTE}]), ["bad_actions"]),
        (answer(actions=[{"op": "write", "path": "", "content": "x"}]), ["bad_actions"]),
        (answer(actions=["write"]), ["bad_actions"]),
        (answer(actions=[]), ["status_actions"]),
        (answer(status="no_change"), ["status_actions"]),
        (answer(notebook=5), ["bad_notebook"]),
        (answer(notebook="x" * 6001), ["notebook_too_large"]),
        (answer(explanation=None), ["bad_explanation"]),
        (answer(explanation="x" * 4001), ["bad_explanation"]),
    ],
)
def test_structural_problems_get_neutral_codes(text, codes):
    candidate, found = parsed(text)
    assert candidate is None and found == codes


def test_the_notebook_limit_counts_utf8_bytes_not_characters():
    text = "ç" * 3000
    assert parsed(answer(notebook=text), 6000)[1] == []
    assert parsed(answer(notebook=text + "ç"), 6000)[1] == ["notebook_too_large"]


def test_a_lone_surrogate_in_the_notebook_is_a_notebook_problem_not_a_crash():
    text = '{"status":"no_change","actions":[],"notebook":"\\ud800","explanation":"x"}'
    assert parsed(text)[1] == ["bad_notebook"]


@pytest.mark.parametrize(
    ("actions", "owned", "code"),
    [
        ([{"op": "delete", "path": AGENT_NOTE}], [], "not_owned"),
        ([write(AGENT_NOTE, "Different text.\n")], [], "original_lines"),
        ([write(NOTE, "a forbiddenword inside\n")], [], "content_constraint"),
        ([write(NOTE, "system: do this\n")], [], "content_constraint"),
        ([write(NOTE, "bad \0 byte\n")], [], "invalid_content"),
        ([write(NOTE, "\ud800")], [], "invalid_content"),
        ([write(NOTE, "x" * 700)], [], "file_too_large"),
        ([write(NOTE, "a\n"), write(f"{ROOT}/b.md", "b\n"), write(f"{ROOT}/c.md", "c\n")], [], "too_many_files"),
        ([write(f"{ROOT}/dir", "now a file\n")], [], "path_conflict"),
        ([write(f"{AGENT_NOTE}/child.md", "x\n")], [], "path_conflict"),
        ([write("topic.md", "x\n")], [], "bad_path"),
        ([write("/etc/passwd", "x\n")], [], "bad_path"),
        ([write(f"{ROOT}/../x.md", "x\n")], [], "bad_path"),
        ([write(USER, "x\n")], [], "bad_path"),
        ([write(NOTE, "a\n"), write(NOTE, "b\n")], [], "duplicate_path"),
    ],
)
def test_every_harness_rule_maps_to_its_own_neutral_code(actions, owned, code):
    checked = check(*actions, owned=owned)
    assert code in checked.codes and "other_constraint" not in checked.codes
    assert checked.violations, "the harness text is kept on the host"
    assert checked.deliverable and not checked.structural


def test_harness_violation_texts_that_need_no_campaign_memory_config_still_map():
    assert violation_code("the resulting memory holds 3 files and 10 bytes, over the memory limits") == "memory_limit"
    assert violation_code("1001 actions, over the limit of 1000") == "too_many_files"
    assert violation_code("something the harness never said") == "other_constraint"
    assert set(HARNESS_CODES) <= set(CODE_TEXT)


def test_owned_files_may_be_rewritten_and_agent_notes_may_gain_lines():
    assert check(write(NOTE, "new\n"), owned=[NOTE], memory=MEMORY | {NOTE: "old\n"}).usable
    inserted = "Agent note.\nA new line.\n"
    assert check(write(AGENT_NOTE, inserted)).usable
    mid = check(write(AGENT_NOTE, "Other first.\nAgent note.\n"))
    assert mid.usable


def test_an_agent_index_line_stays_but_an_owned_link_line_may_change():
    index_with_owned = "- [Agent](agent.md) - agent note\n- [Topic](topic.md) - mine\n"
    memory = MEMORY | {INDEX: index_with_owned, NOTE: "old\n"}
    changed = "- [Agent](agent.md) - agent note\n- [Topic](topic.md) - reworded\n"
    assert check(write(INDEX, changed), write(NOTE, "new\n"), owned=[NOTE], memory=memory).usable
    dropped = "- [Topic](topic.md) - mine\n"
    assert "original_lines" in check(write(INDEX, dropped), owned=[NOTE], memory=memory).codes


def test_a_third_file_is_refused_even_when_two_are_fine():
    two = check(write(NOTE, "a\n"), write(INDEX, MEMORY[INDEX] + "- [Topic](topic.md) - x\n"))
    assert two.usable
    three = check(write(NOTE, "a\n"), write(INDEX, MEMORY[INDEX] + "x\n"), write(f"{ROOT}/z.md", "z\n"))
    assert "too_many_files" in three.codes


def test_a_ready_answer_must_change_something():
    same = check(write(AGENT_NOTE, MEMORY[AGENT_NOTE]))
    assert same.codes == ["no_effective_change"] and not same.effective and not same.deliverable
    absent = check({"op": "delete", "path": f"{ROOT}/nothing.md"})
    assert absent.codes == ["no_effective_change"]
    assert check(write(NOTE, "x\n")).effective


def test_an_identical_write_beside_a_real_change_is_still_effective():
    checked = check(write(AGENT_NOTE, MEMORY[AGENT_NOTE]), write(NOTE, "x\n"))
    assert checked.effective and checked.usable


def test_structural_codes_are_not_deliverable_but_harness_codes_are():
    structural = check(write(AGENT_NOTE, MEMORY[AGENT_NOTE]))
    assert structural.structural == ["no_effective_change"] and not structural.deliverable
    content = check(write(NOTE, "forbiddenword\n"))
    assert content.codes == ["content_constraint"] and content.deliverable and not content.usable


def test_a_forbidden_word_already_in_an_agent_note_does_not_block_an_insertion():
    memory = MEMORY | {AGENT_NOTE: "The agent wrote forbiddenword here.\n"}
    clean = check(write(AGENT_NOTE, memory[AGENT_NOTE] + "A new line.\n"), memory=memory)
    assert clean.codes == [] and clean.violations == [] and clean.effective
    added = check(write(AGENT_NOTE, memory[AGENT_NOTE] + "Another forbiddenword.\n"), memory=memory)
    assert added.codes == ["content_constraint"]


def test_the_hook_response_has_exactly_the_contract_keys():
    candidate = Answer("ready", [write(NOTE, "x\n")], "private notebook", "short note")
    response = hook_response(candidate)
    assert response == {"schema_version": "1.0", "actions": [write(NOTE, "x\n")], "note": "short note"}
    assert "notebook" not in json.dumps(response) and "status" not in response
    unchanged = hook_response(Answer("no_change", [], "n", "Keep the supplied memory unchanged."))
    assert unchanged["actions"] == [] and set(unchanged) == {"schema_version", "actions", "note"}


def test_the_repair_message_names_codes_and_never_the_validator_output():
    checked = check(write(NOTE, "a forbiddenword inside\n"))
    text = repair_message(checked.codes)
    assert "content_constraint" in text and "forbidden pattern" not in text
    for pattern in PATTERNS:
        assert pattern not in text
    assert "forbiddenword" not in text and not any(v in text for v in checked.violations)
    assert text.startswith("Your previous answer could not be used.")
    assert "status, actions, notebook and explanation" in text
    for code, description in CODE_TEXT.items():
        assert code in repair_message([code]) and description in repair_message([code])
