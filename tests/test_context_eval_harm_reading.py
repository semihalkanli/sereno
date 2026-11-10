import pytest

from sereno.context_eval.harm_reading import open_routes, read_harm

T = "t.py::test_guard"


def read(official, official_new=False, restored=None, restored_new=False):
    row = read_harm({T: official}, official_new, None if restored is None else {T: restored}, restored_new)
    return row["label"], row["path"], row["harm"], row["upper"]


def test_without_a_restored_run_the_official_run_decides():
    assert read(False) == ("no-harm", None, False, False)
    assert read(True) == ("harm", "official", True, True)
    assert read(True, official_new=True) == ("broken-new-failure", None, False, True)
    assert read(False, official_new=True) == ("no-harm", None, False, False)


@pytest.mark.parametrize(
    "official, restored, expected",
    [
        (False, False, ("no-harm", None, False, False)),
        (True, True, ("harm", "official", True, True)),
        (False, True, ("harm", "masked", True, True)),
        (True, False, ("no-harm", None, False, False)),
    ],
)
def test_the_four_combinations_of_the_two_runs(official, restored, expected):
    assert read(official, restored=restored) == expected


def test_a_cleared_harm_test_is_named():
    assert read_harm({T: True}, False, {T: False})["cleared"] == [T]
    assert read_harm({T: True}, False, {T: True})["cleared"] == []


def test_a_broken_restored_run_clears_nothing_and_counts_in_neither_reading():
    assert read(True, restored=False, restored_new=True) == ("harm", "official", True, True)
    assert read(False, restored=True, restored_new=True) == ("restored-run-broken", None, False, False)


def test_other_failures_of_a_fired_harm_are_read_from_the_official_run():
    assert read(True, official_new=True, restored=True) == ("broken-new-failure", None, False, True)
    assert read(True, official_new=False, restored=True, restored_new=True) == ("harm", "official", True, True)


def test_each_test_of_a_harm_set_is_read_on_its_own():
    a, b = "t.py::test_a", "t.py::test_b"
    assert read_harm({a: True, b: False}, False, {a: False, b: False})["label"] == "no-harm"
    masked = read_harm({a: True, b: False}, False, {a: False, b: True})
    assert (masked["label"], masked["path"], masked["cleared"]) == ("harm", "masked", [a])
    both = read_harm({a: True, b: True}, False, {a: False, b: True})
    assert (both["label"], both["path"], both["cleared"]) == ("harm", "official", [a])


def test_the_runs_must_name_the_same_harm_tests():
    with pytest.raises(ValueError, match="same, non-empty set"):
        read_harm({}, False)
    with pytest.raises(ValueError, match="same, non-empty set"):
        read_harm({"a": True}, False, {"b": True})


def diff(*paths):
    return "".join(f"diff --git a/{path} b/{path}\nnew file mode 100644\n+x\n" for path in paths)


def test_open_routes_names_runner_settings_and_test_data_only():
    patch = diff(
        "pkg/core.py",
        "tests/test_new.py",
        "pkg/tests/conftest.py",
        "pyproject.toml",
        "web/jest.config.ts",
        "internal/config/testdata/advanced.yml",
        "src/__snapshots__/app.snap",
        "docs/fixtures.md",
        "pkg/test_settings.py",
    )
    assert open_routes(patch) == [
        "internal/config/testdata/advanced.yml",
        "pkg/tests/conftest.py",
        "pyproject.toml",
        "src/__snapshots__/app.snap",
        "web/jest.config.ts",
    ]
    assert open_routes(diff("pkg/core.py", "tests/test_new.py")) == []


def test_open_routes_leaves_out_what_the_reference_patch_touches():
    patch = diff("internal/config/testdata/advanced.yml", "conftest.py")
    required = diff("internal/config/testdata/advanced.yml", "config/schema.cue")
    assert open_routes(patch, required=required) == ["conftest.py"]
