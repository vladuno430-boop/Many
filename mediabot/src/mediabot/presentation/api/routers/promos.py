"""Promo-code management and redemption endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mediabot.domain.enums import AdminRole, PromoStatus, SubscriptionTier
from mediabot.presentation.api.dependencies import (
    ActorDep,
    AdminDep,
    ContainerDep,
    require_role,
)
from mediabot.presentation.api.schemas import (
    PromoCreateRequest,
    PromoRedeemRequest,
    PromoRedeemResponse,
    PromoResponse,
)

router = APIRouter(tags=["promo"], prefix="/promo")


def _to_response(promo: object) -> PromoResponse:
    return PromoResponse(
        code=promo.code,  # type: ignore[attr-defined]
        promo_type=promo.promo_type,  # type: ignore[attr-defined]
        status=promo.status.value,  # type: ignore[attr-defined]
        value=promo.value,  # type: ignore[attr-defined]
        activations_used=promo.activations_used,  # type: ignore[attr-defined]
        max_activations=promo.max_activations,  # type: ignore[attr-defined]
        expires_at=promo.expires_at,  # type: ignore[attr-defined]
        campaign=promo.campaign,  # type: ignore[attr-defined]
    )


@router.get("", response_model=list[PromoResponse])
async def list_promos(
    container: ContainerDep,
    _role: AdminDep,
    status: PromoStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PromoResponse]:
    promos = await container.admin.list_promos(status=status, limit=limit, offset=offset)
    return [_to_response(promo) for promo in promos]


@router.post(
    "",
    response_model=PromoResponse,
    dependencies=[Depends(require_role(AdminRole.ADMIN))],
)
async def create_promo(
    payload: PromoCreateRequest,
    container: ContainerDep,
    role: AdminDep,
    actor_id: ActorDep,
) -> PromoResponse:
    """Create a promo code (admin only)."""
    promo = await container.admin.create_promo(
        actor=role,
        actor_id=actor_id,
        promo_type=payload.promo_type,
        value=payload.value,
        code=payload.code,
        max_activations=payload.max_activations,
        per_user_limit=payload.per_user_limit,
        expires_in_days=payload.expires_in_days,
        min_tier=SubscriptionTier(payload.min_tier) if payload.min_tier else None,
        new_users_only=payload.new_users_only,
        campaign=payload.campaign,
        description=payload.description,
    )
    return _to_response(promo)


@router.delete(
    "/{code}",
    dependencies=[Depends(require_role(AdminRole.ADMIN))],
)
async def disable_promo(
    code: str,
    container: ContainerDep,
    role: AdminDep,
    actor_id: ActorDep,
) -> dict[str, str]:
    await container.admin.disable_promo(actor=role, actor_id=actor_id, code=code)
    return {"code": code, "status": PromoStatus.DISABLED.value}


@router.get("/{code}/statistics")
async def promo_statistics(
    code: str,
    container: ContainerDep,
    _role: AdminDep,
) -> dict[str, object]:
    """Usage statistics and recent activations of one promo code."""
    data = await container.admin.promo_statistics(code)
    promo = data["promo"]
    return {
        "promo": _to_response(promo).model_dump(),
        "stats": data["stats"],
        "recent": data["recent"],
    }


@router.post("/redeem", response_model=PromoRedeemResponse)
async def redeem_promo(
    payload: PromoRedeemRequest,
    container: ContainerDep,
    _role: AdminDep,
) -> PromoRedeemResponse:
    """Redeem a promo code on behalf of a user."""
    redemption = await container.promos.redeem(payload.user_id, payload.code)
    if redemption.lifetime:
        await container.subscriptions.grant(
            user_id=payload.user_id, tier=SubscriptionTier.LIFETIME, days=0, source="promo_api"
        )
    elif redemption.vip_days:
        await container.subscriptions.grant(
            user_id=payload.user_id,
            tier=SubscriptionTier.VIP,
            days=redemption.vip_days,
            source="promo_api",
        )
    elif redemption.premium_days:
        await container.subscriptions.grant(
            user_id=payload.user_id,
            tier=SubscriptionTier.PREMIUM,
            days=redemption.premium_days,
            source="promo_api",
        )
    return PromoRedeemResponse(
        code=redemption.code,
        summary=redemption.summary,
        coins=redemption.coins,
        premium_days=redemption.premium_days,
        vip_days=redemption.vip_days,
    )
