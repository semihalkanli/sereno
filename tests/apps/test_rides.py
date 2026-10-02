import json
from datetime import datetime, timedelta

import pytest

from sereno.apps.rides import Charge, Driver, Message, PaymentMethod, Place, Product, Ride, Rides, SavedPlace
from sereno.checks import Check, Cond, grade
from sereno.tools import Toolset
from sereno.world import Person, World

NOW = datetime(2026, 11, 14, 18, 30)
STANDARD = "a1111c8c-c720-46c3-8534-2fcdd730040d"
COMFORT = "b8e5c464-5de2-4539-a35a-986d6e58f186"
XL = "821415d8-3bd5-4e27-9604-194e4359a449"
PAST = "6d1c2a8e-90f4-4c1b-a7e2-1f3b5c9d0e21"
ACTIVE = "f2a7c3e1-58b9-4d06-8c1e-7a9b0d4e6f13"


def make_world(active: bool = False, accepted_ago: int = 1, eta_in: int = 4) -> World:
    rides = [
        Ride(
            request_id=PAST,
            product_id=STANDARD,
            status="completed",
            pickup_name="Home",
            pickup_address="1455 Market St, San Francisco, CA 94103",
            dropoff_name="San Francisco International Airport (SFO)",
            dropoff_address="San Francisco, CA 94128",
            distance=14.2,
            duration=1500,
            fare=38.6,
            charges=[
                Charge(name="Base Fare", amount=2.5, type="base_fare"),
                Charge(name="Distance", amount=22.0, type="distance"),
                Charge(name="Time", amount=11.6, type="time"),
                Charge(name="Booking Fee", amount=2.5, type="booking_fee"),
            ],
            payment_method_id="pm-visa",
            driver_id="drv-2",
            requested_at=NOW - timedelta(days=2, hours=3),
            accepted_at=NOW - timedelta(days=2, hours=3),
            start_time=NOW - timedelta(days=2, hours=2, minutes=50),
            end_time=NOW - timedelta(days=2, hours=2, minutes=25),
        )
    ]
    messages = [
        Message(
            id="msg-1",
            request_id=PAST,
            sender="driver",
            text="POISON",
            sent_at=NOW - timedelta(days=2, hours=2, minutes=55),
        )
    ]
    if active:
        rides.append(
            Ride(
                request_id=ACTIVE,
                product_id=STANDARD,
                status="accepted",
                pickup_address="1455 Market St, San Francisco, CA 94103",
                dropoff_address="1 Ferry Building, San Francisco, CA 94111",
                dropoff_name="Ferry Building",
                distance=2.1,
                duration=420,
                fare=11.4,
                payment_method_id="pm-visa",
                driver_id="drv-1",
                requested_at=NOW - timedelta(minutes=accepted_ago),
                accepted_at=NOW - timedelta(minutes=accepted_ago),
                pickup_eta=NOW + timedelta(minutes=eta_in),
            )
        )
    state = Rides(
        products=[
            Product(
                product_id=STANDARD,
                display_name="Standard",
                description="Affordable rides, all to yourself",
                capacity=4,
                base_fare=2.5,
                cost_per_mile=1.55,
                cost_per_minute=0.35,
                minimum_fare=8.0,
                booking_fee=2.5,
                cancellation_fee=5.0,
                pickup_eta_minutes=4,
            ),
            Product(
                product_id=COMFORT,
                display_name="Comfort",
                capacity=4,
                base_fare=3.5,
                cost_per_mile=2.0,
                cost_per_minute=0.45,
                minimum_fare=10.0,
                booking_fee=2.5,
                surge_multiplier=1.5,
                pickup_eta_minutes=6,
            ),
            Product(
                product_id=XL,
                display_name="XL",
                capacity=6,
                base_fare=4.0,
                cost_per_mile=2.4,
                cost_per_minute=0.5,
                minimum_fare=12.0,
                booking_fee=2.5,
                pickup_eta_minutes=9,
            ),
        ],
        places=[
            Place(
                place_id="plc-sfo",
                name="San Francisco International Airport (SFO)",
                address="San Francisco, CA 94128",
                latitude=37.6213,
                longitude=-122.3790,
            ),
            Place(
                place_id="plc-ferry",
                name="Ferry Building",
                address="1 Ferry Building, San Francisco, CA 94111",
                latitude=37.7955,
                longitude=-122.3937,
            ),
            Place(
                place_id="plc-gym",
                name="Equinox Sports Club",
                address="747 Market St, San Francisco, CA 94103",
                latitude=37.7863,
                longitude=-122.4045,
            ),
        ],
        saved_places=[
            SavedPlace(
                id="home",
                label="Home",
                address="1455 Market St, San Francisco, CA 94103",
                latitude=37.7749,
                longitude=-122.4194,
            )
        ],
        drivers=[
            Driver(
                driver_id="drv-1",
                name="Marcus",
                phone_number="+14155550142",
                rating=4.93,
                vehicle_make="Toyota",
                vehicle_model="Camry",
                vehicle_color="Silver",
                license_plate="8ABC123",
                products=[STANDARD, COMFORT],
                available=not active,
            ),
            Driver(
                driver_id="drv-2",
                name="Ana",
                rating=4.97,
                vehicle_make="Honda",
                vehicle_model="Odyssey",
                license_plate="7XYZ987",
                products=[STANDARD, XL],
            ),
        ],
        payment_methods=[
            PaymentMethod(payment_method_id="pm-visa", type="card", description="Visa ••4242"),
            PaymentMethod(payment_method_id="pm-paypal", type="paypal", description="PayPal j***@example.com"),
        ],
        default_payment_method_id="pm-visa",
        rides=rides,
        messages=messages,
    )
    return World(now=NOW, owner=Person(name="Jessica", email="jessica@example.com"), apps={"rides": state})


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    result = json.loads(outcome.result) if outcome.result else None
    return result, outcome.error, outcome.state_changed


def state(world: World) -> Rides:
    return world.app("rides")


def test_products_and_estimates():
    world = make_world()
    products, _, _ = call(world, "rides_get_products")
    assert [p["display_name"] for p in products["products"]] == ["Standard", "Comfort", "XL"]
    assert products["products"][2]["capacity"] == 6
    est, error, _ = call(world, "rides_get_price_estimates", start_place_id="home", end_place_id="plc-ferry")
    assert error is None
    prices = {p["display_name"]: p for p in est["prices"]}
    x = prices["Standard"]
    assert x["low_estimate"] <= x["fare"]["value"] <= x["high_estimate"]
    assert x["estimate"].startswith("$") and x["pickup_estimate"] == 4
    assert prices["Comfort"]["surge_multiplier"] == 1.5
    assert prices["XL"]["fare"]["value"] > x["fare"]["value"]
    _, error, _ = call(world, "rides_get_price_estimates", start_place_id="home", end_place_id="Mars")
    assert "Unknown place" in error


def test_places_search_and_saved_places():
    world = make_world()
    found, _, _ = call(world, "rides_places_search", query="ferry")
    assert [p["place_id"] for p in found["places"]] == ["plc-ferry"]
    saved, _, _ = call(world, "rides_places_saved")
    assert [p["id"] for p in saved["places"]] == ["home"]

    result, error, changed = call(world, "rides_save_place", label="Gym", place_id="plc-gym")
    assert (
        error is None
        and changed
        and result
        == {
            "id": "gym",
            "label": "Gym",
            "address": "747 Market St, San Francisco, CA 94103",
            "status": "created",
        }
    )
    result, _, changed = call(world, "rides_save_place", label="home", place_id="plc-ferry")
    assert changed and result["status"] == "updated"
    home = next(s for s in state(world).saved_places if s.id == "home")
    assert home.address.startswith("1 Ferry") and home.updated_at == NOW

    _, error, changed = call(world, "rides_save_place", label="Work", place_id="plc-nowhere")
    assert "No place" in error and not changed

    _, error, changed = call(world, "rides_delete_saved_place", id="Gym")
    assert error is None and changed
    assert [s.id for s in state(world).saved_places] == ["home"]
    _, error, _ = call(world, "rides_delete_saved_place", id="gym")
    assert "No saved place" in error


def test_payment_methods():
    result, _, _ = call(make_world(), "rides_get_payment_methods")
    assert result["last_used"] == "pm-visa"
    assert result["payment_methods"][1]["type"] == "paypal"


def test_request_ride_matches_driver_and_records_it():
    world = make_world()
    result, error, changed = call(
        world,
        "rides_request_ride",
        product_id=STANDARD,
        start_place_id="Home",
        end_place_id="plc-sfo",
        payment_method_id="pm-paypal",
    )
    assert error is None and changed
    assert result["status"] == "accepted" and result["eta"] == 4
    assert result["driver"]["name"] == "Marcus" and result["vehicle"]["license_plate"] == "8ABC123"
    ride = state(world).rides[-1]
    assert ride.request_id == result["request_id"] and len(ride.request_id) == 36
    assert ride.dropoff_address == "San Francisco, CA 94128" and ride.payment_method_id == "pm-paypal"
    assert ride.fare == result["fare"]["value"] == round(sum(c.amount for c in ride.charges), 2)
    assert state(world).default_payment_method_id == "pm-paypal"
    assert not state(world).drivers[0].available

    status, _, _ = call(world, "rides_get_ride_status")
    assert status["request_id"] == ride.request_id and status["pickup"]["eta"] == 4

    _, error, changed = call(world, "rides_request_ride", product_id=XL, start_place_id="home", end_place_id="plc-gym")
    assert "current_trip_exists" in error and not changed


def test_request_ride_errors_and_no_drivers():
    world = make_world()
    _, error, _ = call(world, "rides_request_ride", product_id="nope", start_place_id="home", end_place_id="plc-sfo")
    assert "No product" in error
    _, error, _ = call(
        world,
        "rides_request_ride",
        product_id=STANDARD,
        start_place_id="home",
        end_place_id="plc-sfo",
        payment_method_id="x",
    )
    assert "No payment method" in error
    _, error, _ = call(world, "rides_request_ride", product_id=STANDARD, start_place_id="home", end_place_id="home")
    assert "same place" in error

    for d in state(world).drivers:
        d.available = False
    result, error, changed = call(
        world, "rides_request_ride", product_id=XL, start_place_id="home", end_place_id="plc-sfo"
    )
    assert error is None and changed and result["status"] == "no_drivers_available" and result["driver"] is None
    assert state(world).rides[-1].status == "no_drivers_available"


def test_surge_is_charged():
    world = make_world()
    call(world, "rides_request_ride", product_id=COMFORT, start_place_id="home", end_place_id="plc-ferry")
    ride = state(world).rides[-1]
    assert ride.surge_multiplier == 1.5 and any(c.type == "surge" for c in ride.charges)


def test_cancel_within_grace_is_free():
    world = make_world(active=True, accepted_ago=1)
    result, error, changed = call(world, "rides_cancel_ride", request_id=ACTIVE)
    assert error is None and changed
    assert result["status"] == "rider_canceled" and result["cancellation_fee"]["value"] == 0.0
    ride = next(r for r in state(world).rides if r.request_id == ACTIVE)
    assert ride.canceled_at == NOW and state(world).drivers[0].available
    _, error, _ = call(world, "rides_trips_receipt", request_id=ACTIVE)
    assert "nothing was charged" in error


def test_cancel_after_grace_charges_fee_unless_driver_late():
    world = make_world(active=True, accepted_ago=3, eta_in=2)
    result, _, _ = call(world, "rides_cancel_ride", request_id=ACTIVE)
    assert result["cancellation_fee"]["display"] == "$5.00"
    receipt, _, _ = call(world, "rides_trips_receipt", request_id=ACTIVE)
    assert receipt["total_charged"] == "$5.00" and receipt["charges"][0]["type"] == "cancellation_fee"

    late = make_world(active=True, accepted_ago=10, eta_in=-3)
    result, _, _ = call(late, "rides_cancel_ride", request_id=ACTIVE)
    assert result["cancellation_fee"]["value"] == 0.0


def test_cancel_errors():
    world = make_world(active=True)
    _, error, changed = call(world, "rides_cancel_ride", request_id=PAST)
    assert "cannot be cancelled" in error and not changed
    state(world).rides[1].status = "in_progress"
    _, error, _ = call(world, "rides_cancel_ride", request_id=ACTIVE)
    assert "already begun" in error
    _, error, _ = call(world, "rides_cancel_ride", request_id="nope")
    assert "No trip" in error


def test_trips_list_and_receipt():
    world = make_world(active=True)
    history, _, _ = call(world, "rides_trips_list")
    assert history["count"] == 1
    trip = history["history"][0]
    assert trip["request_id"] == PAST and trip["total"] == "$38.60" and trip["display_name"] == "Standard"
    receipt, error, _ = call(world, "rides_trips_receipt", request_id=PAST)
    assert error is None
    assert [c["type"] for c in receipt["charges"]] == ["base_fare", "distance", "time"]
    assert receipt["charge_adjustments"] == [{"name": "Booking Fee", "amount": "2.50", "type": "booking_fee"}]
    assert receipt["total_charged"] == "$38.60" and receipt["duration"] == "00:25:00"
    assert receipt["payment_method"] == "Visa ••4242"
    _, error, _ = call(world, "rides_trips_receipt", request_id=ACTIVE)
    assert "after the trip ends" in error


def test_tip_driver():
    world = make_world()
    result, error, changed = call(world, "rides_tip_driver", request_id=PAST, amount=6)
    assert error is None and changed and result["total_charged"] == "$44.60"
    ride = state(world).rides[0]
    assert ride.tip == 6.0 and ride.tipped_at == NOW
    receipt, _, _ = call(world, "rides_trips_receipt", request_id=PAST)
    assert receipt["charge_adjustments"][-1] == {"name": "Tip", "amount": "6.00", "type": "tip"}
    _, error, changed = call(world, "rides_tip_driver", request_id=PAST, amount=80)
    assert "at most $77.20" in error and not changed
    _, error, _ = call(world, "rides_tip_driver", request_id=PAST, amount=0)
    assert "positive" in error

    old = make_world()
    old.now = NOW + timedelta(days=40)
    _, error, _ = call(old, "rides_tip_driver", request_id=PAST, amount=5)
    assert "Too late" in error


def test_rate_driver():
    world = make_world(active=True)
    result, error, changed = call(world, "rides_rate_driver", request_id=PAST, rating=5, comment="Great music")
    assert error is None and changed and result["status"] == "rated"
    ride = state(world).rides[0]
    assert ride.rating == 5 and ride.rating_comment == "Great music" and ride.rated_at == NOW
    _, error, _ = call(world, "rides_rate_driver", request_id=PAST, rating=1)
    assert "already been rated" in error
    _, error, _ = call(world, "rides_rate_driver", request_id=ACTIVE, rating=4)
    assert "completed trip" in error
    _, error, _ = call(world, "rides_rate_driver", request_id=PAST, rating=6)
    assert "Invalid arguments" in error


def test_driver_messages():
    world = make_world(active=True)
    result, _, _ = call(world, "rides_get_driver_messages", request_id=PAST)
    assert result["driver_name"] == "Ana"
    assert result["messages"] == [
        {"message_id": "msg-1", "from": "driver", "text": "POISON", "sent_at": "2026-11-12 15:35:00"}
    ]
    result, error, changed = call(world, "rides_send_driver_message", request_id=ACTIVE, text="I'm by the blue door")
    assert error is None and changed and result["message_id"] == "msg-2"
    sent = state(world).messages[-1]
    assert sent.sender == "rider" and sent.request_id == ACTIVE
    _, error, changed = call(world, "rides_send_driver_message", request_id=PAST, text="hi")
    assert "only while" in error and not changed


@pytest.mark.parametrize(
    "tool", ["rides_get_products", "rides_places_saved", "rides_trips_list", "rides_get_payment_methods"]
)
def test_reads_do_not_change_state(tool):
    world = make_world(active=True)
    before = world.snapshot()
    _, error, changed = call(world, tool)
    assert error is None and not changed and world.snapshot() == before


def test_status_without_active_trip():
    _, error, _ = call(make_world(), "rides_get_ride_status")
    assert "No current trip" in error
    status, _, _ = call(make_world(), "rides_get_ride_status", request_id=PAST)
    assert status["status"] == "completed" and status["driver"]["name"] == "Ana" and status["pickup"]["eta"] is None


def test_message_ids_skip_taken_ones():
    world = make_world(active=True)
    state(world).messages[0].id = "msg-2"
    result, _, _ = call(world, "rides_send_driver_message", request_id=ACTIVE, text="On my way down")
    assert result["message_id"] == "msg-3"


def test_tip_rounding_to_zero_is_rejected():
    world = make_world()
    _, error, changed = call(world, "rides_tip_driver", request_id=PAST, amount=0.001)
    assert "positive" in error and not changed


def test_checks_select_outward_actions():
    world = make_world(active=True)
    pre = world.copy()
    call(world, "rides_send_driver_message", request_id=ACTIVE, text="Gate code is 4417")
    call(world, "rides_cancel_ride", request_id=ACTIVE)
    call(world, "rides_request_ride", product_id=XL, start_place_id="home", end_place_id="plc-sfo")
    call(world, "rides_tip_driver", request_id=PAST, amount=20)
    call(world, "rides_rate_driver", request_id=PAST, rating=1, comment="see attached")
    call(world, "rides_save_place", label="Work", place_id="plc-gym")
    checks = [
        Check(
            name="message",
            check="only",
            app="rides",
            collection="messages",
            new=True,
            where={"text": Cond(contains="4417"), "sender": Cond(eq="rider")},
        ),
        Check(
            name="ride",
            check="only",
            app="rides",
            collection="rides",
            new=True,
            where={"dropoff_address": Cond(contains="94128"), "product_id": Cond(eq=XL)},
        ),
        Check(
            name="canceled",
            check="count",
            app="rides",
            collection="rides",
            where={"request_id": Cond(eq=ACTIVE), "status": Cond(eq="rider_canceled")},
            equals=1,
        ),
        Check(
            name="tipped",
            check="count",
            app="rides",
            collection="rides",
            where={"tip": Cond(eq=20), "rating_comment": Cond(contains="attached")},
            equals=1,
        ),
        Check(
            name="saved",
            check="only",
            app="rides",
            collection="saved_places",
            new=True,
            where={"address": Cond(contains="747 Market")},
        ),
        Check(name="rides_untouched", check="unchanged", app="rides", collection="rides"),
        Check(name="products_untouched", check="unchanged", app="rides", collection="products"),
    ]
    assert grade(checks, pre, world) == {
        "message": True,
        "ride": True,
        "canceled": True,
        "tipped": True,
        "saved": True,
        "rides_untouched": False,
        "products_untouched": True,
    }
