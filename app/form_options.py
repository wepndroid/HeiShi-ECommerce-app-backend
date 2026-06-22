"""Listing form dropdown options served to the mobile app."""

from app.schemas import FormOptionDto, ListingFormOptionsDto


def _opt(key, en, zh):
    return FormOptionDto(key=key, labelEn=en, labelZh=zh)


LISTING_FORM_OPTIONS = ListingFormOptionsDto(
    categories=[
        _opt("digital", "Used digital", "二手数码"),
        _opt("home", "Home goods", "家居日用"),
        _opt("fashion", "Fashion & bags", "服饰箱包"),
        _opt("beauty", "Beauty & care", "美妆个护"),
        _opt("misc", "Misc items", "其他好物"),
        _opt("tickets", "Tickets", "票券"),
        _opt("motorcycle", "Motorcycle", "摩托车"),
        _opt("textbooks", "Textbooks", "教材资料"),
    ],
    conditions=[
        _opt("brandNew", "Brand new", "全新"),
        _opt("likeNew95", "95% new", "95新"),
        _opt("likeNew90", "90% new", "90新"),
        _opt("lightlyUsed", "Lightly used", "轻微使用"),
        _opt("withFilm", "Includes film", "含相纸"),
        _opt("foldable", "Foldable", "可折叠"),
        _opt("fullAccessories", "Full accessories", "配件齐全"),
    ],
    pickupMethods=[
        _opt("meetup", "Local pickup", "同城自取"),
        _opt("express", "Express shipping", "快递邮寄"),
        _opt("delivery", "Delivery available", "可送货"),
    ],
    deliveryMethods=[
        _opt("meetup", "Local pickup", "同城自取"),
        _opt("express", "Express shipping", "快递邮寄"),
    ],
    serviceTypes=[
        _opt("moving", "Moving help", "搬家帮手"),
        _opt("cleaning", "Home cleaning", "家庭清洁"),
        _opt("photography", "Product photography", "商品摄影"),
        _opt("tutoring", "Tutoring & coaching", "陪练辅导"),
        _opt("repair", "Repair & assembly", "维修组装"),
        _opt("other", "Other service", "其他服务"),
    ],
    serviceAreas=[
        _opt("clayton", "Clayton", "克莱顿"),
        _opt("box_hill", "Box Hill", "博士山"),
        _opt("melbourne_cbd", "Melbourne CBD", "墨尔本市中心"),
        _opt("southbank", "Southbank", "南岸"),
        _opt("carlton", "Carlton", "卡尔顿"),
        _opt("burwood", "Burwood", "布林伍德"),
        _opt("glen_waverley", "Glen Waverley", "格伦韦弗利"),
        _opt("online", "Online / remote", "线上服务"),
    ],
    serviceTimeSlots=[
        _opt("weekday_evening", "Weekday evenings", "工作日晚间"),
        _opt("weekends", "Weekends", "周末"),
        _opt("flexible", "Flexible schedule", "时间灵活"),
        _opt("by_appointment", "By appointment", "需预约"),
    ],
)