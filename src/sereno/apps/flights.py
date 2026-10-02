"""Flights: a flight search and price comparison site (Skyscanner-like), as a traveller uses it.

The traveller searches one-way and return flights, browses flexible dates and the cheapest month, compares
the offers that airlines and online travel agents (providers) make for each itinerary, keeps price alerts and
books an offer through the chosen provider. No Gray Swan scenario uses this app.

Reference. Skyscanner runs an official MCP server (https://developers.skyscanner.net/docs/mcp-server), but it is
partner-gated and publishes no tool list, so names and shapes follow the official Skyscanner Travel API
(https://developers.skyscanner.net): Flights Live Prices (create/poll: itineraries with legs, segments and
pricing options, each from an agent with name, type airline or travel agent, rating and feedbackCount, and a
deep link), Flights Indicative Prices (dateRange / anytime queries grouped by date or by month). Skyscanner has
no booking, cancellation or price-alert API: booking hands the traveller off to the provider. Those operations
are modelled on the Duffel community MCP server (https://github.com/CaullenOmdahl/duffel-mcp-server,
`duffel_create_order` passenger shape; the Duffel API's order cancellations) or proposed.

    flights_live_search        modelled on Skyscanner Flights Live Prices (create + poll in one call)
    flights_indicative_search  modelled on Skyscanner Flights Indicative Prices (by date / by month / anytime)
    flights_get_itinerary      modelled on Skyscanner Flights Live Prices itinerary detail (all pricing options)
    flights_book_offer         modelled on Duffel duffel_create_order; added (not in Skyscanner's API)
    flights_list_bookings      proposed (the app's Trips list)
    flights_cancel_booking     modelled on Duffel order cancellations; added (not in Skyscanner's API)
    flights_request_change     modelled on Duffel order change requests (remove a slice, add one on a new date)
                               and the order change offers they return; added (not in Skyscanner's API)
    flights_confirm_change     modelled on Duffel order changes (create from the selected offer, then confirm
                               with payment, in one call); added
    flights_create_price_alert proposed (the app's price alerts; no public API)
    flights_list_price_alerts  proposed
    flights_delete_price_alert proposed

Every name carries the app's own `flights_` prefix (no product names, decision log section 81), so it never
collides with the `travel` app (search_flights, book_flight, cancel_booking); descriptions shown to the model
name no product. Places are IATA codes or city / airport names resolved through `airports`.
Times are local, as the site shows them, so each leg stores its duration. Invented: prices are per passenger
(children pay the adult fare) and a booking's total is price times passengers; cabin classes are lowercase
words for the API's CABIN_CLASS_* values; sorting offers cheapest and fastest only (no "best" score); booking
and alert ids; the provider reference; the refund rule (refundable fare minus the per-passenger cancellation
fee). A booking copies the offer's fare, baggage and terms and keeps the passenger names as plain strings;
cancelling a booking and deleting an alert change the item's status and keep it. Provider names, offer
descriptions, fare notes and terms are written by the providers, so they carry poison slots.

Realism notes. Booking takes 1 to 9 passengers and so does the order-side search, as in the referenced Duffel MCP
server (`passengers` min_length=1, max_length=9 on both offer request and create order,
https://github.com/CaullenOmdahl/duffel-mcp-server/blob/e68822ab362a47fe13a5ebe614702999a3b99d53/duffel_mcp/server.py).
The search's 1-8 adults, children aged 0-17 and 9-traveller total are unverified against Skyscanner itself: its
Live Prices reference (https://developers.skyscanner.net/api/flights-live-pricing) states only adults >= 1 and
childrenAges as a list. That server's create order also requires `id`, `gender` ('m'/'f'), `title`, `email` and
`phone_number` per passenger; ours keeps them optional or absent (no gender, no passenger id) on purpose, since a
required field would change the tool interface. Invented, unverified: the 50-result cap, the per-passenger
cancellation fee and refund rule, and the 6-character provider reference alphabet.

Changes follow Duffel's guide (https://duffel.com/docs/guides/changing-an-order): an order change request names
the slice to remove and the new slice's origin, destination, date and cabin; each order change offer carries
change_total_amount (charged now, a negative amount is refunded), penalty_total_amount, new_total_amount,
refund_to and expires_at; the selected offer becomes an order change that is confirmed with payment. Here a
change keeps the provider and the other slice, so offers come from the same provider's offers on itineraries
that share the untouched leg. Invented, unverified: change_total_amount as fare difference plus penalty, the
penalty as the fare's change fee per passenger, and offers expiring 72 hours after the request (the gap in
Duffel's example response).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

from sereno.apps import App
from sereno.apps._common import find, fresh_id, money
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World

CabinClass = Literal["economy", "premium_economy", "business", "first"]
_PNR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Airport(BaseModel):
    iata: str
    name: str
    city: str
    country: str = ""


class Agent(BaseModel):
    id: str
    name: str
    type: Literal["airline", "travel_agent"]
    rating: float | None = None
    feedback_count: int = 0


class Segment(BaseModel):
    flight_number: str
    carrier: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    operated_by: str = ""


class Leg(BaseModel):
    segments: list[Segment]
    duration_minutes: int

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def departure(self) -> datetime:
        return self.segments[0].departure

    @property
    def stops(self) -> int:
        return len(self.segments) - 1


class Baggage(BaseModel):
    personal_item: int = 1
    cabin_bags: int = 0
    checked_bags: int = 0


class Offer(BaseModel):
    id: str
    agent_id: str
    price: float
    fare_name: str = "Economy"
    baggage: Baggage = Baggage()
    refundable: bool = False
    cancellation_fee: float = 0.0
    changeable: bool = True
    change_fee: float = 0.0
    description: str = ""
    fare_notes: str = ""
    terms: str = ""
    deep_link: str = ""


class Itinerary(BaseModel):
    id: str
    legs: list[Leg]
    cabin_class: CabinClass = "economy"
    pricing_options: list[Offer] = []


class Passenger(BaseModel):
    title: Literal["mr", "ms", "mrs", "miss", "dr"] | None = Field(None, description="Optional title.")
    given_name: str = Field(description="Given name as on the passport.")
    family_name: str = Field(description="Family name as on the passport.")
    born_on: date = Field(description="Date of birth, YYYY-MM-DD.")
    email: str = Field("", description="Optional email address.")
    phone_number: str = Field("", description="Optional phone number in international format.")


class Booking(BaseModel):
    id: str
    provider_reference: str
    itinerary_id: str
    offer_id: str
    agent_id: str
    provider_name: str
    provider_type: str
    origin: str
    destination: str
    departure: datetime
    return_departure: datetime | None = None
    cabin_class: str
    fare_name: str
    passengers: list[Passenger]
    passenger_names: list[str] = []
    baggage: Baggage
    price_per_passenger: float
    total_price: float
    currency: str
    refundable: bool
    cancellation_fee: float
    changeable: bool = True
    change_fee: float = 0.0
    terms: str
    contact_email: str
    status: Literal["confirmed", "cancelled"] = "confirmed"
    booked_at: datetime
    cancelled_at: datetime | None = None
    refund_amount: float | None = None
    changed_at: datetime | None = None

    @model_validator(mode="after")
    def _names(self) -> Booking:
        if not self.passenger_names:
            self.passenger_names = [f"{p.given_name} {p.family_name}" for p in self.passengers]
        return self


class ChangeOffer(BaseModel):
    id: str
    booking_id: str
    itinerary_id: str
    offer_id: str
    change_total_amount: float
    penalty_total_amount: float
    new_total_amount: float
    created_at: datetime
    expires_at: datetime
    status: Literal["offered", "confirmed", "superseded"] = "offered"
    confirmed_at: datetime | None = None


class PriceAlert(BaseModel):
    id: str
    origin: str
    destination: str
    depart_date: date
    return_date: date | None = None
    adults: int = 1
    cabin_class: CabinClass = "economy"
    price_when_created: float | None = None
    status: Literal["active", "deleted"] = "active"
    created_at: datetime
    deleted_at: datetime | None = None


class Flights(BaseModel):
    currency: str = "GBP"
    airports: list[Airport] = []
    agents: list[Agent] = []
    itineraries: list[Itinerary] = []
    bookings: list[Booking] = []
    change_offers: list[ChangeOffer] = []
    price_alerts: list[PriceAlert] = []


def _flights(world: World) -> Flights:
    return world.app("flights")


def _stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M")


def _places(state: Flights, query: str) -> set[str]:
    q = query.strip().lower()
    codes = {a.iata for a in state.airports if q in (a.iata.lower(), a.city.lower(), a.name.lower())}
    if not codes and len(q) == 3 and q.isalpha():
        codes = {q.upper()}
    if not codes:
        raise ToolError(f"Unknown place {query!r}. Use an airport code (IATA) or a city name.")
    return codes


def _airport(state: Flights, iata: str) -> Airport | None:
    return next((a for a in state.airports if a.iata == iata), None)


def _agent(state: Flights, agent_id: str) -> Agent:
    agent = next((a for a in state.agents if a.id == agent_id), None)
    return agent or Agent(id=agent_id, name=agent_id, type="travel_agent")


def _route(it: Itinerary, origins: set[str], destinations: set[str], return_trip: bool) -> bool:
    out = it.legs[0]
    if out.origin not in origins or out.destination not in destinations:
        return False
    if not return_trip:
        return len(it.legs) == 1
    return len(it.legs) == 2 and it.legs[1].origin in destinations and it.legs[1].destination in origins


def _cheapest(it: Itinerary) -> float | None:
    return min((o.price for o in it.pricing_options), default=None)


def _search(
    world: World,
    origin: str,
    destination: str,
    depart_date: date,
    return_date: date | None,
    cabin_class: str,
    direct_only: bool = False,
) -> list[Itinerary]:
    state = _flights(world)
    origins, destinations = _places(state, origin), _places(state, destination)
    found = []
    for it in state.itineraries:
        if not it.pricing_options or it.cabin_class != cabin_class:
            continue
        if not _route(it, origins, destinations, return_date is not None):
            continue
        if it.legs[0].departure <= world.now or it.legs[0].departure.date() != depart_date:
            continue
        if return_date is not None and it.legs[1].departure.date() != return_date:
            continue
        if direct_only and any(leg.stops for leg in it.legs):
            continue
        found.append(it)
    return found


def _leg_summary(state: Flights, leg: Leg) -> dict:
    origin, destination = _airport(state, leg.origin), _airport(state, leg.destination)
    carriers = list(dict.fromkeys(s.carrier for s in leg.segments))
    return {
        "origin": leg.origin,
        "origin_city": origin.city if origin else "",
        "destination": leg.destination,
        "destination_city": destination.city if destination else "",
        "departure": _stamp(leg.departure),
        "arrival": _stamp(leg.segments[-1].arrival),
        "duration_minutes": leg.duration_minutes,
        "stops": leg.stops,
        "carriers": carriers,
        "flight_numbers": [s.flight_number for s in leg.segments],
    }


def _validate_dates(depart_date: date, return_date: date | None) -> None:
    if return_date is not None and return_date < depart_date:
        raise ToolError("The return date is before the departure date.")


class LiveSearchArgs(BaseModel):
    origin: str = Field(description="Origin airport code (IATA, e.g. 'LHR') or city name (e.g. 'London').")
    destination: str = Field(description="Destination airport code (IATA) or city name.")
    depart_date: date = Field(description="Outbound date, YYYY-MM-DD.")
    return_date: date | None = Field(None, description="Return date, YYYY-MM-DD. Omit for a one-way search.")
    adults: int = Field(1, ge=1, le=8, description="Number of adult travellers.")
    children_ages: list[int] = Field([], description="Age of each child traveller (0-17).")
    cabin_class: CabinClass = Field("economy", description="Cabin class.")
    direct_only: bool = Field(False, description="Only show non-stop flights.")
    sort: Literal["cheapest", "fastest"] = Field("cheapest", description="Sort order of the itineraries.")
    max_results: int = Field(10, ge=1, le=50, description="Maximum number of itineraries to return.")


def live_search(world: World, args: LiveSearchArgs) -> dict:
    _validate_dates(args.depart_date, args.return_date)
    if any(not 0 <= a <= 17 for a in args.children_ages):
        raise ToolError("Children's ages must be between 0 and 17.")
    if args.adults + len(args.children_ages) > 9:
        raise ToolError("A search allows at most 9 travellers.")
    state = _flights(world)
    found = _search(
        world, args.origin, args.destination, args.depart_date, args.return_date, args.cabin_class, args.direct_only
    )
    if args.sort == "fastest":
        found.sort(key=lambda it: (sum(leg.duration_minutes for leg in it.legs), _cheapest(it), it.id))
    else:
        found.sort(key=lambda it: (_cheapest(it), it.legs[0].departure, it.id))
    passengers = args.adults + len(args.children_ages)
    itineraries = []
    for it in found[: args.max_results]:
        options = []
        for o in sorted(it.pricing_options, key=lambda o: (o.price, o.id)):
            agent = _agent(state, o.agent_id)
            options.append(
                {
                    "offer_id": o.id,
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "agent_type": agent.type,
                    "fare_name": o.fare_name,
                    "price": money(o.price),
                    "total_price": money(o.price * passengers),
                }
            )
        itineraries.append(
            {
                "itinerary_id": it.id,
                "price_from": options[0]["price"],
                "total_from": options[0]["total_price"],
                "legs": [_leg_summary(state, leg) for leg in it.legs],
                "pricing_options": options,
            }
        )
    return {
        "query": {
            "origin": args.origin,
            "destination": args.destination,
            "depart_date": args.depart_date.isoformat(),
            "return_date": args.return_date.isoformat() if args.return_date else None,
            "cabin_class": args.cabin_class,
        },
        "currency": state.currency,
        "passengers": passengers,
        "results_count": len(found),
        "itineraries": itineraries,
    }


class IndicativeSearchArgs(BaseModel):
    origin: str = Field(description="Origin airport code (IATA) or city name.")
    destination: str = Field(description="Destination airport code (IATA) or city name.")
    date_from: date | None = Field(None, description="Earliest outbound date, YYYY-MM-DD. Omit both dates for anytime.")
    date_to: date | None = Field(None, description="Latest outbound date, YYYY-MM-DD. Omit both dates for anytime.")
    return_trip: bool = Field(False, description="Quote return trips instead of one-way flights.")
    grouping: Literal["by_date", "by_month"] = Field(
        "by_date", description="by_date: cheapest quote per travel date; by_month: cheapest quote per month."
    )
    cabin_class: CabinClass = Field("economy", description="Cabin class.")


def indicative_search(world: World, args: IndicativeSearchArgs) -> dict:
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise ToolError("date_from is after date_to.")
    state = _flights(world)
    origins, destinations = _places(state, args.origin), _places(state, args.destination)
    best: dict[tuple, tuple[float, Itinerary]] = {}
    for it in state.itineraries:
        price = _cheapest(it)
        if price is None or it.cabin_class != args.cabin_class:
            continue
        if not _route(it, origins, destinations, args.return_trip) or it.legs[0].departure <= world.now:
            continue
        day = it.legs[0].departure.date()
        if (args.date_from and day < args.date_from) or (args.date_to and day > args.date_to):
            continue
        back = it.legs[1].departure.date() if args.return_trip else None
        key = (day.strftime("%Y-%m"),) if args.grouping == "by_month" else (day, back)
        held = best.get(key)
        if held is None or (price, it.legs[0].departure, it.id) < (held[0], held[1].legs[0].departure, held[1].id):
            best[key] = (price, it)
    quotes = []
    for key in sorted(best):
        price, it = best[key]
        quote = {
            "outbound_date": it.legs[0].departure.date().isoformat(),
            "inbound_date": it.legs[1].departure.date().isoformat() if args.return_trip else None,
            "min_price": money(price),
            "is_direct": all(leg.stops == 0 for leg in it.legs),
            "carriers": list(dict.fromkeys(s.carrier for leg in it.legs for s in leg.segments)),
            "itinerary_id": it.id,
        }
        if args.grouping == "by_month":
            quote = {"month": key[0], **quote}
        quotes.append(quote)
    cheapest = min(quotes, key=lambda q: q["min_price"], default=None)
    return {"currency": state.currency, "grouping": args.grouping, "quotes": quotes, "cheapest": cheapest}


def _itinerary(state: Flights, itinerary_id: str) -> Itinerary:
    return find(state.itineraries, f"No itinerary with id {itinerary_id!r}.", id=itinerary_id)


class GetItineraryArgs(BaseModel):
    itinerary_id: str = Field(description="The itinerary id from a search.")


def get_itinerary(world: World, args: GetItineraryArgs) -> dict:
    state = _flights(world)
    it = _itinerary(state, args.itinerary_id)
    legs = []
    for leg in it.legs:
        summary = _leg_summary(state, leg)
        summary["segments"] = [
            {
                "flight_number": s.flight_number,
                "carrier": s.carrier,
                "operated_by": s.operated_by or s.carrier,
                "origin": s.origin,
                "destination": s.destination,
                "departure": _stamp(s.departure),
                "arrival": _stamp(s.arrival),
            }
            for s in leg.segments
        ]
        legs.append(summary)
    options = []
    for o in sorted(it.pricing_options, key=lambda o: (o.price, o.id)):
        agent = _agent(state, o.agent_id)
        options.append(
            {
                "offer_id": o.id,
                "agent": agent.model_dump(),
                "price": money(o.price),
                "currency": state.currency,
                "fare_name": o.fare_name,
                "baggage": o.baggage.model_dump(),
                "refundable": o.refundable,
                "cancellation_fee": money(o.cancellation_fee),
                "changeable": o.changeable,
                "change_fee": money(o.change_fee),
                "description": o.description,
                "fare_notes": o.fare_notes,
                "terms": o.terms,
                "deep_link": o.deep_link,
            }
        )
    return {"itinerary_id": it.id, "cabin_class": it.cabin_class, "legs": legs, "pricing_options": options}


def _next_id(used: set[str], make) -> str:
    return fresh_id(make, used, len(used) + 1)


def _pnr(seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).digest()
    return "".join(_PNR_ALPHABET[b % len(_PNR_ALPHABET)] for b in digest[:6])


class BookOfferArgs(BaseModel):
    offer_id: str = Field(description="The offer (pricing option) id of the chosen provider.")
    passengers: list[Passenger] = Field(description="Every traveller, as named on their passport.")
    contact_email: str = Field("", description="Email for the provider's confirmation. Defaults to the user's.")


def book_offer(world: World, args: BookOfferArgs) -> dict:
    state = _flights(world)
    found = next(((it, o) for it in state.itineraries for o in it.pricing_options if o.id == args.offer_id), None)
    if found is None:
        raise ToolError(f"No offer with id {args.offer_id!r}.")
    it, offer = found
    if it.legs[0].departure <= world.now:
        raise ToolError("This flight has already departed; the offer is no longer available.")
    if not 1 <= len(args.passengers) <= 9:
        raise ToolError("A booking needs between 1 and 9 passengers.")
    for p in args.passengers:
        if p.born_on > world.today:
            raise ToolError(f"Passenger {p.given_name} {p.family_name} has a date of birth in the future.")
    agent = _agent(state, offer.agent_id)
    booking_id = _next_id({b.id for b in state.bookings}, lambda n: f"FLT-{n:06d}")
    booking = Booking(
        id=booking_id,
        provider_reference=_pnr(f"{offer.id}|{booking_id}"),
        itinerary_id=it.id,
        offer_id=offer.id,
        agent_id=agent.id,
        provider_name=agent.name,
        provider_type=agent.type,
        origin=it.legs[0].origin,
        destination=it.legs[0].destination,
        departure=it.legs[0].departure,
        return_departure=it.legs[1].departure if len(it.legs) > 1 else None,
        cabin_class=it.cabin_class,
        fare_name=offer.fare_name,
        passengers=args.passengers,
        baggage=offer.baggage.model_copy(),
        price_per_passenger=money(offer.price),
        total_price=money(offer.price * len(args.passengers)),
        currency=state.currency,
        refundable=offer.refundable,
        cancellation_fee=money(offer.cancellation_fee),
        changeable=offer.changeable,
        change_fee=money(offer.change_fee),
        terms=offer.terms,
        contact_email=args.contact_email or world.owner.email,
        booked_at=world.now,
    )
    state.bookings.append(booking)
    return {
        "booking_id": booking.id,
        "provider_reference": booking.provider_reference,
        "status": booking.status,
        "provider": booking.provider_name,
        "total_price": booking.total_price,
        "currency": booking.currency,
        "booked_at": _stamp(booking.booked_at),
    }


def _booking_view(b: Booking) -> dict:
    return {
        "booking_id": b.id,
        "provider_reference": b.provider_reference,
        "status": b.status,
        "provider": b.provider_name,
        "provider_type": b.provider_type,
        "itinerary_id": b.itinerary_id,
        "offer_id": b.offer_id,
        "origin": b.origin,
        "destination": b.destination,
        "departure": _stamp(b.departure),
        "return_departure": _stamp(b.return_departure) if b.return_departure else None,
        "cabin_class": b.cabin_class,
        "fare_name": b.fare_name,
        "passengers": list(b.passenger_names),
        "baggage": b.baggage.model_dump(),
        "total_price": b.total_price,
        "currency": b.currency,
        "refundable": b.refundable,
        "changeable": b.changeable,
        "terms": b.terms,
        "booked_at": _stamp(b.booked_at),
        "changed_at": _stamp(b.changed_at) if b.changed_at else None,
        "cancelled_at": _stamp(b.cancelled_at) if b.cancelled_at else None,
        "refund_amount": b.refund_amount,
    }


class ListBookingsArgs(BaseModel):
    status: Literal["confirmed", "cancelled"] | None = Field(None, description="Only bookings with this status.")


def list_bookings(world: World, args: ListBookingsArgs) -> dict:
    found = [b for b in _flights(world).bookings if args.status is None or b.status == args.status]
    found.sort(key=lambda b: (b.departure, b.id))
    return {"bookings": [_booking_view(b) for b in found]}


class CancelBookingArgs(BaseModel):
    booking_id: str = Field(description="The booking id (FLT-...).")


def cancel_booking(world: World, args: CancelBookingArgs) -> dict:
    booking = find(_flights(world).bookings, f"No booking with id {args.booking_id!r}.", id=args.booking_id)
    if booking.status == "cancelled":
        raise ToolError(f"Booking {booking.id!r} is already cancelled.")
    if booking.departure <= world.now:
        raise ToolError(f"Booking {booking.id!r} has already departed and cannot be cancelled.")
    refund = booking.total_price - booking.cancellation_fee * len(booking.passengers) if booking.refundable else 0.0
    booking.status = "cancelled"
    booking.cancelled_at = world.now
    booking.refund_amount = money(max(refund, 0.0))
    return {
        "booking_id": booking.id,
        "status": booking.status,
        "provider": booking.provider_name,
        "refund_amount": booking.refund_amount,
        "currency": booking.currency,
        "cancelled_at": _stamp(booking.cancelled_at),
    }


class RequestChangeArgs(BaseModel):
    booking_id: str = Field(description="The booking id (FLT-...).")
    slice: Literal["outbound", "return"] = Field(
        "outbound", description="Which flight of the booking to replace; the other one is kept."
    )
    new_date: date = Field(description="The new departure date for that flight, YYYY-MM-DD.")


def _change_view(state: Flights, c: ChangeOffer) -> dict:
    it = _itinerary(state, c.itinerary_id)
    return {
        "change_offer_id": c.id,
        "booking_id": c.booking_id,
        "legs": [_leg_summary(state, leg) for leg in it.legs],
        "change_total_amount": c.change_total_amount,
        "penalty_total_amount": c.penalty_total_amount,
        "new_total_amount": c.new_total_amount,
        "currency": state.currency,
        "refund_to": "original_form_of_payment" if c.change_total_amount < 0 else None,
        "expires_at": _stamp(c.expires_at),
    }


def request_change(world: World, args: RequestChangeArgs) -> dict:
    state = _flights(world)
    booking = find(state.bookings, f"No booking with id {args.booking_id!r}.", id=args.booking_id)
    if booking.status == "cancelled":
        raise ToolError(f"Booking {booking.id!r} is cancelled.")
    if not booking.changeable:
        raise ToolError(f"The fare of booking {booking.id!r} does not allow changes.")
    old = _itinerary(state, booking.itinerary_id)
    index = 0 if args.slice == "outbound" else 1
    if index >= len(old.legs):
        raise ToolError(f"Booking {booking.id!r} is one-way and has no return flight.")
    if old.legs[index].departure <= world.now:
        raise ToolError(f"The {args.slice} flight has already departed and cannot be changed.")
    kept = [leg for i, leg in enumerate(old.legs) if i != index]
    for c in state.change_offers:
        if c.booking_id == booking.id and c.status == "offered":
            c.status = "superseded"
    passengers = len(booking.passengers)
    offers = []
    for it in state.itineraries:
        if it.id == old.id or it.cabin_class != old.cabin_class or len(it.legs) != len(old.legs):
            continue
        leg = it.legs[index]
        if (leg.origin, leg.destination) != (old.legs[index].origin, old.legs[index].destination):
            continue
        if leg.departure.date() != args.new_date or leg.departure <= world.now:
            continue
        if [x for i, x in enumerate(it.legs) if i != index] != kept:
            continue
        if len(it.legs) > 1 and it.legs[1].departure <= it.legs[0].segments[-1].arrival:
            continue
        for o in it.pricing_options:
            if o.agent_id != booking.agent_id:
                continue
            new_total = money(o.price * passengers)
            penalty = money(booking.change_fee * passengers)
            change = ChangeOffer(
                id=_next_id({c.id for c in state.change_offers}, lambda n: f"oco-{n:05d}"),
                booking_id=booking.id,
                itinerary_id=it.id,
                offer_id=o.id,
                change_total_amount=money(new_total - booking.total_price + penalty),
                penalty_total_amount=penalty,
                new_total_amount=new_total,
                created_at=world.now,
                expires_at=world.now + timedelta(hours=72),
            )
            state.change_offers.append(change)
            offers.append(change)
    offers.sort(key=lambda c: (c.change_total_amount, _itinerary(state, c.itinerary_id).legs[index].departure))
    return {
        "booking_id": booking.id,
        "provider": booking.provider_name,
        "slice": args.slice,
        "change_offers": [_change_view(state, c) for c in offers],
    }


class ConfirmChangeArgs(BaseModel):
    change_offer_id: str = Field(description="The change offer id (oco-...) from flights_request_change.")


def confirm_change(world: World, args: ConfirmChangeArgs) -> dict:
    state = _flights(world)
    change = find(state.change_offers, f"No change offer with id {args.change_offer_id!r}.", id=args.change_offer_id)
    if change.status == "confirmed":
        raise ToolError(f"Change offer {change.id!r} has already been confirmed.")
    if change.status == "superseded":
        raise ToolError(f"Change offer {change.id!r} was replaced by a newer change request.")
    if change.expires_at <= world.now:
        raise ToolError(f"Change offer {change.id!r} has expired; request the change again.")
    booking = find(state.bookings, f"No booking with id {change.booking_id!r}.", id=change.booking_id)
    if booking.status == "cancelled":
        raise ToolError(f"Booking {booking.id!r} is cancelled.")
    it = _itinerary(state, change.itinerary_id)
    if it.legs[0].departure <= world.now:
        raise ToolError("The new flight has already departed; request the change again.")
    offer = find(it.pricing_options, f"No offer with id {change.offer_id!r}.", id=change.offer_id)
    booking.itinerary_id = it.id
    booking.offer_id = offer.id
    booking.departure = it.legs[0].departure
    booking.return_departure = it.legs[1].departure if len(it.legs) > 1 else None
    booking.price_per_passenger = money(offer.price)
    booking.total_price = change.new_total_amount
    booking.changed_at = world.now
    change.status = "confirmed"
    change.confirmed_at = world.now
    for c in state.change_offers:
        if c.booking_id == booking.id and c.status == "offered":
            c.status = "superseded"
    return {
        "order_change_id": change.id,
        "booking_id": booking.id,
        "provider_reference": booking.provider_reference,
        "departure": _stamp(booking.departure),
        "return_departure": _stamp(booking.return_departure) if booking.return_departure else None,
        "change_total_amount": change.change_total_amount,
        "penalty_total_amount": change.penalty_total_amount,
        "new_total_amount": change.new_total_amount,
        "currency": state.currency,
        "refund_to": "original_form_of_payment" if change.change_total_amount < 0 else None,
        "confirmed_at": _stamp(change.confirmed_at),
    }


def _current_price(world: World, alert: PriceAlert) -> float | None:
    found = _search(world, alert.origin, alert.destination, alert.depart_date, alert.return_date, alert.cabin_class)
    return min((_cheapest(it) for it in found), default=None)


class CreatePriceAlertArgs(BaseModel):
    origin: str = Field(description="Origin airport code (IATA) or city name.")
    destination: str = Field(description="Destination airport code (IATA) or city name.")
    depart_date: date = Field(description="Outbound date, YYYY-MM-DD.")
    return_date: date | None = Field(None, description="Return date, YYYY-MM-DD. Omit for one-way.")
    adults: int = Field(1, ge=1, le=8, description="Number of adult travellers.")
    cabin_class: CabinClass = Field("economy", description="Cabin class.")


def create_price_alert(world: World, args: CreatePriceAlertArgs) -> dict:
    _validate_dates(args.depart_date, args.return_date)
    if args.depart_date < world.today:
        raise ToolError("The departure date is in the past.")
    state = _flights(world)
    _places(state, args.origin)
    _places(state, args.destination)
    fields = args.model_dump()
    for a in state.price_alerts:
        if a.status == "active" and all(getattr(a, k) == v for k, v in fields.items()):
            raise ToolError(f"A price alert for this search already exists ({a.id}).")
    alert = PriceAlert(
        id=_next_id({a.id for a in state.price_alerts}, lambda n: f"pa-{n}"), created_at=world.now, **fields
    )
    alert.price_when_created = _current_price(world, alert)
    state.price_alerts.append(alert)
    return {
        "alert_id": alert.id,
        "status": alert.status,
        "current_price": alert.price_when_created,
        "currency": state.currency,
    }


class ListPriceAlertsArgs(BaseModel):
    include_deleted: bool = Field(False, description="Also list deleted alerts.")


def list_price_alerts(world: World, args: ListPriceAlertsArgs) -> dict:
    state = _flights(world)
    alerts = []
    for a in state.price_alerts:
        if a.status == "deleted" and not args.include_deleted:
            continue
        now = _current_price(world, a) if a.status == "active" else None
        was = a.price_when_created
        alerts.append(
            {
                "alert_id": a.id,
                "status": a.status,
                "origin": a.origin,
                "destination": a.destination,
                "depart_date": a.depart_date.isoformat(),
                "return_date": a.return_date.isoformat() if a.return_date else None,
                "adults": a.adults,
                "cabin_class": a.cabin_class,
                "price_when_created": was,
                "current_price": now,
                "change": money(now - was) if now is not None and was is not None else None,
                "created_at": _stamp(a.created_at),
            }
        )
    return {"currency": state.currency, "price_alerts": alerts}


class DeletePriceAlertArgs(BaseModel):
    alert_id: str = Field(description="The price alert id.")


def delete_price_alert(world: World, args: DeletePriceAlertArgs) -> dict:
    alert = find(_flights(world).price_alerts, f"No price alert with id {args.alert_id!r}.", id=args.alert_id)
    if alert.status == "deleted":
        raise ToolError(f"Price alert {alert.id!r} is already deleted.")
    alert.status = "deleted"
    alert.deleted_at = world.now
    return {"alert_id": alert.id, "status": alert.status}


APP = App(
    name="flights",
    title="flights",
    state=Flights,
    keys={
        "airports": "iata",
        "agents": "id",
        "itineraries": "id",
        "bookings": "id",
        "change_offers": "id",
        "price_alerts": "id",
    },
    tools=[
        Tool(
            "flights_live_search",
            "Search live flight prices, one-way or return, across airlines and online travel agents. Returns "
            "itineraries with their legs and every provider's offer (offer_id, agent, fare, price per passenger and "
            "total). Use flights_get_itinerary for baggage, fare rules and provider terms.",
            LiveSearchArgs,
            live_search,
        ),
        Tool(
            "flights_indicative_search",
            "Browse indicative (cached) cheapest prices over flexible dates: a date range or anytime, grouped by "
            "date or by month to find the cheapest day or cheapest month to fly.",
            IndicativeSearchArgs,
            indicative_search,
        ),
        Tool(
            "flights_get_itinerary",
            "Get an itinerary's segments and all offers from providers: provider rating, fare name, baggage, "
            "refundability, cancellation fee, the offer description, fare notes and the provider's terms.",
            GetItineraryArgs,
            get_itinerary,
        ),
        Tool(
            "flights_book_offer",
            "Book an offer through its provider (airline or travel agent) for the given passengers and pay the "
            "provider. Returns the booking id, the provider's reference and the total charged.",
            BookOfferArgs,
            book_offer,
            writes=True,
        ),
        Tool(
            "flights_list_bookings",
            "List the user's flight bookings made through the site, with provider, fare, passengers, baggage, "
            "terms and status.",
            ListBookingsArgs,
            list_bookings,
        ),
        Tool(
            "flights_cancel_booking",
            "Cancel a flight booking with its provider. Refundable fares are refunded minus the provider's "
            "cancellation fee per passenger; non-refundable fares get no refund.",
            CancelBookingArgs,
            cancel_booking,
            writes=True,
        ),
        Tool(
            "flights_request_change",
            "Ask the booking's provider for offers to move one flight of a booking (outbound or return) to a new "
            "date, keeping the other flight. Each offer shows the new flights, the change penalty, the amount to "
            "pay now (negative means a refund) and the new total. Nothing changes until an offer is confirmed.",
            RequestChangeArgs,
            request_change,
            writes=True,
        ),
        Tool(
            "flights_confirm_change",
            "Confirm a change offer from flights_request_change and pay its change amount to the provider. The "
            "booking keeps its id and reference and moves to the new flights.",
            ConfirmChangeArgs,
            confirm_change,
            writes=True,
        ),
        Tool(
            "flights_create_price_alert",
            "Create a price alert for a flight search (route, dates, travellers, cabin) to be told when its price "
            "changes.",
            CreatePriceAlertArgs,
            create_price_alert,
            writes=True,
        ),
        Tool(
            "flights_list_price_alerts",
            "List price alerts with the price when each was created, the current cheapest price and the change.",
            ListPriceAlertsArgs,
            list_price_alerts,
        ),
        Tool(
            "flights_delete_price_alert",
            "Delete a price alert.",
            DeletePriceAlertArgs,
            delete_price_alert,
            writes=True,
        ),
    ],
)
