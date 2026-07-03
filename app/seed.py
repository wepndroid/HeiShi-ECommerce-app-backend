"""Seed database with demo data matching frontend mock content."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.avatar_photos import avatar_url_for_user_id
from app.auth import hash_password, normalize_phone
from app.config import settings
from app.coupon_service import issue_referral_coupon, issue_welcome_coupon
from app.moderation import seed_blocked_keywords
from app.models import (
    Coupon,
    Listing,
    Order,
    PaymentMethod,
    PlatformCategory,
    PlatformRegion,
    Review,
    SystemNotification,
    User,
    UserSettings,
)
from app.routers.region_safety import REGION_DATA

PRODUCTS = [
    (1, "Edifier W820NB Noise-Cancelling Headphones", "漫步者降噪耳机", 89, "digital", "lightlyUsed", "Clayton", "mia", "Mia_墨尔本"),
    (2, "SMEG Style Electric Kettle", "思美格风格电水壶", 129, "home", "brandNew", "Melbourne CBD", "sunny", "Sunny"),
    (3, "Keychron K2 Mechanical Keyboard", "K2 机械键盘", 79, "digital", "likeNew95", "Carlton", "lucas", "Lucas_墨尔本"),
    (4, "Fujifilm instax mini 40", "富士 mini 40 拍立得", 95, "digital", "withFilm", "Box Hill", "xiaoyu", "小雨同学"),
    (5, "Nordic Folding Desk", "北欧折叠书桌", 45, "home", "foldable", "Burwood", "amy", "Amy"),
    (6, "Melbourne Concert Ticket", "墨尔本演唱会门票", 68, "tickets", "lowPriceTransfer", "Southbank", "ticketShop", "票券小铺"),
    (7, "PTE Speaking Coaching 1-on-1", "PTE口语陪练 1v1", 30, "services", "localService", "Online", "pte", "PTE学长"),
    (8, "Ukulele Starter Kit", "尤克里里初学套装", 55, "misc", "fullAccessories", "Docklands", "luna", "Luna"),
    (9, "Used Bicycle Helmet", "二手自行车头盔", 25, "misc", "safetyGear", "Richmond", "coffee", "咖啡不加糖"),
    (10, "Dyson Supersonic Hair Dryer", "戴森吹风机", 299, "home", "lightlyUsed", "Glen Waverley", "sunny", "Sunny"),
    (11, "Motorcycle Helmet & Gloves", "摩托车头盔与手套", 120, "misc", "fullAccessories", "Doncaster", "allen", "Allen"),
    (12, "Marketing Textbooks Bundle", "市场营销教材资料", 40, "misc", "courseMaterials", "Clayton", "lily", "Lily"),
]

IMAGES = {
    1: "https://images.pexels.com/photos/3780681/pexels-photo-3780681.jpeg?auto=compress&cs=tinysrgb&w=800",
    2: "https://images.pexels.com/photos/4229710/pexels-photo-4229710.jpeg?auto=compress&cs=tinysrgb&w=800",
    3: "https://images.pexels.com/photos/5472358/pexels-photo-5472358.jpeg?auto=compress&cs=tinysrgb&w=800",
    4: "https://images.pexels.com/photos/6964061/pexels-photo-6964061.jpeg?auto=compress&cs=tinysrgb&w=800",
    5: "https://images.pexels.com/photos/2343474/pexels-photo-2343474.jpeg?auto=compress&cs=tinysrgb&w=800",
    6: "https://images.pexels.com/photos/1105666/pexels-photo-1105666.jpeg?auto=compress&cs=tinysrgb&w=800",
    7: "https://images.pexels.com/photos/3782179/pexels-photo-3782179.jpeg?auto=compress&cs=tinysrgb&w=800",
    8: "https://images.pexels.com/photos/8414510/pexels-photo-8414510.jpeg?auto=compress&cs=tinysrgb&w=800",
    9: "https://images.pexels.com/photos/2254065/pexels-photo-2254065.jpeg?auto=compress&cs=tinysrgb&w=800",
    10: "https://images.pexels.com/photos/3992209/pexels-photo-3992209.jpeg?auto=compress&cs=tinysrgb&w=800",
    11: "https://images.pexels.com/photos/2379004/pexels-photo-2379004.jpeg?auto=compress&cs=tinysrgb&w=800",
    12: "https://images.pexels.com/photos/159711/books-bookstore-book-reading-159711.jpeg?auto=compress&cs=tinysrgb&w=800",
}

SERVICES = [
    (101, "Moving Help", "搬家帮手", 60, "Clayton / Box Hill", "truck", "allen", "Allen"),
    (102, "Home Cleaning", "家庭清洁", 120, "Melbourne CBD / Southbank", "broom", "lily", "Lily"),
    (103, "Product Photography", "商品摄影", 80, "Melbourne", "cameraService", "mia", "Mia_墨尔本"),
]

SERVICE_IMAGES = {
    101: 11,
    102: 10,
    103: 4,
}

INBOX_NOTIFICATIONS = [
    (
        "system",
        "HeyMarket policy update",
        "嘿市平台规则更新通知",
        "We updated our community guidelines and escrow policy. Tap to read the summary.",
        "我们更新了社区规范与担保交易规则，点击查看摘要。",
        None,
        None,
        True,
    ),
    (
        "system",
        "Welcome to HeyMarket",
        "欢迎加入嘿市",
        "Your account is ready. Start browsing listings in Melbourne!",
        "账号已就绪，快去墨尔本同城好物看看吧！",
        None,
        None,
        True,
    ),
    (
        "order",
        "Your order has shipped",
        "订单已发货",
        "Your order has shipped — tap for details.",
        "订单已发货，点击查看详情。",
        "orders",
        None,
        True,
    ),
    (
        "follow",
        "Following updates",
        "关注上新",
        "A seller you follow listed 3 new items.",
        "你关注的卖家上新了 3 件好物。",
        "following",
        None,
        False,
    ),
]

_AREA_ZH = {
    "Clayton": "克莱顿",
    "Melbourne CBD": "墨尔本市中心",
    "Carlton": "卡尔顿",
    "Box Hill": "博士山",
    "Burwood": "布林伍德",
    "Southbank": "南岸",
    "Online": "线上",
    "Docklands": "码头区",
    "Richmond": "里士满",
    "Glen Waverley": "格伦韦弗利",
    "Doncaster": "唐卡斯特",
    "Melbourne East": "墨尔本东区",
    "Monash": "莫纳什",
    "Melbourne": "墨尔本",
    "Clayton / Box Hill": "克莱顿 / 博士山",
    "Melbourne CBD / Southbank": "墨尔本市中心 / 南岸",
}


def _loc_zh(label: str) -> str:
    return _AREA_ZH.get(label, label)


_PLATFORM_CATEGORIES = [
    ("product", "digital", "Digital", "数码"),
    ("product", "home", "Home", "家居"),
    ("product", "misc", "Misc", "其他"),
    ("product", "tickets", "Tickets", "票务"),
    ("service", "services", "Services", "服务"),
    ("job", "jobs", "Jobs", "招聘"),
    ("rental", "rentals", "Rentals", "租赁"),
]


def _seed_platform_config(db: Session) -> None:
    if not db.query(PlatformCategory).first():
        for idx, (ctype, key, label_en, label_zh) in enumerate(_PLATFORM_CATEGORIES):
            db.add(
                PlatformCategory(
                    type=ctype,
                    key=key,
                    label_en=label_en,
                    label_zh=label_zh,
                    sort_order=idx,
                    enabled=True,
                )
            )
    if not db.query(PlatformRegion).first():
        sort = 0
        for region in REGION_DATA:
            for city in region.cities:
                db.add(
                    PlatformRegion(
                        country="AU",
                        state=region.state,
                        city=city.name,
                        area=None,
                        label_en=city.name,
                        label_zh=city.cn,
                        is_default_city=city.name == "Melbourne",
                        sort_order=sort,
                        enabled=True,
                    )
                )
                sort += 1
                for area in city.areas:
                    db.add(
                        PlatformRegion(
                            country="AU",
                            state=region.state,
                            city=city.name,
                            area=area,
                            label_en=area,
                            label_zh=area,
                            is_default_city=False,
                            sort_order=sort,
                            enabled=True,
                        )
                    )
                    sort += 1
    db.flush()


def _ensure_admin_user(db: Session) -> None:
    phone = normalize_phone(settings.admin_seed_phone)
    admin = db.query(User).filter(User.phone == phone).first()
    if admin:
        if not admin.is_admin:
            admin.is_admin = True
        return
    admin_id = "admin-seed"
    db.add(
        User(
            id=admin_id,
            nickname="Admin",
            phone=phone,
            password_hash=hash_password(settings.admin_seed_password),
            heishi_id="HSADMIN001",
            city="Melbourne",
            is_admin=True,
            phone_verified=True,
        )
    )
    db.flush()
    db.add(UserSettings(user_id=admin_id))


def _sync_demo_review_status(db: Session) -> None:
    demo_ids = [pid for pid, *_ in PRODUCTS] + [sid for sid, *_ in SERVICES] + [200]
    for listing in db.query(Listing).filter(Listing.id.in_(demo_ids)).all():
        if listing.review_status != "approved":
            listing.review_status = "approved"


def seed(db: Session) -> None:
    seed_blocked_keywords(db)
    _ensure_admin_user(db)
    _seed_platform_config(db)
    if db.query(Listing).first():
        _sync_listing_translations(db)
        _sync_user_avatars(db)
        _sync_demo_review_status(db)
        demo = db.query(User).filter(User.id == "12345678").first()
        if demo:
            _seed_inbox_notifications(db, demo.id)
            issue_welcome_coupon(db, demo.id, demo.language)
            issue_referral_coupon(db, demo.id, demo.language)
        db.commit()
        return

    sellers: dict[str, User] = {}

    def get_seller(key: str, nickname: str) -> User:
        if key in sellers:
            return sellers[key]
        user = User(
            id=key if len(key) > 8 else f"seller-{key}",
            nickname=nickname,
            phone=f"04{abs(hash(key)) % 100000000:08d}"[:10],
            password_hash=hash_password("demo123"),
            heishi_id=f"HS{abs(hash(key)) % 100000000:08d}"[:10],
            city="Melbourne",
            avatar_url=avatar_url_for_user_id(key if len(key) > 8 else f"seller-{key}"),
        )
        db.add(user)
        db.flush()
        sellers[key] = user
        db.add(UserSettings(user_id=user.id))
        return user

    for pid, title, title_zh, price, cat, tag, loc, skey, snick in PRODUCTS:
        seller = get_seller(skey, snick)
        img = IMAGES.get(pid, IMAGES[1])
        listing_type = "service" if cat == "services" else "product"
        listing = Listing(
            id=pid,
            seller_id=seller.id,
            type=listing_type,
            title=title,
            title_zh=title_zh,
            description=f"Quality {title} available in {loc}. Meetup or post available.",
            description_zh=f"{title_zh}，{_loc_zh(loc)} 面交或邮寄。",
            price=price,
            category_key=cat,
            tag_key=tag,
            condition_key=tag if cat != "tickets" else None,
            location_label=loc,
            region_state="VIC",
            region_city="Melbourne",
            region_area=loc if loc != "Melbourne CBD" else "Melbourne CBD",
            image_url=img,
            status="active",
            review_status="approved",
            negotiable=True,
            escrow_supported=True,
            view_count=10 + pid * 3,
            favorite_count=pid % 5,
        )
        extras = [IMAGES.get(((pid + offset - 1) % 12) + 1, img) for offset in (1, 2)]
        listing.images = list(dict.fromkeys([img, *extras]))
        db.add(listing)

    bundle_seller = get_seller("amy", "Amy")
    bundle_listing = Listing(
        id=200,
        seller_id=bundle_seller.id,
        type="bundle",
        title="Clayton 2BR whole-home clearance",
        title_zh="克莱顿两房整屋清仓",
        description="Near Monash, pickup by Jun 28. Buy separately or as a bundle.",
        description_zh="莫纳什附近，6月28日前自提。可整包也可单买。",
        price=260,
        category_key="home",
        tag_key="bundleSet",
        condition_key="lightlyUsed",
        location_label="Clayton",
        region_state="VIC",
        region_city="Melbourne",
        region_area="Clayton",
        image_url=IMAGES[5],
        status="active",
        review_status="approved",
        negotiable=True,
        escrow_supported=True,
        view_count=48,
        favorite_count=12,
    )
    bundle_listing.images = [IMAGES[0]]
    bundle_listing.pickup_methods = ["meetup"]
    bundle_listing.bundle_meta = {
        "fullPrice": 260,
        "pickupDeadline": "2026-06-28",
        "allowSeparateSale": True,
        "pickupWindow": "weekdayEvening",
        "totalItems": 4,
        "coverImageUrls": [IMAGES[0]],
        "items": [
            {"id": "desk", "title": "Nordic folding desk", "sharePrice": 35, "separatePrice": 35, "imageUrl": IMAGES[5], "status": "available"},
            {"id": "microwave", "title": "Microwave", "sharePrice": 45, "imageUrl": IMAGES[2], "status": "onHold"},
            {"id": "chairs", "title": "Dining chairs (x2)", "sharePrice": 20, "separatePrice": 20, "imageUrl": IMAGES[10], "status": "available"},
            {"id": "bedFrame", "title": "Queen bed frame", "sharePrice": 40, "imageUrl": IMAGES[4], "status": "sold"},
        ],
    }
    db.add(bundle_listing)

    for sid, title, title_zh, price, area, icon, skey, snick in SERVICES:
        seller = get_seller(skey, snick)
        img = IMAGES.get(SERVICE_IMAGES.get(sid, 7), IMAGES[7])
        listing = Listing(
            id=sid,
            seller_id=seller.id,
            type="service",
            title=title,
            title_zh=title_zh,
            description=f"Professional {title} in {area}.",
            description_zh=f"专业{title_zh}，服务区域：{_loc_zh(area)}。",
            price=price,
            category_key="services",
            tag_key="localService",
            location_label=area,
            region_state="VIC",
            region_city="Melbourne",
            region_area="Clayton",
            image_url=img,
            status="active",
            review_status="approved",
            service_icon=icon,
        )
        listing.images = [img]
        db.add(listing)

    demo = User(
        id="12345678",
        nickname="Holden",
        phone=normalize_phone("0400000000"),
        password_hash=hash_password("demo123"),
        heishi_id="HS12345678",
        city="Melbourne",
        language="en",
        avatar_url=avatar_url_for_user_id("12345678"),
    )
    db.add(demo)
    db.flush()
    db.add(UserSettings(user_id=demo.id))
    db.add(PaymentMethod(user_id=demo.id, type="card", label="Visa •••• 4242", last4="4242", is_default=True))
    db.add(PaymentMethod(user_id=demo.id, type="apple_pay", label="Apple Pay", is_default=False))
    db.add(Coupon(
        user_id=demo.id,
        amount=5,
        description="Welcome coupon A$5 off",
        kind="welcome",
        status="available",
    ))
    db.add(Coupon(
        user_id=demo.id,
        amount=10,
        description="Referral bonus A$10",
        kind="referral",
        status="available",
    ))
    _seed_inbox_notifications(db, demo.id)
    db.commit()


def _seed_inbox_notifications(db: Session, user_id: str) -> None:
    existing = db.query(SystemNotification).filter(SystemNotification.user_id == user_id).count()
    if existing >= len(INBOX_NOTIFICATIONS):
        return
    if existing:
        db.query(SystemNotification).filter(SystemNotification.user_id == user_id).delete()
    for cat, title, title_zh, body, body_zh, action_type, action_ref, unread in INBOX_NOTIFICATIONS:
        db.add(
            SystemNotification(
                user_id=user_id,
                category=cat,
                title=title,
                title_zh=title_zh,
                body=body,
                body_zh=body_zh,
                action_type=action_type,
                action_ref=action_ref,
                unread=unread,
            )
        )


def _sync_user_avatars(db: Session) -> None:
    """Apply portrait URLs to demo users when seed data changes (existing DB)."""
    for user in db.query(User).all():
        url = avatar_url_for_user_id(user.id)
        if url and not user.avatar_url:
            user.avatar_url = url
    db.commit()


def _sync_listing_translations(db: Session) -> None:
    """Refresh demo listing Chinese titles when seed data changes (existing DB)."""
    for pid, title, title_zh, _price, _cat, tag, loc, _skey, _snick in PRODUCTS:
        listing = db.query(Listing).filter(Listing.id == pid).first()
        if not listing:
            continue
        listing.title = title
        listing.title_zh = title_zh
        listing.tag_key = tag
        listing.description_zh = f"{title_zh}，{_loc_zh(loc)} 面交或邮寄。"
    for sid, title, title_zh, _price, area, _icon, _skey, _snick in SERVICES:
        listing = db.query(Listing).filter(Listing.id == sid).first()
        if not listing:
            continue
        listing.title = title
        listing.title_zh = title_zh
        listing.description_zh = f"专业{title_zh}，服务区域：{_loc_zh(area)}。"
    db.commit()
