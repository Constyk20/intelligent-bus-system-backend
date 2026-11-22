from app.schemas.booking import BookingCreate
from app.schemas.passenger import CardIssue, PassengerCreate
from app.services.booking_service import create_booking
from app.services.passenger_service import issue_card, register_passenger


def simulate_arrival(data, staff: dict) -> dict:
    """Create a demonstration passenger, fund a card, and place them in the queue."""
    from app.database import get_database
    from bson import ObjectId
    from fastapi import HTTPException

    route = get_database()["routes"].find_one({"_id": ObjectId(data.route_id)})
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    passenger = register_passenger(
        PassengerCreate(
            full_name=data.full_name,
            phone=data.phone,
            terminal_id=route["terminal_id"],
        ),
        staff,
    )
    card = issue_card(
        CardIssue(passenger_id=str(passenger["_id"]), initial_balance=data.initial_balance),
        staff,
    )
    booking = create_booking(
        BookingCreate(card_number=card["card_number"], route_id=str(route["_id"])),
        staff,
    )
    return {
        "passenger_id": str(passenger["_id"]),
        "passenger_name": passenger["full_name"],
        "card_number": card["card_number"],
        "balance": card["balance"],
        "booking_reference": booking["booking_reference"],
        "ticket_number": booking.get("ticket_number"),
        "queue_status": "waiting",
        "simulated": True,
    }