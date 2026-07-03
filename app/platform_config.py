"""Load platform CMS data (categories, regions, banners) for mobile catalog APIs."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.form_options import LISTING_FORM_OPTIONS, _opt
from app.models import PlatformBanner, PlatformCategory, PlatformRegion
from app.routers.region_safety import REGION_DATA
from app.schemas import FormOptionDto, ListingFormOptionsDto, RegionCityDto, RegionDto


def form_options_from_db(db: Session) -> ListingFormOptionsDto:
    rows = (
        db.query(PlatformCategory)
        .filter(PlatformCategory.enabled.is_(True))
        .order_by(PlatformCategory.sort_order.asc(), PlatformCategory.id.asc())
        .all()
    )
    if not rows:
        return LISTING_FORM_OPTIONS

    product_cats = [
        _opt(row.key, row.label_en, row.label_zh)
        for row in rows
        if row.type == "product"
    ]
    service_types = [
        _opt(row.key, row.label_en, row.label_zh)
        for row in rows
        if row.type == "service"
    ]
    if not product_cats:
        product_cats = list(LISTING_FORM_OPTIONS.categories)
    if not service_types:
        service_types = list(LISTING_FORM_OPTIONS.serviceTypes)

    return ListingFormOptionsDto(
        categories=product_cats,
        conditions=list(LISTING_FORM_OPTIONS.conditions),
        pickupMethods=list(LISTING_FORM_OPTIONS.pickupMethods),
        deliveryMethods=list(LISTING_FORM_OPTIONS.deliveryMethods),
        serviceTypes=service_types,
        serviceAreas=list(LISTING_FORM_OPTIONS.serviceAreas),
        serviceTimeSlots=list(LISTING_FORM_OPTIONS.serviceTimeSlots),
    )


def regions_from_db(db: Session) -> list[RegionDto]:
    rows = (
        db.query(PlatformRegion)
        .filter(PlatformRegion.enabled.is_(True))
        .order_by(PlatformRegion.sort_order.asc(), PlatformRegion.id.asc())
        .all()
    )
    if not rows:
        return REGION_DATA

    by_state: dict[str, dict[str, dict]] = {}
    state_names = {r.state: r.stateName for r in REGION_DATA}
    city_zh: dict[tuple[str, str], str] = {}
    for region in REGION_DATA:
        for city in region.cities:
            city_zh[(region.state, city.name)] = city.cn

    for row in rows:
        state_bucket = by_state.setdefault(row.state, {"cities": {}})
        city_bucket = state_bucket["cities"].setdefault(
            row.city,
            {"name": row.city, "cn": row.label_zh if not row.area else city_zh.get((row.state, row.city), row.city), "areas": []},
        )
        if row.area:
            city_bucket["areas"].append(row.area)

    result: list[RegionDto] = []
    for state, payload in by_state.items():
        cities = []
        for city_name, city_data in payload["cities"].items():
            areas = city_data["areas"] or [city_name]
            cities.append(
                RegionCityDto(
                    name=city_name,
                    cn=city_data["cn"],
                    areas=areas,
                )
            )
        result.append(
            RegionDto(
                state=state,
                stateName=state_names.get(state, state),
                cities=cities,
            )
        )
    return result or REGION_DATA


def banners_from_db(db: Session, position: str = "home") -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = (
        db.query(PlatformBanner)
        .filter(PlatformBanner.enabled.is_(True), PlatformBanner.position == position)
        .order_by(PlatformBanner.created_at.desc())
        .all()
    )
    items: list[dict] = []
    for row in rows:
        if row.online_at and row.online_at > now:
            continue
        if row.offline_at and row.offline_at <= now:
            continue
        items.append(
            {
                "id": row.id,
                "title": row.title,
                "imageUrl": row.image_url,
                "linkUrl": row.link_url,
                "position": row.position,
            }
        )
    return items
