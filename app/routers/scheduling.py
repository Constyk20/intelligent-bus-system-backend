from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends

from app.routers.auth import get_current_staff, require_roles
from app.schemas.scheduling import RecommendationResponse
from app.services.scheduling_service import (
    approve_recommendation,
    generate_recommendations,
    list_recommendations,
    reject_recommendation,
)

router = APIRouter(prefix="/scheduling", tags=["Scheduling"])
MANAGERS = ("super_admin", "terminal_admin")


@router.post("/recommendations/generate", response_model=List[RecommendationResponse])
def generate(staff: Annotated[dict, Depends(require_roles(*MANAGERS))]):
    return [RecommendationResponse(**doc) for doc in generate_recommendations(staff)]


@router.get("/recommendations", response_model=List[RecommendationResponse])
def listing(
    staff: Annotated[dict, Depends(get_current_staff)],
    status: Optional[str] = None,
):
    return [RecommendationResponse(**doc) for doc in list_recommendations(staff, status)]


@router.post("/recommendations/{recommendation_id}/approve", response_model=RecommendationResponse)
def approve(
    recommendation_id: str,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return RecommendationResponse(**approve_recommendation(recommendation_id, staff))


@router.post("/recommendations/{recommendation_id}/reject", response_model=RecommendationResponse)
def reject(
    recommendation_id: str,
    staff: Annotated[dict, Depends(require_roles(*MANAGERS))],
):
    return RecommendationResponse(**reject_recommendation(recommendation_id, staff))