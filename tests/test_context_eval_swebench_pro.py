import json

import pytest

from sereno.context_eval import pro_ctrf
from sereno.context_eval.dataset import load_task
from sereno.context_eval.swebench_pro import export, export_task, short_id

BASE = "a" * 40
INSTANCE = f"instance_internetarchive__openlibrary-{'b' * 40}-v{'c' * 40}"


def row(**changes):
    data = {
        "instance_id": INSTANCE,
        "repo": "internetarchive/openlibrary",
        "repo_language": "python",
        "base_commit": BASE,
        "docker_image": "ghcr.io/scaleapi/swe-bench_pro-v2:example",
        "problem_statement": "Fix it.",
        "requirements": "- Keep it.",
        "interface": "",
        "patch": "diff --git a/x.py b/x.py\n",
        "test_patch": "diff --git a/t.py b/t.py\n",
        "fail_to_pass": json.dumps(["t.py::test_new"]),
        "pass_to_pass": json.dumps(["t.py::test_old"]),
        "selected_test_files_to_run": json.dumps(["t.py"]),
        "before_repo_set_cmd": "\n".join(
            [
                f"git reset --hard {BASE}",
                "git clean -fd",
                f"git checkout {BASE}",
                "git apply --verbose /tests/test_patch.patch",
            ]
        ),
    }
    return data | changes


@pytest.fixture
def sources(tmp_path):
    official = tmp_path / "official"
    official.mkdir()
    for name in ("run_script.sh", "parser.py"):
        (official / f"{INSTANCE}.{name}").write_text(f"# {name}\n")
    grader = tmp_path / "grader.py"
    grader.write_text("# grader\n")
    return official, grader, tmp_path / "out"


def test_short_id_keeps_repo_and_commit_prefix():
    assert short_id(INSTANCE) == f"openlibrary-{'b' * 8}"
    assert short_id(f"instance_flipt-io__flipt-{'d' * 40}") == f"flipt-{'d' * 8}"
    with pytest.raises(ValueError):
        short_id("flipt-123")


def test_export_writes_a_task_the_runner_loads(sources):
    official, grader, out = sources
    harm = {"run": ["t2.py"], "node_ids": ["t2.py::test_harm[a b]"]}
    directory = export_task(row(), official, grader, out, harm)
    task = load_task(out, directory.name)
    assert (task.base_commit, task.image) == (BASE, "ghcr.io/scaleapi/swe-bench_pro-v2:example")
    assert task.instruction.startswith("Fix it.\n\nRequirements:\n- Keep it.\n\nIMPORTANT:")
    config = json.loads((directory / "tests" / "config.json").read_text())
    assert config["p2p_node_ids"] == ["t.py::test_old", "t2.py::test_harm[a b]"]
    assert config["harm_node_ids"] == ["t2.py::test_harm[a b]"]
    assert config["source"]["selection"] == ["t.py", "t2.py"]
    script = (directory / "tests" / "test.sh").read_text()
    assert "bash /tests/run_script.sh 't.py,t2.py' >" in script
    for name in ("Dockerfile", "test.patch", "grader.py", "run_script.sh", "parser.py", "to_ctrf.py"):
        assert (directory / "tests" / name).is_file()
    assert (directory / "solution" / "solution.patch").read_text() == "diff --git a/x.py b/x.py\n"


def test_export_refuses_what_the_shared_grader_cannot_reproduce(sources):
    official, grader, out = sources
    extra = row()["before_repo_set_cmd"] + "\ngit checkout HEAD -- other.py"
    with pytest.raises(ValueError, match="setup steps"):
        export_task(row(before_repo_set_cmd=extra), official, grader, out)
    with pytest.raises(ValueError, match="fail-to-pass"):
        export_task(row(), official, grader, out, {"node_ids": ["t.py::test_new"]})
    (official / f"{INSTANCE}.parser.py").write_text("")
    with pytest.raises(FileNotFoundError):
        export_task(row(), official, grader, out)


def test_ctrf_reads_harm_ids_the_official_parser_missed():
    parsed = {"tests": [{"name": "t.py::test_old", "status": "PASSED"}, {"name": "t.py::test_x", "status": "ERROR"}]}
    harm = ["t.py::test_harm[A N\\xf6vel (Ed)]", "t.py::test_harm[b c]", "t.py::test_gone", "t.py::test_old"]
    config = {"f2p_node_ids": [], "p2p_node_ids": ["t.py::test_old", *harm], "harm_node_ids": harm}
    lines = [
        "t.py::test_harm[A N\\xf6vel (Ed)] RERUN [ 10%]",
        "t.py::test_harm[A N\\xf6vel (Ed)] FAILED [ 10%]",
        "t.py::test_harm[b c] PASSED [  20%]",
        "FAILED t.py::test_harm[A N\\xf6vel (Ed)] - AssertionError",
        "t.py::test_harm[A N\\xf6vel (Ed)] PASSED",
        "t.py::test_harm[b c]x PASSED [ 30%]",
        "t.py::test_gone PASSED and more [ 40%]",
    ]
    rows = {t["name"]: t["status"] for t in pro_ctrf.convert(parsed, config, lines)["results"]["tests"]}
    assert rows == {
        "t.py::test_old": "passed",
        "t.py::test_x": "failed",
        "t.py::test_harm[A N\\xf6vel (Ed)]": "failed",
        "t.py::test_harm[b c]": "passed",
    }


def test_ctrf_refuses_a_graded_name_that_both_passed_and_failed():
    def parsed(*rows):
        return {"tests": [{"name": name, "status": status} for name, status in rows]}

    config = {"f2p_node_ids": ["TestNew"], "p2p_node_ids": ["Test_CUE"], "harm_node_ids": ["Test_CUE"]}
    for rows in ([("Test_CUE", "PASSED"), ("Test_CUE", "FAILED")], [("TestNew", "SKIPPED"), ("TestNew", "PASSED")]):
        with pytest.raises(ValueError, match="both passed and did not pass"):
            pro_ctrf.convert(parsed(*rows), config, [])
    same = parsed(("Test_CUE", "FAILED"), ("Test_CUE", "ERROR"), ("TestServer", "PASSED"), ("TestServer", "FAILED"))
    assert [t["status"] for t in pro_ctrf.convert(same, config, [])["results"]["tests"]] == ["failed"] * 2 + [
        "passed",
        "failed",
    ]


def test_a_harm_file_must_cover_every_instance_and_name_only_known_ones(sources, tmp_path):
    official, grader, out = sources
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([row()]))
    entry = {"run": [], "node_ids": ["t.py::test_old"]}
    for content, message in (({}, "has no entry"), ({INSTANCE: entry, INSTANCE + "x": entry}, "not in")):
        (tmp_path / "harm.json").write_text(json.dumps(content))
        with pytest.raises(KeyError, match=message):
            export(rows, official, grader, out, [INSTANCE], tmp_path / "harm.json")
    (tmp_path / "harm.json").write_text(json.dumps({INSTANCE: entry}))
    assert [d.name for d in export(rows, official, grader, out, [INSTANCE], tmp_path / "harm.json")] == [
        short_id(INSTANCE)
    ]
    assert [d.name for d in export(rows, official, grader, out, [INSTANCE])] == [short_id(INSTANCE)]
