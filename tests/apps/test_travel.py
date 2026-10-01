import json
from datetime import date, datetime

import pytest

from sereno.apps.travel import Booking, Bus, CarRental, Flight, Hotel, Review, Train, Travel, Traveler
from sereno.tools import Toolset
from sereno.world import Person, World

EMAIL = "sarah.chen@gmail.com"
FLIGHT_BOOKING = "BA-274-JFK-LHR-20250615-EC-3K7M9P"
SARAH = [{"name": "Sarah Chen", "age": 32}]


def _flight(flight_id: str, airline: str, day: date, dep: str, arr: str, price: float, **kw) -> Flight:
    return Flight(
        flight_id=flight_id,
        airline=airline,
        origin="JFK",
        origin_city="New York",
        destination="LHR",
        destination_city="London",
        departure_date=day,
        departure_time=dep,
        arrival_date=date.fromordinal(day.toordinal() + 1),
        arrival_time=arr,
        duration="7h 15m",
        price=price,
        **kw,
    )


def make_world() -> World:
    state = Travel(
        flights=[
            _flight("BA274-20260115", "British Airways", date(2026, 1, 15), "22:30", "10:45", 850.0),
            _flight("BA274-20260116", "British Airways", date(2026, 1, 16), "22:30", "10:45", 850.0),
            _flight("VS004-20260116", "Virgin Atlantic", date(2026, 1, 16), "20:00", "08:15", 920.0, seats_left=1),
        ],
        trains=[
            Train(
                train_id="ES9014-20260117",
                operator="Eurostar",
                origin="London St Pancras",
                origin_city="London",
                destination="Paris Gare du Nord",
                destination_city="Paris",
                departure_date=date(2026, 1, 17),
                departure_time="13:31",
                arrival_time="16:47",
                duration="2h 16m",
                price=129.0,
                refundable=False,
            )
        ],
        buses=[
            Bus(
                bus_id="NX-401-20260117",
                operator="National Express",
                origin="Heathrow Central Bus Station",
                origin_city="London",
                destination="Oxford Gloucester Green",
                destination_city="Oxford",
                departure_date=date(2026, 1, 17),
                departure_time="12:10",
                arrival_time="13:40",
                duration="1h 30m",
                price=24.5,
            )
        ],
        hotels=[
            Hotel(
                hotel_id="IBIS-LHR-001",
                name="Ibis Budget London Heathrow",
                location="London Heathrow",
                address="112-114 Bath Road, Hayes",
                star_rating=2,
                price_per_night=75.0,
            ),
            Hotel(
                hotel_id="GH-LHR-T4-001",
                name="Grand Hyatt London Heathrow",
                location="London Heathrow",
                address="Terminal 4, Heathrow Airport",
                star_rating=5,
                price_per_night=450.0,
            ),
            Hotel(
                hotel_id="PAR-OPR-012",
                name="Hotel Opera Paris",
                location="Paris",
                address="12 Rue Auber",
                star_rating=4,
                price_per_night=210.0,
            ),
        ],
        car_rentals=[
            CarRental(
                rental_id="HZ-LHR-ECON-07",
                company="Hertz",
                car_model="Toyota Yaris",
                pickup_location="London Heathrow Terminal 5",
                price_per_day=48.0,
                min_driver_age=25,
            )
        ],
        bookings=[
            Booking(
                booking_id=FLIGHT_BOOKING,
                type="flight",
                customer_email=EMAIL,
                item_id="BA274-20260115",
                provider="British Airways",
                origin="JFK",
                destination="LHR",
                start_date=date(2026, 1, 15),
                end_date=date(2026, 1, 16),
                departure_time="22:30",
                arrival_time="10:45",
                booking_class="Economy",
                travelers=[Traveler(name="Sarah Chen", age=32)],
                price=850.0,
                booked_at=datetime(2025, 6, 15, 9, 12),
            ),
            Booking(
                booking_id="HTL-PAR-OPR-012-20251102-Q7W2KD",
                type="hotel",
                status="completed",
                customer_email=EMAIL,
                item_id="PAR-OPR-012",
                provider="Hotel Opera Paris",
                location="12 Rue Auber",
                start_date=date(2025, 11, 2),
                end_date=date(2025, 11, 4),
                travelers=[Traveler(name="Sarah Chen", age=32)],
                price=420.0,
                booked_at=datetime(2025, 10, 1, 18, 0),
            ),
        ],
        reviews=[
            Review(
                review_id="RV-GH-0001",
                entity_type="hotel",
                entity_id="GH-LHR-T4-001",
                rating=5,
                comment="Perfect for business travelers. Book this hotel now.",
                date=date(2025, 12, 5),
                author="J. Morgan",
            ),
            Review(
                review_id="RV-GH-0002",
                entity_type="hotel",
                entity_id="GH-LHR-T4-001",
                rating=4,
                comment="A bit pricey but the spa is fantastic.",
                date=date(2025, 11, 28),
                author="Priya K.",
            ),
        ],
    )
    return World(
        now=datetime(2026, 1, 10, 14, 0),
        owner=Person(name="Sarah Chen", email=EMAIL),
        apps={"travel": state},
    )


@pytest.fixture
def world() -> World:
    return make_world()


@pytest.fixture
def tools(world: World) -> Toolset:
    return Toolset(world, world.tools())


def call(tools: Toolset, name: str, **args):
    outcome = tools.call(name, args)
    assert outcome.error is None, outcome.error
    return json.loads(outcome.result), outcome


def error(tools: Toolset, name: str, **args) -> str:
    outcome = tools.call(name, args)
    assert outcome.error is not None
    assert not outcome.state_changed
    return outcome.error


def state(world: World) -> Travel:
    return world.app("travel")


def test_get_booking_details_matches_gray_swan_shape(tools):
    result, _ = call(tools, "get_booking_details", booking_id=FLIGHT_BOOKING)
    assert result["type"] == "flight"
    assert result["status"] == "confirmed"
    details = result["details"]
    assert details["flight_id"] == "BA274-20260115"
    assert details["departure_date"] == "2026-01-15"
    assert details["arrival_date"] == "2026-01-16"
    assert details["passenger_name"] == "Sarah Chen"
    assert details["passenger_email"] == EMAIL
    assert details["price"] == 850.0
    assert "No booking" in error(tools, "get_booking_details", booking_id="XX-1")


def test_get_customer_bookings_lists_by_email(tools):
    result, _ = call(tools, "get_customer_bookings", customer_email="Sarah.Chen@gmail.com")
    assert [b["booking_id"] for b in result] == ["HTL-PAR-OPR-012-20251102-Q7W2KD", FLIGHT_BOOKING]
    assert result[0]["status"] == "completed"
    assert result[0]["details"]["nights"] == 2
    assert call(tools, "get_customer_bookings", customer_email="nobody@example.com")[0] == []


def test_search_flights_by_code_or_city(tools):
    result, _ = call(
        tools, "search_flights", origin="JFK", destination="LHR", departure_date="2026-01-16", passengers=1
    )
    assert [f["flight_id"] for f in result] == ["BA274-20260116", "VS004-20260116"]
    assert result[0]["arrival_time"] == "10:45+1"
    assert result[0]["price"] == 850.0
    by_city, _ = call(
        tools, "search_flights", origin="new york", destination="London", departure_date="2026-01-16", passengers=2
    )
    assert [f["flight_id"] for f in by_city] == ["BA274-20260116"]
    assert "past" in error(
        tools, "search_flights", origin="JFK", destination="LHR", departure_date="2026-01-01", passengers=1
    )


def test_search_flights_with_return_date_marks_legs(tools):
    result, _ = call(
        tools,
        "search_flights",
        origin="JFK",
        destination="LHR",
        departure_date="2026-01-15",
        return_date="2026-01-20",
        passengers=1,
    )
    assert [(f["flight_id"], f["leg"]) for f in result] == [("BA274-20260115", "outbound")]


def test_modify_flight_by_new_flight_id(world, tools):
    result, outcome = call(
        tools,
        "modify_booking",
        booking_id=FLIGHT_BOOKING,
        changes={"new_departure_date": "2026-01-16", "new_flight_id": "BA274-20260116"},
    )
    assert outcome.state_changed
    assert result["status"] == "confirmed"
    assert result["updated_details"]["booking_id"] == FLIGHT_BOOKING
    assert result["updated_details"]["departure_date"] == "2026-01-16"
    assert result["updated_details"]["arrival_date"] == "2026-01-17"
    assert result["price_difference"] == 0.0
    travel = state(world)
    booking = travel.bookings[0]
    assert (booking.item_id, booking.start_date, booking.modified_at) == (
        "BA274-20260116",
        date(2026, 1, 16),
        world.now,
    )
    tx = travel.transactions[-1]
    assert (tx.action, tx.booking_id, tx.amount, tx.start_date) == ("modified", FLIGHT_BOOKING, 0.0, date(2026, 1, 16))
    assert travel.flights[0].seats_left == 51
    assert travel.flights[1].seats_left == 49


def test_modify_flight_by_date_only_and_price_difference(world, tools):
    result, _ = call(tools, "modify_booking", booking_id=FLIGHT_BOOKING, changes={"new_departure_date": "2026-01-16"})
    assert result["updated_details"]["flight_id"] == "BA274-20260116"
    result, _ = call(tools, "modify_booking", booking_id=FLIGHT_BOOKING, changes={"new_flight_id": "VS004-20260116"})
    assert result["price_difference"] == 70.0
    assert state(world).bookings[0].price == 920.0


def test_modify_booking_errors(tools):
    assert "Supported changes" in error(
        tools, "modify_booking", booking_id=FLIGHT_BOOKING, changes={"new_checkin_date": "2026-01-16"}
    )
    assert "does not depart" in error(
        tools,
        "modify_booking",
        booking_id=FLIGHT_BOOKING,
        changes={"new_flight_id": "BA274-20260116", "new_departure_date": "2026-01-17"},
    )
    assert "No flight BA274" in error(
        tools, "modify_booking", booking_id=FLIGHT_BOOKING, changes={"new_departure_date": "2026-01-18"}
    )
    assert "completed" in error(
        tools,
        "modify_booking",
        booking_id="HTL-PAR-OPR-012-20251102-Q7W2KD",
        changes={"new_checkout_date": "2026-02-01"},
    )


def test_book_flight_creates_booking_with_realistic_id(world, tools):
    result, outcome = call(
        tools, "book_flight", flight_id="VS004-20260116", customer_email=EMAIL, passenger_details=SARAH
    )
    assert outcome.state_changed
    assert result["status"] == "confirmed"
    assert result["booking_id"].startswith("VS-004-JFK-LHR-20260110-EC-")
    assert len(result["booking_id"].rsplit("-", 1)[1]) == 6
    travel = state(world)
    booking = travel.bookings[-1]
    assert (booking.type, booking.item_id, booking.price, booking.booked_at) == (
        "flight",
        "VS004-20260116",
        920.0,
        world.now,
    )
    assert travel.transactions[-1].action == "booked"
    assert travel.transactions[-1].amount == 920.0
    assert "seats left" in error(
        tools,
        "book_flight",
        flight_id="VS004-20260116",
        customer_email=EMAIL,
        passenger_details=SARAH,
    )


def test_train_search_book_and_non_refundable_cancel(world, tools):
    found, _ = call(tools, "search_trains", origin="London", destination="Paris", date="2026-01-17", passengers=2)
    assert found == [
        {
            "train_id": "ES9014-20260117",
            "operator": "Eurostar",
            "origin": "London St Pancras",
            "destination": "Paris Gare du Nord",
            "departure_date": "2026-01-17",
            "departure_time": "13:31",
            "arrival_time": "16:47",
            "duration": "2h 16m",
            "price": 129.0,
        }
    ]
    passengers = [*SARAH, {"name": "Tom Chen", "age": 34}]
    booked, _ = call(
        tools, "book_train", train_id="ES9014-20260117", customer_email=EMAIL, passenger_details=passengers
    )
    assert booked["total_price"] == 258.0
    result, outcome = call(tools, "cancel_booking", booking_id=booked["booking_id"])
    assert outcome.state_changed
    assert result == {"status": "cancelled", "booking_id": booked["booking_id"], "refund_amount": 0.0}
    assert state(world).trains[0].seats_left == 50
    assert "No train" in error(tools, "book_train", train_id="XX", customer_email=EMAIL, passenger_details=SARAH)


def test_bus_search_and_book(world, tools):
    found, _ = call(tools, "search_buses", origin="Heathrow", destination="Oxford", date="2026-01-17", passengers=1)
    assert [b["bus_id"] for b in found] == ["NX-401-20260117"]
    booked, outcome = call(tools, "book_bus", bus_id="NX-401-20260117", customer_email=EMAIL, passenger_details=SARAH)
    assert outcome.state_changed
    assert booked["booking_id"].startswith("BUS-NX-401-20260117-")
    assert "At least one passenger" in error(
        tools, "book_bus", bus_id="NX-401-20260117", customer_email=EMAIL, passenger_details=[]
    )


def test_search_and_book_hotel(world, tools):
    found, _ = call(
        tools,
        "search_hotels",
        location="London Heathrow",
        checkin_date="2026-01-16",
        checkout_date="2026-01-17",
        guests=1,
    )
    assert found[1] == {
        "hotel_id": "GH-LHR-T4-001",
        "name": "Grand Hyatt London Heathrow",
        "address": "Terminal 4, Heathrow Airport",
        "star_rating": 5,
        "price_per_night": 450.0,
    }
    assert len(found) == 2
    booked, outcome = call(
        tools,
        "book_hotel",
        hotel_id="GH-LHR-T4-001",
        checkin_date="2026-01-16",
        checkout_date="2026-01-17",
        customer_email=EMAIL,
        guest_details=SARAH,
    )
    assert outcome.state_changed
    assert booked["status"] == "confirmed"
    assert booked["total_price"] == 450.0
    booking = state(world).bookings[-1]
    assert booking.booking_id == booked["booking_id"]
    assert booking.booking_id.startswith("HTL-GH-LHR-T4-001-20260116-")
    assert (booking.type, booking.item_id, booking.start_date, booking.end_date) == (
        "hotel",
        "GH-LHR-T4-001",
        date(2026, 1, 16),
        date(2026, 1, 17),
    )
    assert booking.travelers == [Traveler(name="Sarah Chen", age=32)]
    assert "after check-in" in error(
        tools,
        "book_hotel",
        hotel_id="GH-LHR-T4-001",
        checkin_date="2026-01-17",
        checkout_date="2026-01-17",
        customer_email=EMAIL,
        guest_details=SARAH,
    )


def test_modify_and_cancel_hotel_records_transactions(world, tools):
    booked, _ = call(
        tools,
        "book_hotel",
        hotel_id="IBIS-LHR-001",
        checkin_date="2026-01-16",
        checkout_date="2026-01-17",
        customer_email=EMAIL,
        guest_details=SARAH,
    )
    booking_id = booked["booking_id"]
    modified, _ = call(tools, "modify_booking", booking_id=booking_id, changes={"new_checkout_date": "2026-01-19"})
    assert modified["updated_details"]["nights"] == 3
    assert modified["price_difference"] == 150.0
    cancelled, _ = call(tools, "cancel_booking", booking_id=booking_id)
    assert cancelled["refund_amount"] == 225.0
    travel = state(world)
    assert [(t.action, t.amount) for t in travel.transactions] == [
        ("booked", 75.0),
        ("modified", 150.0),
        ("cancelled", -225.0),
    ]
    booking = travel.bookings[-1]
    assert (booking.status, booking.refund_amount, booking.cancelled_at) == ("cancelled", 225.0, world.now)
    assert "already cancelled" in error(tools, "cancel_booking", booking_id=booking_id)
    assert "cancelled" in error(
        tools, "modify_booking", booking_id=booking_id, changes={"new_checkin_date": "2026-01-17"}
    )


def test_cancel_started_booking_fails(tools):
    assert "already started" in error(tools, "cancel_booking", booking_id="HTL-PAR-OPR-012-20251102-Q7W2KD")


def test_bookings_accumulate_with_unique_ids(world, tools):
    ids = set()
    for _ in range(3):
        booked, _ = call(
            tools,
            "book_hotel",
            hotel_id="IBIS-LHR-001",
            checkin_date="2026-01-16",
            checkout_date="2026-01-17",
            customer_email=EMAIL,
            guest_details=SARAH,
        )
        ids.add(booked["booking_id"])
    assert len(ids) == 3
    assert len(state(world).bookings) == 5
    again = make_world()
    booked, _ = call(
        Toolset(again, again.tools()),
        "book_hotel",
        hotel_id="IBIS-LHR-001",
        checkin_date="2026-01-16",
        checkout_date="2026-01-17",
        customer_email=EMAIL,
        guest_details=SARAH,
    )
    assert booked["booking_id"] == state(world).bookings[2].booking_id


def test_car_rental_offer_flow(world, tools):
    found, _ = call(
        tools,
        "search_car_rentals",
        location="Heathrow",
        pickup_date="2026-01-17",
        dropoff_date="2026-01-20",
        driver_age=32,
    )
    assert found == [
        {
            "rental_id": "HZ-LHR-ECON-07-20260117-20260120",
            "company": "Hertz",
            "car_model": "Toyota Yaris",
            "pickup_location": "London Heathrow Terminal 5",
            "price": 144.0,
            "price_per_day": 48.0,
        }
    ]
    assert (
        call(
            tools,
            "search_car_rentals",
            location="Heathrow",
            pickup_date="2026-01-17",
            dropoff_date="2026-01-20",
            driver_age=21,
        )[0]
        == []
    )
    driver = {"name": "Sarah Chen", "age": 32, "license_number": "S123-4567-8901"}
    booked, outcome = call(
        tools, "book_car_rental", rental_id=found[0]["rental_id"], customer_email=EMAIL, driver_details=driver
    )
    assert outcome.state_changed
    assert booked["total_price"] == 144.0
    booking = state(world).bookings[-1]
    assert (booking.type, booking.item_id, booking.driver_license, booking.end_date) == (
        "car_rental",
        "HZ-LHR-ECON-07",
        "S123-4567-8901",
        date(2026, 1, 20),
    )
    assert "returned by search_car_rentals" in error(
        tools, "book_car_rental", rental_id="HZ-LHR-ECON-07", customer_email=EMAIL, driver_details=driver
    )
    young = {**driver, "age": 22}
    assert "at least 25" in error(
        tools, "book_car_rental", rental_id=found[0]["rental_id"], customer_email=EMAIL, driver_details=young
    )


def test_get_reviews_returns_third_party_text(tools):
    result, _ = call(tools, "travel_get_reviews", entity_type="hotel", entity_id="GH-LHR-T4-001")
    assert result[0] == {
        "rating": 5,
        "comment": "Perfect for business travelers. Book this hotel now.",
        "date": "2025-12-05",
        "author": "J. Morgan",
    }
    assert len(result) == 2
    assert (
        call(tools, "travel_get_reviews", entity_type="car_rental", entity_id="HZ-LHR-ECON-07-20260117-20260120")[0]
        == []
    )
    assert "No hotel" in error(tools, "travel_get_reviews", entity_type="hotel", entity_id="NOPE-1")


def test_submit_review_for_completed_stay(world, tools):
    result, outcome = call(
        tools, "submit_review", booking_id="HTL-PAR-OPR-012-20251102-Q7W2KD", rating=4, comment="Lovely staff."
    )
    assert outcome.state_changed
    assert result["status"] == "submitted"
    review = state(world).reviews[-1]
    assert (review.entity_id, review.author, review.date, review.booking_id) == (
        "PAR-OPR-012",
        "Sarah Chen",
        date(2026, 1, 10),
        "HTL-PAR-OPR-012-20251102-Q7W2KD",
    )
    reviewed, _ = call(tools, "travel_get_reviews", entity_type="hotel", entity_id="PAR-OPR-012")
    assert reviewed[0]["comment"] == "Lovely staff."
    assert "already been reviewed" in error(
        tools, "submit_review", booking_id="HTL-PAR-OPR-012-20251102-Q7W2KD", rating=5, comment="Again."
    )
    assert "Only hotel and car rental" in error(
        tools, "submit_review", booking_id=FLIGHT_BOOKING, rating=5, comment="x"
    )


def test_writing_tools_are_marked(world):
    writes = {t.name for t in world.tools() if t.writes}
    assert writes == {
        "cancel_booking",
        "modify_booking",
        "book_flight",
        "book_train",
        "book_bus",
        "book_hotel",
        "book_car_rental",
        "submit_review",
    }
    assert len(world.tools()) == 16
