import json
from datetime import datetime

import pytest

from sereno.apps import get_app
from sereno.apps.flights import (
    Agent,
    Airport,
    Baggage,
    Booking,
    Flights,
    Itinerary,
    Leg,
    Offer,
    Passenger,
    Segment,
)
from sereno.tools import Toolset
from sereno.world import Person, World

EMAIL = "emma.clarke@gmail.com"
EMMA = {"title": "ms", "given_name": "Emma", "family_name": "Clarke", "born_on": "1991-04-12"}
TOM = {"given_name": "Tom", "family_name": "Clarke", "born_on": "1989-08-30"}
BA_DIRECT = "13554-2611140715--32480-0-9772-2611141030"
EZY_DIRECT = "13771-2611140620--31915-0-9772-2611140945"
IB_STOP = "13771-2611140900--32132-1-9772-2611141455"
BA_RETURN = "13554-2611140715--32480-0-9772-2611141030|9772-2611211120--32480-0-13554-2611211235"
BA_DEC = "13554-2612050715--32480-0-9772-2612051030"
DEPARTED = "13554-2610010715--32480-0-9772-2610011030"


def _seg(number: str, carrier: str, origin: str, destination: str, dep: str, arr: str) -> Segment:
    return Segment(
        flight_number=number,
        carrier=carrier,
        origin=origin,
        destination=destination,
        departure=datetime.fromisoformat(dep),
        arrival=datetime.fromisoformat(arr),
    )


def _leg(minutes: int, *segments: Segment) -> Leg:
    return Leg(segments=list(segments), duration_minutes=minutes)


def _ba_out(day: str) -> Leg:
    return _leg(135, _seg("BA 478", "British Airways", "LHR", "BCN", f"{day}T07:15", f"{day}T10:30"))


def make_world() -> World:
    state = Flights(
        airports=[
            Airport(iata="LHR", name="London Heathrow", city="London", country="United Kingdom"),
            Airport(iata="LGW", name="London Gatwick", city="London", country="United Kingdom"),
            Airport(iata="MAD", name="Madrid Barajas", city="Madrid", country="Spain"),
            Airport(iata="BCN", name="Barcelona El Prat", city="Barcelona", country="Spain"),
        ],
        agents=[
            Agent(id="baww", name="British Airways", type="airline", rating=4.3, feedback_count=10412),
            Agent(id="ezyy", name="easyJet", type="airline", rating=4.1, feedback_count=8820),
            Agent(id="iber", name="Iberia", type="airline", rating=4.0, feedback_count=3120),
            Agent(id="edus", name="eDreams", type="travel_agent", rating=3.6, feedback_count=5210),
        ],
        itineraries=[
            Itinerary(
                id=BA_DIRECT,
                legs=[_ba_out("2026-11-14")],
                pricing_options=[
                    Offer(
                        id="po-ba-1",
                        agent_id="baww",
                        price=142.0,
                        fare_name="Economy Standard",
                        baggage=Baggage(cabin_bags=1, checked_bags=1),
                        refundable=True,
                        cancellation_fee=50.0,
                        terms="Changes allowed for a fee. Refund minus GBP 50 per passenger.",
                    ),
                    Offer(
                        id="po-edus-1",
                        agent_id="edus",
                        price=128.5,
                        fare_name="Basic",
                        description="Best price with eDreams Prime.",
                        fare_notes="Seats assigned at check-in.",
                        terms="Non-refundable. eDreams service fee applies to changes.",
                    ),
                ],
            ),
            Itinerary(
                id=EZY_DIRECT,
                legs=[_leg(145, _seg("U2 8577", "easyJet", "LGW", "BCN", "2026-11-14T06:20", "2026-11-14T09:45"))],
                pricing_options=[Offer(id="po-ezy-1", agent_id="ezyy", price=89.0, fare_name="Standard")],
            ),
            Itinerary(
                id=IB_STOP,
                legs=[
                    _leg(
                        295,
                        _seg("IB 3163", "Iberia", "LGW", "MAD", "2026-11-14T09:00", "2026-11-14T12:20"),
                        _seg("IB 1934", "Iberia", "MAD", "BCN", "2026-11-14T13:40", "2026-11-14T14:55"),
                    )
                ],
                pricing_options=[Offer(id="po-ib-1", agent_id="iber", price=95.0)],
            ),
            Itinerary(
                id=BA_RETURN,
                legs=[
                    _ba_out("2026-11-14"),
                    _leg(135, _seg("BA 479", "British Airways", "BCN", "LHR", "2026-11-21T11:20", "2026-11-21T12:35")),
                ],
                pricing_options=[Offer(id="po-ba-r1", agent_id="baww", price=260.0)],
            ),
            Itinerary(
                id=BA_DEC,
                legs=[_ba_out("2026-12-05")],
                pricing_options=[Offer(id="po-ba-d1", agent_id="baww", price=70.0)],
            ),
            Itinerary(
                id=DEPARTED,
                legs=[_ba_out("2026-10-01")],
                pricing_options=[Offer(id="po-old", agent_id="baww", price=60.0)],
            ),
            Itinerary(
                id="13554-2611140715--32480-0-9772-2611141030-C",
                legs=[_ba_out("2026-11-14")],
                cabin_class="business",
                pricing_options=[Offer(id="po-ba-c1", agent_id="baww", price=540.0)],
            ),
        ],
    )
    return World(
        now=datetime(2026, 10, 2, 9, 0), owner=Person(name="Emma Clarke", email=EMAIL), apps={"flights": state}
    )


def call(ts: Toolset, name: str, **args):
    out = ts.call(name, args)
    assert out.error is None, out.error
    return json.loads(out.result), out


@pytest.fixture
def ts() -> Toolset:
    world = make_world()
    return Toolset(world, world.tools())


def test_live_search_one_way_by_city_cheapest_first(ts):
    res, out = call(
        ts, "flights_live_search", origin="London", destination="BCN", depart_date="2026-11-14", children_ages=[7]
    )
    assert [i["itinerary_id"] for i in res["itineraries"]] == [EZY_DIRECT, IB_STOP, BA_DIRECT]
    assert res["passengers"] == 2 and res["currency"] == "GBP"
    ba = res["itineraries"][2]
    assert [o["agent_name"] for o in ba["pricing_options"]] == ["eDreams", "British Airways"]
    assert ba["price_from"] == 128.5 and ba["total_from"] == 257.0
    assert ba["legs"][0]["origin_city"] == "London" and ba["legs"][0]["stops"] == 0
    assert not out.state_changed


def test_live_search_direct_only_fastest_and_cabin(ts):
    res, _ = call(
        ts, "flights_live_search", origin="LGW", destination="Barcelona", depart_date="2026-11-14", direct_only=True
    )
    assert [i["itinerary_id"] for i in res["itineraries"]] == [EZY_DIRECT]
    res, _ = call(
        ts, "flights_live_search", origin="London", destination="BCN", depart_date="2026-11-14", sort="fastest"
    )
    assert res["itineraries"][0]["itinerary_id"] == BA_DIRECT
    res, _ = call(
        ts, "flights_live_search", origin="LHR", destination="BCN", depart_date="2026-11-14", cabin_class="business"
    )
    assert res["itineraries"][0]["price_from"] == 540.0


def test_live_search_return_and_departed(ts):
    res, _ = call(
        ts, "flights_live_search", origin="LHR", destination="BCN", depart_date="2026-11-14", return_date="2026-11-21"
    )
    assert [i["itinerary_id"] for i in res["itineraries"]] == [BA_RETURN]
    assert len(res["itineraries"][0]["legs"]) == 2
    res, _ = call(ts, "flights_live_search", origin="LHR", destination="BCN", depart_date="2026-10-01")
    assert res["results_count"] == 0


def test_live_search_errors(ts):
    out = ts.call("flights_live_search", {"origin": "Atlantis", "destination": "BCN", "depart_date": "2026-11-14"})
    assert "Unknown place" in out.error
    out = ts.call(
        "flights_live_search",
        {"origin": "LHR", "destination": "BCN", "depart_date": "2026-11-14", "return_date": "2026-11-10"},
    )
    assert "before the departure" in out.error


def test_indicative_search_by_date_month_and_anytime(ts):
    res, _ = call(
        ts,
        "flights_indicative_search",
        origin="London",
        destination="BCN",
        date_from="2026-11-01",
        date_to="2026-12-31",
    )
    assert [(q["outbound_date"], q["min_price"]) for q in res["quotes"]] == [("2026-11-14", 89.0), ("2026-12-05", 70.0)]
    assert res["quotes"][0]["is_direct"] and res["quotes"][0]["carriers"] == ["easyJet"]
    res, _ = call(ts, "flights_indicative_search", origin="LHR", destination="BCN", grouping="by_month")
    assert [(q["month"], q["min_price"]) for q in res["quotes"]] == [("2026-11", 128.5), ("2026-12", 70.0)]
    assert res["cheapest"]["month"] == "2026-12"
    res, _ = call(ts, "flights_indicative_search", origin="LHR", destination="BCN", return_trip=True)
    assert res["quotes"] == [
        {
            "outbound_date": "2026-11-14",
            "inbound_date": "2026-11-21",
            "min_price": 260.0,
            "is_direct": True,
            "carriers": ["British Airways"],
            "itinerary_id": BA_RETURN,
        }
    ]
    out = ts.call(
        "flights_indicative_search",
        {"origin": "LHR", "destination": "BCN", "date_from": "2026-12-01", "date_to": "2026-11-01"},
    )
    assert "after date_to" in out.error


def test_get_itinerary_shows_provider_content(ts):
    res, _ = call(ts, "flights_get_itinerary", itinerary_id=BA_DIRECT)
    first, second = res["pricing_options"]
    assert first["agent"]["name"] == "eDreams" and first["agent"]["type"] == "travel_agent"
    assert first["fare_notes"] == "Seats assigned at check-in." and "Non-refundable" in first["terms"]
    assert second["baggage"] == {"personal_item": 1, "cabin_bags": 1, "checked_bags": 1}
    assert res["legs"][0]["segments"][0]["flight_number"] == "BA 478"
    assert "No itinerary" in ts.call("flights_get_itinerary", {"itinerary_id": "nope"}).error


def test_book_offer_leaves_booking_record(ts):
    res, out = call(ts, "flights_book_offer", offer_id="po-ba-1", passengers=[EMMA, TOM])
    assert out.state_changed
    assert res["booking_id"] == "FLT-000001" and res["provider"] == "British Airways" and res["total_price"] == 284.0
    assert len(res["provider_reference"]) == 6
    b = ts.world.app("flights").bookings[0]
    assert b.status == "confirmed" and b.contact_email == EMAIL and b.booked_at == ts.world.now
    assert b.baggage.checked_bags == 1 and b.fare_name == "Economy Standard" and "Refund minus" in b.terms
    assert [p.given_name for p in b.passengers] == ["Emma", "Tom"] and b.origin == "LHR"


def test_book_offer_return_and_id_skips_seeded(ts):
    world = ts.world
    seeded = Booking(
        id="FLT-000001",
        provider_reference="QX7P2M",
        itinerary_id=BA_DEC,
        offer_id="po-ba-d1",
        agent_id="baww",
        provider_name="British Airways",
        provider_type="airline",
        origin="LHR",
        destination="BCN",
        departure=datetime(2026, 12, 5, 7, 15),
        cabin_class="economy",
        fare_name="Economy",
        passengers=[Passenger.model_validate(EMMA)],
        baggage=Baggage(),
        price_per_passenger=70.0,
        total_price=70.0,
        currency="GBP",
        refundable=False,
        cancellation_fee=0.0,
        terms="",
        contact_email=EMAIL,
        booked_at=datetime(2026, 9, 20, 18, 0),
    )
    world.app("flights").bookings.append(seeded)
    res, _ = call(ts, "flights_book_offer", offer_id="po-ba-r1", passengers=[EMMA], contact_email="emma@work.example")
    assert res["booking_id"] == "FLT-000002"
    b = world.app("flights").bookings[-1]
    assert b.return_departure == datetime(2026, 11, 21, 11, 20) and b.contact_email == "emma@work.example"


def test_book_offer_errors(ts):
    assert "No offer" in ts.call("flights_book_offer", {"offer_id": "x", "passengers": [EMMA]}).error
    out = ts.call("flights_book_offer", {"offer_id": "po-old", "passengers": [EMMA]})
    assert "already departed" in out.error and not out.state_changed
    assert "between 1 and 9" in ts.call("flights_book_offer", {"offer_id": "po-ba-1", "passengers": []}).error
    assert ts.world.app("flights").bookings == []


def test_list_and_cancel_bookings(ts):
    call(ts, "flights_book_offer", offer_id="po-ba-1", passengers=[EMMA, TOM])
    call(ts, "flights_book_offer", offer_id="po-edus-1", passengers=[EMMA])
    res, out = call(ts, "flights_cancel_booking", booking_id="FLT-000001")
    assert out.state_changed and res["refund_amount"] == 184.0 and res["status"] == "cancelled"
    res, _ = call(ts, "flights_cancel_booking", booking_id="FLT-000002")
    assert res["refund_amount"] == 0.0 and res["provider"] == "eDreams"
    b = ts.world.app("flights").bookings[0]
    assert b.status == "cancelled" and b.cancelled_at == ts.world.now
    assert "already cancelled" in ts.call("flights_cancel_booking", {"booking_id": "FLT-000001"}).error
    assert "No booking" in ts.call("flights_cancel_booking", {"booking_id": "FLT-9"}).error
    res, _ = call(ts, "flights_list_bookings", status="cancelled")
    assert [b["booking_id"] for b in res["bookings"]] == ["FLT-000001", "FLT-000002"]
    assert res["bookings"][0]["passengers"] == ["Emma Clarke", "Tom Clarke"]
    res, _ = call(ts, "flights_list_bookings", status="confirmed")
    assert res["bookings"] == []


def test_price_alerts_create_list_delete(ts):
    res, out = call(ts, "flights_create_price_alert", origin="London", destination="BCN", depart_date="2026-11-14")
    assert out.state_changed and res == {
        "alert_id": "pa-1",
        "status": "active",
        "current_price": 89.0,
        "currency": "GBP",
    }
    dup = ts.call("flights_create_price_alert", {"origin": "London", "destination": "BCN", "depart_date": "2026-11-14"})
    assert "already exists" in dup.error
    ts.world.app("flights").itineraries[1].pricing_options[0].price = 79.0
    res, out = call(ts, "flights_list_price_alerts")
    assert not out.state_changed
    alert = res["price_alerts"][0]
    assert alert["price_when_created"] == 89.0 and alert["current_price"] == 79.0 and alert["change"] == -10.0
    res, out = call(ts, "flights_delete_price_alert", alert_id="pa-1")
    assert out.state_changed and res["status"] == "deleted"
    stored = ts.world.app("flights").price_alerts[0]
    assert stored.status == "deleted" and stored.deleted_at == ts.world.now
    assert "already deleted" in ts.call("flights_delete_price_alert", {"alert_id": "pa-1"}).error
    assert call(ts, "flights_list_price_alerts")[0]["price_alerts"] == []
    assert len(call(ts, "flights_list_price_alerts", include_deleted=True)[0]["price_alerts"]) == 1


def test_price_alert_errors(ts):
    out = ts.call("flights_create_price_alert", {"origin": "LHR", "destination": "BCN", "depart_date": "2026-09-01"})
    assert "in the past" in out.error
    out = ts.call(
        "flights_create_price_alert", {"origin": "Nowhere", "destination": "BCN", "depart_date": "2026-11-14"}
    )
    assert "Unknown place" in out.error
    assert "No price alert" in ts.call("flights_delete_price_alert", {"alert_id": "pa-7"}).error


def test_links_with_travel_without_name_collisions():
    world = make_world()
    world.apps["travel"] = get_app("travel").state()
    names = [t.name for t in world.tools()]
    Toolset(world, world.tools())
    assert all(n.startswith("flights_") for n in names if n in {t.name for t in get_app("flights").tools})
