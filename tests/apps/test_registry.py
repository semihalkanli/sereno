from collections import Counter

from sereno.apps import app_names, get_app


def test_every_app_loads_and_owns_its_tools():
    for name in app_names():
        assert all(t.app == name for t in get_app(name).tools), name


def test_tool_names_are_unique_across_apps():
    counts = Counter(t.name for name in app_names() for t in get_app(name).tools)
    assert [n for n, c in counts.items() if c > 1] == []
