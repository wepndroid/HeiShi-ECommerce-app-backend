from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.database import get_db
from app.models import Coupon, Favorite, Follow, Listing, User, ViewHistory
from app.pagination import paginate
from app.schemas import CouponDto, FavoriteDto, FollowDto, Paginated
from app.serializers import coupon_to_dto, favorite_to_dto, follow_to_dto, iso

router = APIRouter(tags=["user-library"])


@router.get("/favorites", response_model=Paginated[FavoriteDto])
def list_favorites(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Favorite).filter(Favorite.user_id == user.id).order_by(Favorite.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([favorite_to_dto(f.listing_id, f.created_at) for f in items], page, pageSize, total)


@router.post("/favorites/{listing_id}", response_model=FavoriteDto, status_code=201)
def add_favorite(listing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    fav = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.listing_id == listing_id).first()
    if fav:
        return favorite_to_dto(fav.listing_id, fav.created_at)
    fav = Favorite(user_id=user.id, listing_id=listing_id)
    listing.favorite_count += 1
    db.add(fav)
    db.commit()
    db.refresh(fav)
    return favorite_to_dto(fav.listing_id, fav.created_at)


@router.delete("/favorites/{listing_id}", status_code=204)
def remove_favorite(listing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    fav = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.listing_id == listing_id).first()
    if fav:
        listing = db.query(Listing).filter(Listing.id == listing_id).first()
        if listing and listing.favorite_count > 0:
            listing.favorite_count -= 1
        db.delete(fav)
        db.commit()
    return Response(status_code=204)


@router.get("/history/views", response_model=Paginated[dict])
def list_history(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(ViewHistory).filter(ViewHistory.user_id == user.id).order_by(ViewHistory.viewed_at.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    dto_items = [{"listingId": h.listing_id, "viewedAt": iso(h.viewed_at)} for h in items]
    return paginate(dto_items, page, pageSize, total)


@router.post("/history/views/{listing_id}", status_code=204)
def record_view(listing_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Listing not found", "details": {}})
    record = db.query(ViewHistory).filter(ViewHistory.user_id == user.id, ViewHistory.listing_id == listing_id).first()
    now = datetime.now(timezone.utc)
    if record:
        record.viewed_at = now
    else:
        db.add(ViewHistory(user_id=user.id, listing_id=listing_id, viewed_at=now))
    db.commit()
    return Response(status_code=204)


@router.get("/follows", response_model=Paginated[FollowDto])
def list_follows(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (
        db.query(Follow)
        .filter(Follow.follower_id == user.id)
        .order_by(Follow.created_at.desc())
    )
    total = q.count()
    follows = q.offset((page - 1) * pageSize).limit(pageSize).all()
    items = []
    for f in follows:
        followed = db.query(User).filter(User.id == f.followed_id).first()
        if followed:
            items.append(follow_to_dto(followed, f.created_at))
    return paginate(items, page, pageSize, total)


@router.post("/follows/{target_user_id}", status_code=204)
def follow_user(target_user_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if target_user_id == user.id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot follow yourself", "details": {}})
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    existing = db.query(Follow).filter(Follow.follower_id == user.id, Follow.followed_id == target_user_id).first()
    if not existing:
        db.add(Follow(follower_id=user.id, followed_id=target_user_id))
        db.commit()
    return Response(status_code=204)


@router.delete("/follows/{target_user_id}", status_code=204)
def unfollow_user(target_user_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    follow = db.query(Follow).filter(Follow.follower_id == user.id, Follow.followed_id == target_user_id).first()
    if follow:
        db.delete(follow)
        db.commit()
    return Response(status_code=204)


@router.get("/coupons", response_model=Paginated[CouponDto])
def list_coupons(
    status: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Coupon).filter(Coupon.user_id == user.id)
    if status:
        q = q.filter(Coupon.status == status)
    q = q.order_by(Coupon.id.desc())
    total = q.count()
    items = q.offset((page - 1) * pageSize).limit(pageSize).all()
    return paginate([coupon_to_dto(c) for c in items], page, pageSize, total)


@router.post("/coupons/{coupon_id}/redeem", status_code=204)
def redeem_coupon(coupon_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.user_id == user.id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Coupon not found", "details": {}})
    if coupon.status != "available":
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Coupon not available", "details": {}})
    coupon.status = "used"
    db.commit()
    return Response(status_code=204)
