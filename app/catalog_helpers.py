from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from app.models import Listing

OTHER_AREAS = "其他地区"
ALL_AREAS = "全部区域"
ALL_AREAS_SENTINELS = {ALL_AREAS, OTHER_AREAS}


def normalize_location(loc: str) -> str:
    return "Melbourne CBD" if loc == "CBD" else loc


def listing_in_region(listing: Listing, region_state: str | None, region_city: str | None, region_area: str | None) -> bool:
    if region_state and listing.region_state != region_state:
        return False
    if region_city and listing.region_city != region_city:
        return False
    if not region_area or region_area in ALL_AREAS_SENTINELS:
        return True
    loc = normalize_location(listing.location_label)
    area = normalize_location(region_area)
    if area == OTHER_AREAS:
        known = {
            "Box Hill", "Glen Waverley", "Clayton", "Doncaster", "Melbourne CBD",
            "Southbank", "Carlton", "Burwood", "Docklands", "Richmond", "Online",
        }
        return loc not in known
    if area == "Melbourne CBD":
        return "CBD" in loc or "Melbourne" in loc
    return loc == area or area in loc or loc in area


def apply_region_filter(q: Query, region_state: str | None, region_city: str | None, region_area: str | None) -> Query:
    if region_state:
        q = q.filter(Listing.region_state == region_state)
    if region_city:
        q = q.filter(Listing.region_city == region_city)
    if region_area and region_area not in ALL_AREAS_SENTINELS:
        if region_area == OTHER_AREAS:
            known = [
                "Box Hill", "Glen Waverley", "Clayton", "Doncaster", "Melbourne CBD",
                "Southbank", "Carlton", "Burwood", "Docklands", "Richmond",
            ]
            q = q.filter(~Listing.location_label.in_(known))
        elif region_area == "Melbourne CBD":
            q = q.filter(or_(Listing.location_label.contains("CBD"), Listing.location_label.contains("Melbourne")))
        else:
            q = q.filter(
                or_(
                    Listing.location_label == region_area,
                    Listing.location_label.contains(region_area),
                    Listing.region_area == region_area,
                )
            )
    return q


def apply_tab_filter(q: Query, tab: str | None) -> Query:
    if not tab or tab == "recommended":
        return q
    if tab == "newArrivals":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        return q.filter(Listing.created_at >= cutoff)
    if tab == "digital":
        return q.filter(Listing.category_key == "digital")
    if tab == "services":
        return q.filter(Listing.type == "service")
    if tab == "tickets":
        return q.filter(Listing.category_key == "tickets")
    return q


def apply_search(q: Query, q_text: str | None, sort: str | None) -> Query:
    if q_text:
        pattern = f"%{q_text.strip()}%"
        q = q.filter(or_(Listing.title.ilike(pattern), Listing.description.ilike(pattern), Listing.title_zh.ilike(pattern)))
    if sort == "priceAsc":
        q = q.order_by(Listing.price.asc())
    elif sort == "priceDesc":
        q = q.order_by(Listing.price.desc())
    elif sort == "newest":
        q = q.order_by(Listing.created_at.desc())
    elif sort == "relevance" and q_text:
        q = q.order_by(Listing.view_count.desc(), Listing.created_at.desc())
    else:
        q = q.order_by(Listing.created_at.desc())
    return q


def get_or_create_settings(db: Session, user_id: str):
    from app.models import UserSettings

    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        settings = UserSettings(user_id=user_id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings
