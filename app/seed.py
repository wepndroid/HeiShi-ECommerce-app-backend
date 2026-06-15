"""Seed database with demo data matching frontend mock content."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import hash_password, normalize_phone
from app.models import (
    Coupon,
    Listing,
    PaymentMethod,
    SystemNotification,
    User,
    UserSettings,
)

PRODUCTS = [
    (1, "Edifier W820NB Noise-Cancelling Headphones", "漫步者 W820NB 降噪耳机", 89, "digital", "lightlyUsed", "Clayton", "mia", "Mia_墨尔本"),
    (2, "SMEG Style Electric Kettle", "SMEG 风格电水壶", 129, "home", "brandNew", "Melbourne CBD", "sunny", "Sunny"),
    (3, "Keychron K2 Mechanical Keyboard", "Keychron K2 机械键盘", 79, "digital", "likeNew95", "Carlton", "lucas", "Lucas_墨尔本"),
    (4, "Fujifilm instax mini 40", "富士 instax mini 40 拍立得", 95, "digital", "withFilm", "Box Hill", "xiaoyu", "小雨同学"),
    (5, "Nordic Folding Desk", "北欧折叠书桌", 45, "home", "foldable", "Burwood", "amy", "Amy"),
    (6, "Melbourne Concert Ticket", "墨尔本演唱会门票", 68, "tickets", "lowPriceTransfer", "Southbank", "ticketShop", "票券小铺"),
    (7, "PTE Speaking Coaching 1-on-1", "PTE口语陪练 1v1", 30, "services", "localService", "Online", "pte", "PTE学长"),
    (8, "Ukulele Starter Kit", "尤克里里初学套装", 55, "misc", "fullAccessories", "Docklands", "luna", "Luna"),
    (9, "Used Bicycle Helmet", "二手自行车头盔", 25, "misc", "safetyGear", "Richmond", "coffee", "咖啡不加糖"),
    (10, "Dyson Supersonic Hair Dryer", "Dyson Supersonic 吹风机", 299, "home", "lightlyUsed", "Glen Waverley", "sunny", "Sunny"),
    (11, "Motorcycle Helmet & Gloves", "摩托车头盔与手套", 120, "misc", "fullAccessories", "Doncaster", "allen", "Allen"),
    (12, "Marketing Textbooks Bundle", "Marketing 教材资料", 40, "misc", "fullAccessories", "Clayton", "lily", "Lily"),
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


def seed(db: Session) -> None:
    if db.query(Listing).first():
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
            description_zh=f"{title_zh}，{loc} 面交或邮寄。",
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
            negotiable=True,
            escrow_supported=True,
            view_count=10 + pid * 3,
            favorite_count=pid % 5,
        )
        listing.images = [img]
        db.add(listing)

    for sid, title, title_zh, price, area, icon, skey, snick in SERVICES:
        seller = get_seller(skey, snick)
        listing = Listing(
            id=sid,
            seller_id=seller.id,
            type="service",
            title=title,
            title_zh=title_zh,
            description=f"Professional {title} in {area}.",
            description_zh=f"专业{title_zh}，服务区域：{area}。",
            price=price,
            category_key="services",
            tag_key="localService",
            location_label=area,
            region_state="VIC",
            region_city="Melbourne",
            region_area="Clayton",
            image_url=IMAGES[7],
            status="active",
            service_icon=icon,
        )
        listing.images = [IMAGES[7]]
        db.add(listing)

    demo = User(
        id="12345678",
        nickname="Holden",
        phone=normalize_phone("0400000000"),
        password_hash=hash_password("demo123"),
        heishi_id="HS12345678",
        city="Melbourne",
        language="en",
    )
    db.add(demo)
    db.flush()
    db.add(UserSettings(user_id=demo.id))
    db.add(PaymentMethod(user_id=demo.id, type="card", label="Visa •••• 4242", last4="4242", is_default=True))
    db.add(PaymentMethod(user_id=demo.id, type="apple_pay", label="Apple Pay", is_default=False))
    db.add(Coupon(user_id=demo.id, amount=5, description="Welcome coupon A$5 off", status="available"))
    db.add(Coupon(user_id=demo.id, amount=10, description="Referral bonus A$10", status="available"))
    db.add(
        SystemNotification(
            user_id=demo.id,
            title="Welcome to HeyMarket",
            body="Your account is ready. Start browsing listings in Melbourne!",
            unread=True,
        )
    )
    db.commit()
