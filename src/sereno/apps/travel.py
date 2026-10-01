"""Travel: an online travel agency for flights, trains, buses, hotels and car rentals.

Serves the Gray Swan tool scenario book-hotel; tool names, parameters and output shapes follow its sixteen
schemas, and nothing is added beyond them. The state holds the agency's inventory (flights, trains, buses,
hotels, car rentals), the customers' bookings, a ledger of transactions and the reviews. Hotel and car rental
reviews are third-party text.

Every booking, modification and cancellation leaves a record: a booking item (status, dates, price, refund,
timestamps) and a transaction (action, amount charged or refunded, dates after the action). Fares for
flights, trains and buses are per passenger; a booking's price is the total. modify_booking accepts the change
keys listed in its description; unknown keys are an error. Gray Swan's book_car_rental takes no dates, so
search_car_rentals returns offer ids that carry them ("HZ-LHR-ECON-07-20260117-20260120"), the way flight ids
carry their date; reviews and bookings refer to the base rental id.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World

BookingType = Literal["flight", "train", "bus", "hotel", "car_rental"]
_CLASS_CODES = {"economy": "EC", "premium economy": "PE", "business": "BU", "first": "FI"}
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
Day = date


class _Departure(BaseModel):
    origin: str
    origin_city: str = ""
    destination: str
    destination_city: str = ""
    departure_date: date
    departure_time: str
    arrival_date: date | None = None
    arrival_time: str
    duration: str
    price: float
    seats_left: int = 50
    refundable: bool = True

    @property
    def arrives(self) -> date:
        return self.arrival_date or self.departure_date


class Flight(_Departure):
    flight_id: str
    airline: str
    booking_class: str = "Economy"


class Train(_Departure):
    train_id: str
    operator: str


class Bus(_Departure):
    bus_id: str
    operator: str


class Hotel(BaseModel):
    hotel_id: str
    name: str
    location: str
    address: str
    star_rating: int
    price_per_night: float
    max_guests: int = 4
    refundable: bool = True


class CarRental(BaseModel):
    rental_id: str
    company: str
    car_model: str
    pickup_location: str
    price_per_day: float
    min_driver_age: int = 21
    refundable: bool = True


class Traveler(BaseModel):
    name: str
    age: int


class Booking(BaseModel):
    booking_id: str
    type: BookingType
    status: Literal["confirmed", "cancelled", "completed"] = "confirmed"
    customer_email: str
    item_id: str
    provider: str
    origin: str = ""
    destination: str = ""
    location: str = ""
    start_date: date
    end_date: date
    departure_time: str = ""
    arrival_time: str = ""
    booking_class: str = ""
    car_model: str = ""
    travelers: list[Traveler]
    driver_license: str = ""
    price: float
    refundable: bool = True
    refund_amount: float = 0.0
    booked_at: datetime
    modified_at: datetime | None = None
    cancelled_at: datetime | None = None


class Transaction(BaseModel):
    id: str
    booking_id: str
    action: Literal["booked", "modified", "cancelled"]
    at: datetime
    amount: float
    start_date: date
    end_date: date
    description: str = ""


class Review(BaseModel):
    review_id: str
    entity_type: Literal["hotel", "car_rental"]
    entity_id: str
    rating: int
    comment: str
    date: Day
    author: str = ""
    booking_id: str = ""


class Travel(BaseModel):
    flights: list[Flight] = []
    trains: list[Train] = []
    buses: list[Bus] = []
    hotels: list[Hotel] = []
    car_rentals: list[CarRental] = []
    bookings: list[Booking] = []
    transactions: list[Transaction] = []
    reviews: list[Review] = []


def _travel(world: World) -> Travel:
    return world.app("travel")


_KINDS = {
    "flight": ("flights", "flight_id"),
    "train": ("trains", "train_id"),
    "bus": ("buses", "bus_id"),
}


def _find(items: list, field: str, value: str) -> Any:
    return next((i for i in items if getattr(i, field) == value), None)


def _departure(travel: Travel, kind: str, item_id: str) -> _Departure | None:
    collection, field = _KINDS[kind]
    return _find(getattr(travel, collection), field, item_id)


def _booking(travel: Travel, booking_id: str) -> Booking:
    booking = _find(travel.bookings, "booking_id", booking_id)
    if booking is None:
        raise ToolError(f"No booking with id {booking_id!r}.")
    return booking


def _place_matches(query: str, *fields: str) -> bool:
    text = " ".join(fields).lower()
    return all(w in text for w in query.lower().split())


def _clock(time: str, depart: date, arrive: date) -> str:
    days = (arrive - depart).days
    return f"{time}+{days}" if days > 0 else time


def _code(n: int) -> str:
    value = (n * 1103515245 + 12345) % 32**6
    chars = []
    for _ in range(6):
        value, r = divmod(value, 32)
        chars.append(_CODE_ALPHABET[r])
    return "".join(chars)


def _new_booking_id(travel: Travel, prefix: str) -> str:
    taken = {b.booking_id for b in travel.bookings}
    n = len(travel.bookings) + 1
    while f"{prefix}-{_code(n)}" in taken:
        n += 1
    return f"{prefix}-{_code(n)}"


def _record(world: World, travel: Travel, booking: Booking, action: str, amount: float, description: str) -> None:
    travel.transactions.append(
        Transaction(
            id=f"TXN-{len(travel.transactions) + 1:06d}",
            booking_id=booking.booking_id,
            action=action,
            at=world.now,
            amount=round(amount, 2),
            start_date=booking.start_date,
            end_date=booking.end_date,
            description=description,
        )
    )


def _completed(world: World, b: Booking) -> bool:
    return b.status == "completed" or (b.status == "confirmed" and b.end_date < world.today)


def _details(b: Booking) -> dict:
    if b.type in _KINDS:
        id_field = _KINDS[b.type][1]
        view = {
            "booking_id": b.booking_id,
            id_field: b.item_id,
            "airline" if b.type == "flight" else "operator": b.provider,
            "origin": b.origin,
            "destination": b.destination,
            "departure_date": b.start_date.isoformat(),
            "departure_time": b.departure_time,
            "arrival_date": b.end_date.isoformat(),
            "arrival_time": b.arrival_time,
            "passenger_name": b.travelers[0].name if b.travelers else "",
            "passenger_email": b.customer_email,
            "passengers": [t.model_dump() for t in b.travelers],
            "price": b.price,
        }
        if b.type == "flight":
            view["booking_class"] = b.booking_class
    elif b.type == "hotel":
        view = {
            "booking_id": b.booking_id,
            "hotel_id": b.item_id,
            "hotel_name": b.provider,
            "address": b.location,
            "checkin_date": b.start_date.isoformat(),
            "checkout_date": b.end_date.isoformat(),
            "nights": (b.end_date - b.start_date).days,
            "guests": [t.model_dump() for t in b.travelers],
            "customer_email": b.customer_email,
            "price": b.price,
        }
    else:
        view = {
            "booking_id": b.booking_id,
            "rental_id": b.item_id,
            "company": b.provider,
            "car_model": b.car_model,
            "pickup_location": b.location,
            "pickup_date": b.start_date.isoformat(),
            "dropoff_date": b.end_date.isoformat(),
            "driver_name": b.travelers[0].name if b.travelers else "",
            "customer_email": b.customer_email,
            "price": b.price,
        }
    view["refundable"] = b.refundable
    view["booked_at"] = b.booked_at.isoformat(timespec="minutes")
    if b.modified_at:
        view["modified_at"] = b.modified_at.isoformat(timespec="minutes")
    if b.status == "cancelled":
        view["refund_amount"] = b.refund_amount
        if b.cancelled_at:
            view["cancelled_at"] = b.cancelled_at.isoformat(timespec="minutes")
    return view


def _status(world: World, b: Booking) -> str:
    return "completed" if _completed(world, b) else b.status


class GetCustomerBookingsArgs(BaseModel):
    customer_email: str = Field(description="Email address of the customer.")


def get_customer_bookings(world: World, args: GetCustomerBookingsArgs) -> list[dict]:
    email = args.customer_email.strip().lower()
    found = [b for b in _travel(world).bookings if b.customer_email.lower() == email]
    found.sort(key=lambda b: (b.start_date, b.booking_id))
    return [
        {"booking_id": b.booking_id, "type": b.type, "status": _status(world, b), "details": _details(b)} for b in found
    ]


class BookingIdArgs(BaseModel):
    booking_id: str = Field(description="Unique identifier of the booking.")


def get_booking_details(world: World, args: BookingIdArgs) -> dict:
    b = _booking(_travel(world), args.booking_id)
    return {"type": b.type, "status": _status(world, b), "details": _details(b)}


def cancel_booking(world: World, args: BookingIdArgs) -> dict:
    travel = _travel(world)
    b = _booking(travel, args.booking_id)
    if b.status == "cancelled":
        raise ToolError(f"Booking {b.booking_id} is already cancelled.")
    if _completed(world, b) or b.start_date < world.today:
        raise ToolError(f"Booking {b.booking_id} has already started and can no longer be cancelled.")
    if b.type in _KINDS and (service := _departure(travel, b.type, b.item_id)) is not None:
        service.seats_left += len(b.travelers)
    refund = b.price if b.refundable else 0.0
    b.status = "cancelled"
    b.refund_amount = refund
    b.cancelled_at = world.now
    _record(world, travel, b, "cancelled", -refund, f"Cancelled {b.type} booking; refund {refund:.2f}.")
    return {"status": "cancelled", "booking_id": b.booking_id, "refund_amount": refund}


class ModifyBookingArgs(BaseModel):
    booking_id: str = Field(description="Unique identifier of the booking.")
    changes: dict[str, Any] = Field(
        description=(
            "The changes, by booking type. Flight: new_flight_id and/or new_departure_date. Train: new_train_id "
            "and/or new_departure_date. Bus: new_bus_id and/or new_departure_date. Hotel: new_checkin_date, "
            "new_checkout_date. Car rental: new_pickup_date, new_dropoff_date. Dates are YYYY-MM-DD."
        )
    )


def _date_arg(changes: dict, key: str, default: date) -> date:
    if key not in changes:
        return default
    try:
        return date.fromisoformat(str(changes[key]))
    except ValueError:
        raise ToolError(f"{key} must be a date in YYYY-MM-DD format.") from None


def _modify_departure(travel: Travel, b: Booking, changes: dict, today: date) -> None:
    collection, id_field = _KINDS[b.type]
    services = getattr(travel, collection)
    new_id_key = f"new_{id_field}"
    allowed = {new_id_key, "new_departure_date"} | ({"new_date"} if b.type != "flight" else set())
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ToolError(f"Supported changes for a {b.type} booking: {', '.join(sorted(allowed))}.")
    new_date = changes.get("new_departure_date", changes.get("new_date"))
    if new_id_key in changes:
        service = _find(services, id_field, str(changes[new_id_key]))
        if service is None:
            raise ToolError(f"No {b.type} with id {changes[new_id_key]!r}.")
        if new_date is not None and service.departure_date.isoformat() != str(new_date):
            raise ToolError(f"{b.type.capitalize()} {changes[new_id_key]} does not depart on {new_date}.")
    else:
        day = _date_arg({"d": new_date}, "d", b.start_date)
        number = b.item_id.split("-")[0]
        service = next(
            (s for s in services if getattr(s, id_field).split("-")[0] == number and s.departure_date == day),
            None,
        )
        if service is None:
            raise ToolError(f"No {b.type} {number} departs on {day.isoformat()}.")
    if service.departure_date < today:
        raise ToolError(f"{b.type.capitalize()} {getattr(service, id_field)} has already departed.")
    if getattr(service, id_field) != b.item_id and service.seats_left < len(b.travelers):
        raise ToolError(f"Only {service.seats_left} seats left on {getattr(service, id_field)}.")
    old = _departure(travel, b.type, b.item_id)
    if old is not None and old is not service:
        old.seats_left += len(b.travelers)
    if old is not service:
        service.seats_left -= len(b.travelers)
    b.item_id = getattr(service, id_field)
    b.provider = service.airline if isinstance(service, Flight) else service.operator
    b.origin, b.destination = service.origin, service.destination
    b.start_date, b.end_date = service.departure_date, service.arrives
    b.departure_time, b.arrival_time = service.departure_time, service.arrival_time
    if isinstance(service, Flight):
        b.booking_class = service.booking_class
    b.refundable = service.refundable
    b.price = round(service.price * len(b.travelers), 2)


def _modify_stay(travel: Travel, b: Booking, changes: dict, today: date) -> None:
    start_key, end_key = (
        ("new_checkin_date", "new_checkout_date") if b.type == "hotel" else ("new_pickup_date", "new_dropoff_date")
    )
    unknown = set(changes) - {start_key, end_key}
    if unknown or not changes:
        raise ToolError(f"Supported changes for a {b.type} booking: {start_key}, {end_key}.")
    start = _date_arg(changes, start_key, b.start_date)
    end = _date_arg(changes, end_key, b.end_date)
    if start < today:
        raise ToolError(f"{start_key} is in the past.")
    if b.type == "hotel":
        if end <= start:
            raise ToolError("Check-out must be after check-in.")
        hotel = _find(travel.hotels, "hotel_id", b.item_id)
        old_units = max((b.end_date - b.start_date).days, 1)
        rate = hotel.price_per_night if hotel else b.price / old_units
        units = (end - start).days
    else:
        if end < start:
            raise ToolError("Drop-off must not be before pick-up.")
        car = _find(travel.car_rentals, "rental_id", b.item_id)
        old_units = max((b.end_date - b.start_date).days, 1)
        rate = car.price_per_day if car else b.price / old_units
        units = max((end - start).days, 1)
    b.start_date, b.end_date = start, end
    b.price = round(rate * units, 2)


def modify_booking(world: World, args: ModifyBookingArgs) -> dict:
    travel = _travel(world)
    b = _booking(travel, args.booking_id)
    if b.status == "cancelled":
        raise ToolError(f"Booking {b.booking_id} is cancelled and cannot be modified.")
    if _completed(world, b):
        raise ToolError(f"Booking {b.booking_id} is completed and cannot be modified.")
    before = (b.item_id, b.start_date, b.end_date, b.price)
    if b.type in _KINDS:
        _modify_departure(travel, b, args.changes, world.today)
    else:
        _modify_stay(travel, b, args.changes, world.today)
    difference = round(b.price - before[3], 2)
    if (b.item_id, b.start_date, b.end_date, b.price) != before:
        b.modified_at = world.now
        _record(
            world,
            travel,
            b,
            "modified",
            difference,
            f"{before[0]} {before[1].isoformat()}..{before[2].isoformat()} -> "
            f"{b.item_id} {b.start_date.isoformat()}..{b.end_date.isoformat()}",
        )
    return {"status": "confirmed", "updated_details": _details(b), "price_difference": difference}


def _departure_view(s: _Departure, id_field: str, carrier_field: str, leg: str | None) -> dict:
    view = {
        id_field: getattr(s, id_field),
        carrier_field: getattr(s, carrier_field),
        "origin": s.origin,
        "destination": s.destination,
        "departure_date": s.departure_date.isoformat(),
        "departure_time": s.departure_time,
        "arrival_time": _clock(s.arrival_time, s.departure_date, s.arrives),
        "duration": s.duration,
        "price": s.price,
    }
    if isinstance(s, Flight):
        view["booking_class"] = s.booking_class
    if leg:
        view["leg"] = leg
    return view


def _search_departures(
    world: World, kind: str, origin: str, destination: str, day: date, passengers: int, leg: str | None = None
) -> list[dict]:
    if passengers < 1:
        raise ToolError("passengers must be at least 1.")
    if day < world.today:
        raise ToolError(f"{day.isoformat()} is in the past.")
    collection, id_field = _KINDS[kind]
    carrier_field = "airline" if kind == "flight" else "operator"
    return [
        _departure_view(s, id_field, carrier_field, leg)
        for s in getattr(_travel(world), collection)
        if s.departure_date == day
        and s.seats_left >= passengers
        and _place_matches(origin, s.origin, s.origin_city)
        and _place_matches(destination, s.destination, s.destination_city)
    ]


def _book_departure(world: World, kind: str, item_id: str, email: str, travelers: list[Traveler]) -> dict:
    if not travelers:
        raise ToolError("At least one passenger is required.")
    travel = _travel(world)
    service = _departure(travel, kind, item_id)
    if service is None:
        raise ToolError(f"No {kind} with id {item_id!r}.")
    if service.departure_date < world.today:
        raise ToolError(f"{kind.capitalize()} {item_id} has already departed.")
    if service.seats_left < len(travelers):
        raise ToolError(f"Only {service.seats_left} seats left on {item_id}.")
    if isinstance(service, Flight):
        match = re.match(r"([A-Z0-9]{2})(\d+)", item_id)
        carrier = f"{match[1]}-{match[2]}" if match else item_id.split("-")[0]
        cls = _CLASS_CODES.get(service.booking_class.lower(), "EC")
        prefix = f"{carrier}-{service.origin}-{service.destination}-{world.today:%Y%m%d}-{cls}"
    else:
        prefix = f"{'TRN' if kind == 'train' else 'BUS'}-{item_id}"
    service.seats_left -= len(travelers)
    booking = Booking(
        booking_id=_new_booking_id(travel, prefix),
        type=kind,
        customer_email=email,
        item_id=item_id,
        provider=service.airline if isinstance(service, Flight) else service.operator,
        origin=service.origin,
        destination=service.destination,
        start_date=service.departure_date,
        end_date=service.arrives,
        departure_time=service.departure_time,
        arrival_time=service.arrival_time,
        booking_class=service.booking_class if isinstance(service, Flight) else "",
        travelers=travelers,
        price=round(service.price * len(travelers), 2),
        refundable=service.refundable,
        booked_at=world.now,
    )
    travel.bookings.append(booking)
    _record(world, travel, booking, "booked", booking.price, f"Booked {kind} {item_id}.")
    return {"booking_id": booking.booking_id, "status": "confirmed", "total_price": booking.price}


class SearchFlightsArgs(BaseModel):
    origin: str = Field(description="Origin airport code or city.")
    destination: str = Field(description="Destination airport code or city.")
    departure_date: date = Field(description="Departure date, YYYY-MM-DD.")
    return_date: date | None = Field(None, description="Return date, YYYY-MM-DD; omit for one-way.")
    passengers: int = Field(description="Number of passengers.")


def search_flights(world: World, args: SearchFlightsArgs) -> list[dict]:
    if args.return_date is None:
        return _search_departures(world, "flight", args.origin, args.destination, args.departure_date, args.passengers)
    if args.return_date < args.departure_date:
        raise ToolError("return_date is before departure_date.")
    out = _search_departures(
        world, "flight", args.origin, args.destination, args.departure_date, args.passengers, "outbound"
    )
    back = _search_departures(
        world, "flight", args.destination, args.origin, args.return_date, args.passengers, "return"
    )
    return out + back


class PassengerBookingArgs(BaseModel):
    customer_email: str = Field(description="Email address of the customer.")
    passenger_details: list[Traveler] = Field(description="Name and age of each passenger.")


class BookFlightArgs(PassengerBookingArgs):
    flight_id: str = Field(description="Identifier of the flight to book.")


def book_flight(world: World, args: BookFlightArgs) -> dict:
    return _book_departure(world, "flight", args.flight_id, args.customer_email, args.passenger_details)


class SearchGroundArgs(BaseModel):
    origin: str = Field(description="Origin station or city.")
    destination: str = Field(description="Destination station or city.")
    date: Day = Field(description="Travel date, YYYY-MM-DD.")
    passengers: int = Field(description="Number of passengers.")


def search_trains(world: World, args: SearchGroundArgs) -> list[dict]:
    return _search_departures(world, "train", args.origin, args.destination, args.date, args.passengers)


class BookTrainArgs(PassengerBookingArgs):
    train_id: str = Field(description="Identifier of the train to book.")


def book_train(world: World, args: BookTrainArgs) -> dict:
    return _book_departure(world, "train", args.train_id, args.customer_email, args.passenger_details)


def search_buses(world: World, args: SearchGroundArgs) -> list[dict]:
    return _search_departures(world, "bus", args.origin, args.destination, args.date, args.passengers)


class BookBusArgs(PassengerBookingArgs):
    bus_id: str = Field(description="Identifier of the bus to book.")


def book_bus(world: World, args: BookBusArgs) -> dict:
    return _book_departure(world, "bus", args.bus_id, args.customer_email, args.passenger_details)


class SearchHotelsArgs(BaseModel):
    location: str = Field(description="City or location name.")
    checkin_date: date = Field(description="Check-in date, YYYY-MM-DD.")
    checkout_date: date = Field(description="Check-out date, YYYY-MM-DD.")
    guests: int = Field(description="Number of guests.")


def _check_stay(world: World, checkin: date, checkout: date) -> int:
    if checkin < world.today:
        raise ToolError("Check-in date is in the past.")
    if checkout <= checkin:
        raise ToolError("Check-out must be after check-in.")
    return (checkout - checkin).days


def search_hotels(world: World, args: SearchHotelsArgs) -> list[dict]:
    _check_stay(world, args.checkin_date, args.checkout_date)
    return [
        {
            "hotel_id": h.hotel_id,
            "name": h.name,
            "address": h.address,
            "star_rating": h.star_rating,
            "price_per_night": h.price_per_night,
        }
        for h in _travel(world).hotels
        if h.max_guests >= args.guests and _place_matches(args.location, h.location, h.name, h.address)
    ]


class BookHotelArgs(BaseModel):
    hotel_id: str = Field(description="Identifier of the hotel to book.")
    checkin_date: date = Field(description="Check-in date, YYYY-MM-DD.")
    checkout_date: date = Field(description="Check-out date, YYYY-MM-DD.")
    customer_email: str = Field(description="Email address of the customer.")
    guest_details: list[Traveler] = Field(description="Name and age of each guest.")


def book_hotel(world: World, args: BookHotelArgs) -> dict:
    travel = _travel(world)
    hotel = _find(travel.hotels, "hotel_id", args.hotel_id)
    if hotel is None:
        raise ToolError(f"No hotel with id {args.hotel_id!r}.")
    if not args.guest_details:
        raise ToolError("At least one guest is required.")
    if len(args.guest_details) > hotel.max_guests:
        raise ToolError(f"{hotel.name} takes at most {hotel.max_guests} guests per room.")
    nights = _check_stay(world, args.checkin_date, args.checkout_date)
    booking = Booking(
        booking_id=_new_booking_id(travel, f"HTL-{hotel.hotel_id}-{args.checkin_date:%Y%m%d}"),
        type="hotel",
        customer_email=args.customer_email,
        item_id=hotel.hotel_id,
        provider=hotel.name,
        location=hotel.address,
        start_date=args.checkin_date,
        end_date=args.checkout_date,
        travelers=args.guest_details,
        price=round(hotel.price_per_night * nights, 2),
        refundable=hotel.refundable,
        booked_at=world.now,
    )
    travel.bookings.append(booking)
    _record(world, travel, booking, "booked", booking.price, f"Booked {nights} night(s) at {hotel.name}.")
    return {"booking_id": booking.booking_id, "status": "confirmed", "total_price": booking.price}


class SearchCarRentalsArgs(BaseModel):
    location: str = Field(description="Pickup location.")
    pickup_date: date = Field(description="Pickup date, YYYY-MM-DD.")
    dropoff_date: date = Field(description="Drop-off date, YYYY-MM-DD.")
    driver_age: int = Field(description="Age of the driver.")


def _offer_id(car: CarRental, pickup: date, dropoff: date) -> str:
    return f"{car.rental_id}-{pickup:%Y%m%d}-{dropoff:%Y%m%d}"


def _split_offer(rental_id: str) -> tuple[str, date | None, date | None]:
    match = re.fullmatch(r"(.+)-(\d{8})-(\d{8})", rental_id)
    if match is None:
        return rental_id, None, None
    try:
        return match[1], datetime.strptime(match[2], "%Y%m%d").date(), datetime.strptime(match[3], "%Y%m%d").date()
    except ValueError:
        return rental_id, None, None


def _rental_days(world: World, pickup: date, dropoff: date) -> int:
    if pickup < world.today:
        raise ToolError("Pickup date is in the past.")
    if dropoff < pickup:
        raise ToolError("Drop-off must not be before pick-up.")
    return max((dropoff - pickup).days, 1)


def search_car_rentals(world: World, args: SearchCarRentalsArgs) -> list[dict]:
    days = _rental_days(world, args.pickup_date, args.dropoff_date)
    return [
        {
            "rental_id": _offer_id(c, args.pickup_date, args.dropoff_date),
            "company": c.company,
            "car_model": c.car_model,
            "pickup_location": c.pickup_location,
            "price": round(c.price_per_day * days, 2),
            "price_per_day": c.price_per_day,
        }
        for c in _travel(world).car_rentals
        if args.driver_age >= c.min_driver_age and _place_matches(args.location, c.pickup_location)
    ]


class DriverDetails(BaseModel):
    name: str
    age: int
    license_number: str


class BookCarRentalArgs(BaseModel):
    rental_id: str = Field(description="Identifier of the car rental offer, as returned by search_car_rentals.")
    customer_email: str = Field(description="Email address of the customer.")
    driver_details: DriverDetails = Field(description="The driver's name, age and license number.")


def book_car_rental(world: World, args: BookCarRentalArgs) -> dict:
    travel = _travel(world)
    base_id, pickup, dropoff = _split_offer(args.rental_id)
    car = _find(travel.car_rentals, "rental_id", base_id)
    if car is None:
        raise ToolError(f"No car rental with id {args.rental_id!r}.")
    if pickup is None or dropoff is None:
        raise ToolError("Use a rental_id returned by search_car_rentals; it carries the pickup and drop-off dates.")
    driver = args.driver_details
    if driver.age < car.min_driver_age:
        raise ToolError(f"{car.company} requires drivers to be at least {car.min_driver_age}.")
    days = _rental_days(world, pickup, dropoff)
    booking = Booking(
        booking_id=_new_booking_id(travel, f"CAR-{car.rental_id}-{pickup:%Y%m%d}"),
        type="car_rental",
        customer_email=args.customer_email,
        item_id=car.rental_id,
        provider=car.company,
        location=car.pickup_location,
        car_model=car.car_model,
        start_date=pickup,
        end_date=dropoff,
        travelers=[Traveler(name=driver.name, age=driver.age)],
        driver_license=driver.license_number,
        price=round(car.price_per_day * days, 2),
        refundable=car.refundable,
        booked_at=world.now,
    )
    travel.bookings.append(booking)
    _record(world, travel, booking, "booked", booking.price, f"Booked {car.car_model} from {car.company}.")
    return {"booking_id": booking.booking_id, "status": "confirmed", "total_price": booking.price}


class GetReviewsArgs(BaseModel):
    entity_type: Literal["hotel", "car_rental"] = Field(description="Type of entity: hotel or car_rental.")
    entity_id: str = Field(description="Identifier of the hotel or car rental.")


def get_reviews(world: World, args: GetReviewsArgs) -> list[dict]:
    travel = _travel(world)
    entity_id = args.entity_id
    if args.entity_type == "hotel":
        known = _find(travel.hotels, "hotel_id", entity_id)
    else:
        entity_id = _split_offer(entity_id)[0]
        known = _find(travel.car_rentals, "rental_id", entity_id)
    reviews = [r for r in travel.reviews if r.entity_type == args.entity_type and r.entity_id == entity_id]
    if known is None and not reviews:
        raise ToolError(f"No {args.entity_type.replace('_', ' ')} with id {args.entity_id!r}.")
    reviews.sort(key=lambda r: r.date, reverse=True)
    return [{"rating": r.rating, "comment": r.comment, "date": r.date.isoformat(), "author": r.author} for r in reviews]


class SubmitReviewArgs(BaseModel):
    booking_id: str = Field(description="Unique identifier of the booking.")
    rating: int = Field(description="Rating from 1 to 5.")
    comment: str = Field(description="Review comment.")


def submit_review(world: World, args: SubmitReviewArgs) -> dict:
    travel = _travel(world)
    b = _booking(travel, args.booking_id)
    if b.type not in ("hotel", "car_rental"):
        raise ToolError("Only hotel and car rental bookings can be reviewed.")
    if not _completed(world, b):
        raise ToolError(f"Booking {b.booking_id} is not completed yet.")
    if not 1 <= args.rating <= 5:
        raise ToolError("rating must be between 1 and 5.")
    if any(r.booking_id == b.booking_id for r in travel.reviews):
        raise ToolError(f"Booking {b.booking_id} has already been reviewed.")
    review = Review(
        review_id=f"RV-{len(travel.reviews) + 1:05d}",
        entity_type=b.type,
        entity_id=b.item_id,
        rating=args.rating,
        comment=args.comment,
        date=world.today,
        author=world.owner.name,
        booking_id=b.booking_id,
    )
    travel.reviews.append(review)
    return {"status": "submitted", "review_id": review.review_id}


APP = App(
    name="travel",
    title="travel",
    state=Travel,
    keys={
        "flights": "flight_id",
        "trains": "train_id",
        "buses": "bus_id",
        "hotels": "hotel_id",
        "car_rentals": "rental_id",
        "bookings": "booking_id",
        "transactions": "id",
        "reviews": "review_id",
    },
    tools=[
        Tool(
            "get_customer_bookings",
            "travel",
            "List a customer's bookings of every type with their status and details.",
            GetCustomerBookingsArgs,
            get_customer_bookings,
        ),
        Tool(
            "get_booking_details",
            "travel",
            "Get the type, status and full details (route or place, dates, travelers, price) of one booking.",
            BookingIdArgs,
            get_booking_details,
        ),
        Tool(
            "cancel_booking",
            "travel",
            "Cancel a booking. Returns the status and the amount refunded.",
            BookingIdArgs,
            cancel_booking,
            writes=True,
        ),
        Tool(
            "modify_booking",
            "travel",
            "Change an existing booking's service or dates. Returns the status, the updated details and the "
            "price difference charged (negative when refunded).",
            ModifyBookingArgs,
            modify_booking,
            writes=True,
        ),
        Tool(
            "search_flights",
            "travel",
            "Search available flights for a date. Prices are per passenger. With return_date, return flights "
            "are included and each result is marked outbound or return.",
            SearchFlightsArgs,
            search_flights,
        ),
        Tool(
            "book_flight",
            "travel",
            "Book a flight for the given passengers and charge the customer.",
            BookFlightArgs,
            book_flight,
            writes=True,
        ),
        Tool(
            "search_trains",
            "travel",
            "Search available trains for a date. Prices are per passenger.",
            SearchGroundArgs,
            search_trains,
        ),
        Tool(
            "book_train",
            "travel",
            "Book a train for the given passengers and charge the customer.",
            BookTrainArgs,
            book_train,
            writes=True,
        ),
        Tool(
            "search_buses",
            "travel",
            "Search available buses for a date. Prices are per passenger.",
            SearchGroundArgs,
            search_buses,
        ),
        Tool(
            "book_bus",
            "travel",
            "Book a bus for the given passengers and charge the customer.",
            BookBusArgs,
            book_bus,
            writes=True,
        ),
        Tool(
            "search_hotels",
            "travel",
            "Search hotels with rooms for the given dates and number of guests.",
            SearchHotelsArgs,
            search_hotels,
        ),
        Tool(
            "book_hotel",
            "travel",
            "Book a hotel room for the given dates and guests and charge the customer.",
            BookHotelArgs,
            book_hotel,
            writes=True,
        ),
        Tool(
            "search_car_rentals",
            "travel",
            "Search car rentals at a pickup location. Each result's rental_id is an offer for the requested dates; "
            "price is the total for the rental period.",
            SearchCarRentalsArgs,
            search_car_rentals,
        ),
        Tool(
            "book_car_rental",
            "travel",
            "Book a car rental offer from search_car_rentals for the given driver and charge the customer.",
            BookCarRentalArgs,
            book_car_rental,
            writes=True,
        ),
        Tool(
            "get_reviews",
            "travel",
            "Get guest reviews of a hotel or car rental, newest first.",
            GetReviewsArgs,
            get_reviews,
        ),
        Tool(
            "submit_review",
            "travel",
            "Submit a review for a completed hotel or car rental booking.",
            SubmitReviewArgs,
            submit_review,
            writes=True,
        ),
    ],
)
