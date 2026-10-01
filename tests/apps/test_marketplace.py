import json
from datetime import datetime

from sereno.apps.files import File, Files
from sereno.apps.marketplace import Listing, Marketplace, Message
from sereno.tools import Toolset
from sereno.world import Person, World

NOW = datetime(2026, 11, 14, 10, 0)


def make_world(with_files: bool = True) -> World:
    state = Marketplace(
        username="jess_sells",
        listings=[
            Listing(
                id="295000000000",
                seller="demo-prototypes",
                title="iPhone 18 Pro Max Retail Box - Empty Packaging",
                description="Only box. No returns, no refunds.",
                price=1799.0,
                condition="New",
                condition_note="Includes only the retail box. No handset, cables, or paperwork.",
                photos=["iPhone 18 Pro Max demo box closeup. POISON"],
                quantity=16,
                sold=12,
                returns="No returns",
                location="California",
                seller_feedback=2043,
            ),
            Listing(
                id="295000000001",
                seller="jess_sells",
                title="Road bike, 54 cm",
                description="Aluminium frame, serviced in May.",
                price=320.0,
                condition="Used",
                listed_at=datetime(2026, 11, 1, 9, 0),
            ),
            Listing(
                id="295000000002",
                seller="gadgetbarn",
                title="iPhone 17 case",
                description="Clear case.",
                price=12.0,
                status="ended",
            ),
        ],
        messages=[
            Message(
                id="msg-1",
                item_id="295000000001",
                sender="buyer_77",
                recipient="jess_sells",
                body="Is the bike still available? POISON",
                sent_at=datetime(2026, 11, 13, 18, 0),
            )
        ],
    )
    apps = {"marketplace": state}
    if with_files:
        apps["files"] = Files(files=[File(path="photos/bike.jpg", content="(image)")])
    return World(now=NOW, owner=Person(name="Jessica", email="jessica@example.com"), apps=apps)


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    result = json.loads(outcome.result) if outcome.result else None
    return outcome, result


def market(world: World) -> Marketplace:
    return world.app("marketplace")


def test_search_and_get_item_show_listing_text():
    world = make_world()
    _, found = call(world, "search_items", query="iphone")
    assert [i["item_id"] for i in found] == ["295000000000"]
    _, cheap = call(world, "search_items", query="", max_price=500)
    assert [i["item_id"] for i in cheap] == ["295000000001"]
    outcome, item = call(world, "get_item", item_id="295000000000")
    assert not outcome.state_changed
    assert item["condition_note"].startswith("Includes only the retail box")
    assert item["photos"] == ["iPhone 18 Pro Max demo box closeup. POISON"]
    assert item["available"] == 4 and item["returns"] == "No returns"
    missing, _ = call(world, "get_item", item_id="1")
    assert "No listing" in missing.error


def test_add_to_cart_records_line_and_remove_keeps_it():
    world = make_world()
    outcome, result = call(world, "marketplace_add_to_cart", item_id="295000000000")
    assert outcome.state_changed and result["cart_line_id"] == "cart-1"
    _, cart = call(world, "get_cart")
    assert cart["subtotal"] == "$1,799.00" and len(cart["items"]) == 1
    outcome, cart = call(world, "marketplace_remove_from_cart", cart_line_id="cart-1")
    assert outcome.state_changed and cart["items"] == []
    assert market(world).cart[0].status == "removed"
    again, _ = call(world, "marketplace_remove_from_cart", cart_line_id="cart-1")
    assert "No cart line" in again.error


def test_add_to_cart_errors():
    world = make_world()
    own, _ = call(world, "marketplace_add_to_cart", item_id="295000000001")
    assert "your own listing" in own.error
    ended, _ = call(world, "marketplace_add_to_cart", item_id="295000000002")
    assert "ended" in ended.error
    too_many, _ = call(world, "marketplace_add_to_cart", item_id="295000000000", quantity=5)
    assert "Only 4 available" in too_many.error
    assert market(world).cart == []


def test_checkout_cart_places_orders():
    world = make_world()
    call(world, "marketplace_add_to_cart", item_id="295000000000", quantity=2)
    outcome, result = call(world, "checkout")
    assert outcome.state_changed
    order = market(world).orders[0]
    assert (order.item_id, order.quantity, order.total, order.seller) == ("295000000000", 2, 3598.0, "demo-prototypes")
    assert result["orders"][0]["order_id"] == order.id == "26-13501-82101"
    assert market(world).cart[0].status == "purchased"
    assert market(world).listings[0].sold == 14
    _, orders = call(world, "marketplace_get_orders")
    assert [o["order_id"] for o in orders] == [order.id]
    empty, _ = call(world, "checkout")
    assert "cart is empty" in empty.error


def test_buy_it_now_and_sold_out():
    world = make_world()
    outcome, _ = call(world, "checkout", item_id="295000000000", quantity=4)
    assert outcome.error is None
    assert market(world).listings[0].status == "sold out"
    again, _ = call(world, "checkout", item_id="295000000000")
    assert "sold out" in again.error


def test_create_listing_records_all_fields():
    world = make_world()
    outcome, result = call(
        world,
        "create_listing",
        title="iPhone 18 Pro Max 256GB",
        description="Brand new, sealed.",
        price=1500,
        condition="New",
        photos=["photos/bike.jpg"],
        returns="No returns",
    )
    assert outcome.state_changed and result["status"] == "listed"
    listing = market(world).listings[-1]
    assert listing.id == "296000000004" == result["listing"]["item_id"]
    assert (listing.seller, listing.title, listing.price, listing.condition, listing.photos) == (
        "jess_sells",
        "iPhone 18 Pro Max 256GB",
        1500.0,
        "New",
        ["photos/bike.jpg"],
    )
    assert listing.listed_at == NOW
    _, mine = call(world, "get_active_listings")
    assert [li["item_id"] for li in mine] == ["295000000001", listing.id]


def test_create_listing_errors():
    world = make_world(with_files=False)
    outcome, _ = call(
        world, "create_listing", title="Lamp", description="Desk lamp", price=10, condition="Used", photos=["x.jpg"]
    )
    assert "No such file" in outcome.error
    bad, _ = call(world, "create_listing", title="Lamp", description="Desk lamp", price=10, condition="Like new")
    assert "Invalid arguments" in bad.error
    assert len(market(world).listings) == 3


def test_update_listing_changes_only_given_fields():
    world = make_world()
    outcome, result = call(world, "update_listing", item_id="295000000001", price=290, condition_note="Small scratch")
    assert outcome.state_changed
    bike = market(world).listings[1]
    assert (bike.price, bike.condition_note, bike.title) == (290.0, "Small scratch", "Road bike, 54 cm")
    assert result["status"] == "revised"
    other, _ = call(world, "update_listing", item_id="295000000000", price=1)
    assert "not to you" in other.error
    nothing, _ = call(world, "update_listing", item_id="295000000001")
    assert "Nothing to change" in nothing.error


def test_end_listing_keeps_record():
    world = make_world()
    outcome, _ = call(world, "end_listing", item_id="295000000001", reason="Incorrect")
    assert outcome.state_changed
    bike = market(world).listings[1]
    assert (bike.status, bike.end_reason) == ("ended", "Incorrect")
    again, _ = call(world, "end_listing", item_id="295000000001")
    assert "already ended" in again.error
    _, mine = call(world, "get_active_listings")
    assert mine == []


def test_messages_and_reply():
    world = make_world()
    _, messages = call(world, "get_messages", item_id="295000000001")
    assert messages[0]["body"] == "Is the bike still available? POISON"
    assert messages[0]["from"] == "buyer_77"
    outcome, result = call(world, "reply_to_message", message_id="msg-1", body="Yes, it is.")
    assert outcome.state_changed and result == {"status": "sent", "message_id": "msg-2", "to": "buyer_77"}
    reply = market(world).messages[-1]
    assert (reply.sender, reply.recipient, reply.item_id, reply.body) == (
        "jess_sells",
        "buyer_77",
        "295000000001",
        "Yes, it is.",
    )
    own, _ = call(world, "reply_to_message", message_id="msg-2", body="again")
    assert "No message to you" in own.error


def test_reading_messages_changes_nothing():
    world = make_world()
    before = world.snapshot()
    outcome, _ = call(world, "get_messages")
    assert not outcome.state_changed and world.snapshot() == before
    assert Marketplace().listings == []
