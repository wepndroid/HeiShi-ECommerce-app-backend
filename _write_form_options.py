from pathlib import Path
Path("app/form_options.py").write_text("""# -*- coding: utf-8 -*-
\"\"\"Listing / checkout form dropdown options served to the mobile app.\"\"\"

from app.schemas import FormOptionDto, ListingFormOptionsDto

_CATEGORIES = [
    FormOptionDto(key=\"digital\", labelEn=\"Used digital\", labelZh=\"\u4e8c\u624b\u6570\u7801\"),
    FormOptionDto(key=\"home\", labelEn=\"Home goods\", labelZh=\"\u5bb6\u5c45\u65e5\u7528\"),
    FormOptionDto(key=\"fashion\", labelEn=\"Fashion & bags\", labelZh=\"\u670d\u9970\u7bb1\u5305\"),
    FormOptionDto(key=\"beauty\", labelEn=\"Beauty & care\", labelZh=\"\u7f8e\u5986\u4e2a\u62a4\"),
    FormOptionDto(key=\"misc\", labelEn=\"Misc items\", labelZh=\"\u5176\u4ed6\u597d\u7269\"),
    FormOptionDto(key=\"tickets\", labelEn=\"Tickets\", labelZh=\"\u7968\u5238\"),
    FormOptionDto(key=\"motorcycle\", labelEn=\"Motorcycle\", labelZh=\"\u6469\u6258\u8f66\"),
    FormOptionDto(key=\"textbooks\", labelEn=\"Textbooks\", labelZh=\"\u6559\u6750\u8d44\u6599\"),
]

_CONDITIONS = [
    FormOptionDto(key=\"brandNew\", labelEn=\"Brand new\", labelZh=\"\u5168\u65b0\"),
    FormOptionDto(key=\"likeNew95\", labelEn=\"95% new\", labelZh=\"95\u65b0\"),
    FormOptionDto(key=\"likeNew90\", labelEn=\"90% new\", labelZh=\"90\u65b0\"),
    FormOptionDto(key=\"lightlyUsed\", labelEn=\"Lightly used\", labelZh=\"\u8f7b\u5fae\u4f7f\u7528\"),
    FormOptionDto(key=\"withFilm\", labelEn=\"Includes film\", labelZh=\"\u542b\u76f8\u7eb8\"),
    FormOptionDto(key=\"foldable\", labelEn=\"Foldable\", labelZh=\"\u53ef\u6298\u53e0\"),
    FormOptionDto(key=\"fullAccessories\", labelEn=\"Full accessories\", labelZh=\"\u914d\u4ef6\u9f50\u5168\"),
]

_PICKUP_METHODS = [
    FormOptionDto(key=\"meetup\", labelEn=\"Local pickup\", labelZh=\"\u540c\u57ce\u81ea\u53d6\"),
    FormOptionDto(key=\"express\", labelEn=\"Express shipping\", labelZh=\"\u5feb\u9012\u90ae\u5bc4\"),
    FormOptionDto(key=\"delivery\", labelEn=\"Delivery available\", labelZh=\"\u53ef\u9001\u8d27\"),
]

_DELIVERY_METHODS = [
    FormOptionDto(key=\"meetup\", labelEn=\"Local pickup\", labelZh=\"\u540c\u57ce\u81ea\u53d6\"),
    FormOptionDto(key=\"express\", labelEn=\"Express shipping\", labelZh=\"\u5feb\u9012\u90ae\u5bc4\"),
]

_SERVICE_TYPES = [
    FormOptionDto(key=\"moving\", labelEn=\"Moving help\", labelZh=\"\u642c\u5bb6\u5e2e\u624b\"),
    FormOptionDto(key=\"cleaning\", labelEn=\"Home cleaning\", labelZh=\"\u5bb6\u5ead\u6e05\u6d01\"),
    FormOptionDto(key=\"photography\", labelEn=\"Product photography\", labelZh=\"\u5546\u54c1\u6444\u5f71\"),
    FormOptionDto(key=\"tutoring\", labelEn=\"Tutoring & coaching\", labelZh=\"\u966a\u7ec3\u8f85\u5bfc\"),
    FormOptionDto(key=\"repair\", labelEn=\"Repair & assembly\", labelZh=\"\u7ef4\u4fee\u7ec4\u88c5\"),
    FormOptionDto(key=\"other\", labelEn=\"Other service\", labelZh=\"\u5176\u4ed6\u670d\u52a1\"),
]

_SERVICE_AREAS = [
    FormOptionDto(key=\"clayton\", labelEn=\"Clayton\", labelZh=\"Clayton\"),
    FormOptionDto(key=\"box_hill\", labelEn=\"Box Hill\", labelZh=\"Box Hill\"),
    FormOptionDto(key=\"melbourne_cbd\", labelEn=\"Melbourne CBD\", labelZh=\"\u58a8\u5c14\u672c CBD\"),
    FormOptionDto(key=\"southbank\", labelEn=\"Southbank\", labelZh=\"Southbank\"),
    FormOptionDto(key=\"carlton\", labelEn=\"Carlton\", labelZh=\"Carlton\"),
    FormOptionDto(key=\"burwood\", labelEn=\"Burwood\", labelZh=\"Burwood\"),
    FormOptionDto(key=\"glen_waverley\", labelEn=\"Glen Waverley\", labelZh=\"Glen Waverley\"),
    FormOptionDto(key=\"online\", labelEn=\"Online / remote\", labelZh=\"\u7ebf\u4e0a\u670d\u52a1\"),
]

_SERVICE_TIME_SLOTS = [
    FormOptionDto(key=\"weekday_evening\", labelEn=\"Weekday evenings\", labelZh=\"\u5de5\u4f5c\u65e5\u665a\u95f4\"),
    FormOptionDto(key=\"weekend\", labelEn=\"Weekends\", labelZh=\"\u5468\u672b\"),
    FormOptionDto(key=\"flexible\", labelEn=\"Flexible schedule\", labelZh=\"\u65f6\u95f4\u7075\u6d3b\"),
    FormOptionDto(key=\"by_appointment\", labelEn=\"By appointment\", labelZh=\"\u9884\u7ea6\u5236\"),
]

LISTING_FORM_OPTIONS = ListingFormOptionsDto(
    categories=_CATEGORIES,
    conditions=_CONDITIONS,
    pickupMethods=_PICKUP_METHODS,
    deliveryMethods=_DELIVERY_METHODS,
    serviceTypes=_SERVICE_TYPES,
    serviceAreas=_SERVICE_AREAS,
    serviceTimeSlots=_SERVICE_TIME_SLOTS,
)
""", encoding=\"utf-8\")
print('written')
