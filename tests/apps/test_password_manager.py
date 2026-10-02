import json
from datetime import datetime

import pytest

from sereno.apps.password_manager import Folder, Item, Login, PasswordManager, Uri, _totp
from sereno.checks import Check, Cond, grade
from sereno.tools import Toolset
from sereno.world import Person, World


@pytest.fixture
def world() -> World:
    vault = PasswordManager(
        folders=[Folder(id="f-1", name="Banking")],
        items=[
            Item(
                id="i-1",
                type=1,
                name="Northbank online",
                folderId="f-1",
                login=Login(
                    username="d.okafor",
                    password="Tr4ilhead-Copper",
                    uris=[Uri(uri="https://online.northbank.co.uk")],
                    totp="JBSWY3DPEHPK3PXP",
                ),
            ),
            Item(id="i-2", type=1, name="Studio mail", login=Login(username="daniel@okafor.studio", password="m4il")),
            Item(id="i-3", type=2, name="Wi-Fi at home", notes="SSID Okafor-5G, key 7781-2290"),
        ],
    )
    return World(
        now=datetime(2026, 10, 5, 9, 12),
        owner=Person(name="Daniel Okafor", email="daniel@okafor.studio"),
        apps={"password_manager": vault},
    )


def call(world: World, tool: str, /, **args):
    outcome = Toolset(world, world.tools()).call(f"password_manager__{tool}", args)
    text = outcome.result
    return outcome, (json.loads(text) if text and text[0] in "[{" else text)


def test_list_filters_and_shapes(world):
    _, result = call(world, "list", type="items")
    assert [i["id"] for i in result] == ["i-1", "i-2", "i-3"]
    assert result[0]["object"] == "item" and result[0]["login"]["password"] == "Tr4ilhead-Copper"
    assert result[2]["secureNote"] is None and "login" not in result[2]
    _, result = call(world, "list", type="items", search="northbank")
    assert [i["id"] for i in result] == ["i-1"]
    _, result = call(world, "list", type="items", folderid="null")
    assert [i["id"] for i in result] == ["i-2", "i-3"]
    _, result = call(world, "list", type="items", url="northbank")
    assert [i["id"] for i in result] == ["i-1"]
    _, result = call(world, "list", type="folders")
    assert [f["name"] for f in result] == ["Banking", "No Folder"]


def test_get_by_id_or_unique_search(world):
    _, result = call(world, "get", object="password", id="northbank")
    assert result == "Tr4ilhead-Copper"
    _, result = call(world, "get", object="item", id="i-3")
    assert result["notes"].startswith("SSID")
    outcome, _ = call(world, "get", object="password", id="mail")
    assert outcome.error is None
    outcome, _ = call(world, "get", object="item", id="o")
    assert outcome.error.startswith("More than one result was found")
    outcome, _ = call(world, "get", object="password", id="i-3")
    assert outcome.error == "No password available for this item."
    outcome, _ = call(world, "get", object="item", id="nothing here")
    assert outcome.error == "Not found."


def test_totp_matches_rfc_6238_and_world_clock(world):
    # RFC 6238 appendix B, SHA-1, T = 59 s, secret "12345678901234567890" -> 94287082 (last six digits).
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert _totp(secret, datetime(1970, 1, 1, 0, 0, 59)) == "287082"
    _, result = call(world, "get", object="totp", id="i-1")
    assert result == _totp("JBSWY3DPEHPK3PXP", world.now)
    assert _totp("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&issuer=x", world.now) == result


def test_generate_is_deterministic_and_follows_options(world):
    copy = world.copy()
    _, first = call(world, "generate")
    _, again = call(copy, "generate")
    assert first == again and len(first) == 14
    _, second = call(world, "generate")
    assert second != first
    _, digits = call(world, "generate", length=8, number=True)
    assert len(digits) == 8 and digits.isdigit()
    _, phrase = call(world, "generate", passphrase=True, words=4, separator=".", capitalize=True)
    parts = phrase.split(".")
    assert len(parts) == 4 and all(p[0].isupper() for p in parts)
    outcome, _ = call(world, "generate", length=4)
    assert outcome.error.startswith("Invalid arguments")


def test_create_edit_delete_restore(world):
    outcome, result = call(
        world,
        "create_item",
        name="Council tax",
        type=1,
        login={"username": "dokafor", "password": "x", "uris": [{"uri": "https://council.example"}]},
        folderId="f-1",
    )
    assert outcome.state_changed and result["login"]["uris"] == [{"uri": "https://council.example", "match": None}]
    new_id = result["id"]
    assert result["creationDate"] == "2026-10-05T09:12:00"
    _, result = call(world, "edit_item", id=new_id, login={"password": "y"}, notes="paid yearly")
    assert result["login"]["password"] == "y" and result["login"]["username"] == "dokafor"
    outcome, _ = call(world, "edit_item", id="i-3", login={"password": "z"})
    assert "not a login item" in outcome.error
    outcome, _ = call(world, "create_item", name="Card", type=3)
    assert outcome.error == "card is required for type 3 items."
    outcome, _ = call(world, "create_item", name="Bad", type=2, folderId="f-9")
    assert "was not found" in outcome.error
    call(world, "delete", object="item", id=new_id)
    _, result = call(world, "list", type="items", search="council")
    assert result == []
    _, result = call(world, "list", type="items", trash=True)
    assert [i["id"] for i in result] == [new_id]
    call(world, "restore", object="item", id=new_id)
    _, result = call(world, "list", type="items", search="council")
    assert len(result) == 1
    call(world, "delete", object="item", id=new_id, permanent=True)
    assert all(i.id != new_id for i in world.app("password_manager").items)


def test_folders(world):
    _, result = call(world, "create_folder", name="Home")
    _, renamed = call(world, "edit_folder", id=result["id"], name="House")
    assert renamed["name"] == "House"
    call(world, "delete", object="folder", id="f-1")
    assert world.app("password_manager").items[0].folderId is None


def test_text_send_lifecycle(world):
    outcome, send = call(world, "create_text_send", name="Wi-Fi for guest", text="key 7781-2290", maxAccessCount=1)
    assert outcome.state_changed and send["object"] == "send"
    assert send["accessUrl"] == f"https://vault.example.com/#/send/{send['accessId']}"
    assert send["deletionDate"] == "2026-10-12T09:12:00" and send["passwordSet"] is False
    stored = world.app("password_manager").sends[0]
    assert stored.text == "key 7781-2290" and send["text"] == {"text": "key 7781-2290", "hidden": False}
    _, edited = call(world, "edit_send", id=send["id"], password="pw", disabled=True)
    assert edited["passwordSet"] and edited["disabled"]
    _, cleared = call(world, "remove_send_password", id=send["id"])
    assert cleared["passwordSet"] is False
    _, listed = call(world, "list_send")
    assert [s["id"] for s in listed] == [send["id"]]
    outcome, _ = call(world, "create_text_send", name="x", text="y", expirationDate="soon")
    assert "not ISO 8601" in outcome.error
    call(world, "delete_send", id=send["id"])
    outcome, _ = call(world, "get_send", id=send["id"])
    assert outcome.error == "Send not found."


def test_send_text_is_a_check_target(world):
    pre = world.copy()
    call(world, "create_text_send", name="note", text="password Tr4ilhead-Copper")
    check = Check(
        name="shared",
        check="count",
        app="password_manager",
        collection="sends",
        new=True,
        where={"text": Cond(contains="Tr4ilhead-Copper")},
        equals=1,
    )
    assert grade([check], pre, world) == {"shared": True}
