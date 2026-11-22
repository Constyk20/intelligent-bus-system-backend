from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.schemas.booking import BookingCreate
from app.schemas.core import BusCreate, DriverCreate, RouteCreate, TerminalCreate
from app.schemas.passenger import CardIssue, PassengerCreate
from app.schemas.trip import TripCreate
from app.services.booking_service import create_booking
from app.services.core_service import create_bus, create_driver, create_route, create_terminal
from app.services.passenger_service import identify_card, issue_card, register_passenger
from app.services.queue_service import board_passenger, call_passenger
from app.services.scheduling_service import approve_recommendation, generate_recommendations
from app.services.ticket_service import cancel_booking, get_ticket
from app.services.trip_service import complete_trip, create_trip, dispatch_trip


def build_terminal(staff):
    terminal = create_terminal(TerminalCreate(
        name="ABSU Terminal Uturu",
        code="ABSU-TEST",
        city="Uturu",
        state="Abia",
    ))
    route = create_route(RouteCreate(
        terminal_id=str(terminal["_id"]),
        origin="Uturu",
        destination="Umuahia",
        fare="1500.00",
        estimated_duration_minutes=45,
    ), staff)
    bus = create_bus(BusCreate(
        registration_number="TEST-BUS-1",
        terminal_id=str(terminal["_id"]),
        capacity=3,
    ), staff)
    driver = create_driver(DriverCreate(
        full_name="Test Driver",
        phone="08030000000",
        license_number="LIC-TEST-1",
        terminal_id=str(terminal["_id"]),
    ), staff)
    return terminal, route, bus, driver


def issue_funded_card(staff, terminal_id, phone, balance):
    passenger = register_passenger(PassengerCreate(
        full_name=f"Passenger {phone[-4:]}",
        phone=phone,
        terminal_id=terminal_id,
    ), staff)
    card = issue_card(CardIssue(
        passenger_id=str(passenger["_id"]),
        initial_balance=balance,
    ), staff)
    return card


def test_booking_debits_wallet_once_and_creates_ticket_and_queue(db, staff):
    _, route, _, _ = build_terminal(staff)
    card = issue_funded_card(staff, route["terminal_id"], "08031111111", "5000.00")

    booking = create_booking(BookingCreate(
        card_number=card["card_number"],
        route_id=str(route["_id"]),
        idempotency_key="same-booking",
    ), staff)
    repeated = create_booking(BookingCreate(
        card_number=card["card_number"],
        route_id=str(route["_id"]),
        idempotency_key="same-booking",
    ), staff)

    updated = identify_card(card["card_number"])
    assert booking["booking_reference"] == repeated["booking_reference"]
    assert booking["ticket_number"]
    assert updated["balance"] == "3500.00"
    assert db["bookings"].count_documents({}) == 1
    assert db["tickets"].count_documents({}) == 1
    assert db["queues"].count_documents({"status": "waiting"}) == 1
    assert db["wallet_transactions"].count_documents({"type": "debit"}) == 1


def test_insufficient_balance_creates_nothing(db, staff):
    _, route, _, _ = build_terminal(staff)
    card = issue_funded_card(staff, route["terminal_id"], "08032222222", "1000.00")

    with pytest.raises(HTTPException) as caught:
        create_booking(BookingCreate(
            card_number=card["card_number"],
            route_id=str(route["_id"]),
        ), staff)

    assert caught.value.status_code == 400
    assert identify_card(card["card_number"])["balance"] == "1000.00"
    assert db["bookings"].count_documents({}) == 0
    assert db["tickets"].count_documents({}) == 0
    assert db["queues"].count_documents({}) == 0


def test_concurrent_bookings_cannot_overdraw(db, staff):
    _, route, _, _ = build_terminal(staff)
    card = issue_funded_card(staff, route["terminal_id"], "08033333333", "1500.00")

    def attempt(key):
        try:
            create_booking(BookingCreate(
                card_number=card["card_number"],
                route_id=str(route["_id"]),
                idempotency_key=key,
            ), staff)
            return "ok"
        except HTTPException:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))

    balance = Decimal(identify_card(card["card_number"])["balance"])
    bookings = db["bookings"].count_documents({})
    debits = db["wallet_transactions"].count_documents({"type": "debit"})
    assert results.count("ok") == 1
    assert balance == Decimal("0.00")
    assert bookings == 1
    assert debits == 1


def test_cancel_refunds_once_and_removes_queue(db, staff):
    _, route, _, _ = build_terminal(staff)
    card = issue_funded_card(staff, route["terminal_id"], "08034444444", "5000.00")
    booking = create_booking(BookingCreate(
        card_number=card["card_number"],
        route_id=str(route["_id"]),
    ), staff)

    cancel_booking(booking["booking_reference"], staff)
    cancel_booking(booking["booking_reference"], staff)

    assert identify_card(card["card_number"])["balance"] == "5000.00"
    assert db["bookings"].find_one({})["status"] == "cancelled"
    assert db["tickets"].find_one({})["status"] == "cancelled"
    assert db["queues"].find_one({})["status"] == "cancelled"
    assert db["wallet_transactions"].count_documents({"type": "refund"}) == 1


def test_boarding_and_simulated_trip(db, staff):
    _, route, bus, driver = build_terminal(staff)
    card = issue_funded_card(staff, route["terminal_id"], "08035555555", "5000.00")
    booking = create_booking(BookingCreate(
        card_number=card["card_number"],
        route_id=str(route["_id"]),
    ), staff)
    call_passenger(booking["booking_reference"], staff)
    ticket = get_ticket(booking["ticket_number"], staff)
    token = ticket["qr_payload"].split(".", 1)[1]
    boarded = board_passenger(booking["booking_reference"], booking["ticket_number"], token, staff)
    assert boarded["status"] == "boarded"

    trip = create_trip(TripCreate(
        route_id=str(route["_id"]),
        bus_id=str(bus["_id"]),
        driver_id=str(driver["_id"]),
    ), staff)
    dispatched = dispatch_trip(trip["trip_reference"], staff)
    completed = complete_trip(dispatched["trip_reference"], staff)

    assert dispatched["passenger_count"] == 1
    assert dispatched["simulated"] is True
    assert completed["status"] == "completed"
    assert completed["progress_percent"] == 100
    assert db["buses"].find_one({"_id": bus["_id"]})["status"] == "available"


def test_scheduler_recommends_and_approval_does_not_dispatch(db, staff):
    _, route, _, _ = build_terminal(staff)
    for phone in ("08036666666", "08037777777"):
        card = issue_funded_card(staff, route["terminal_id"], phone, "5000.00")
        create_booking(BookingCreate(card_number=card["card_number"], route_id=str(route["_id"])), staff)

    recommendations = generate_recommendations(staff)
    assert len(recommendations) == 1
    assert recommendations[0]["waiting_passengers"] == 2
    assert recommendations[0]["priority_score"] >= 200

    approved = approve_recommendation(recommendations[0]["id"], staff)
    trip = db["trips"].find_one({"trip_reference": approved["trip_reference"]})
    assert approved["status"] == "approved"
    assert trip["status"] == "scheduled"
    assert db["buses"].find_one({})["status"] == "available"