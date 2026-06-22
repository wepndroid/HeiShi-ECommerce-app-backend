"""Real portrait photo URLs for demo users (Pexels)."""

PERSON_AVATAR_URLS: dict[str, str] = {
    "mia": "https://images.pexels.com/photos/774909/pexels-photo-774909.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "sunny": "https://images.pexels.com/photos/1222271/pexels-photo-1222271.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "lucas": "https://images.pexels.com/photos/220453/pexels-photo-220453.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "xiaoyu": "https://images.pexels.com/photos/12392920/pexels-photo-12392920.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "amy": "https://images.pexels.com/photos/415829/pexels-photo-415829.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "ticketShop": "https://images.pexels.com/photos/91227/pexels-photo-91227.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "pte": "https://images.pexels.com/photos/1681010/pexels-photo-1681010.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "luna": "https://images.pexels.com/photos/1130626/pexels-photo-1130626.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "coffee": "https://images.pexels.com/photos/1462630/pexels-photo-1462630.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "allen": "https://images.pexels.com/photos/1043474/pexels-photo-1043474.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "lily": "https://images.pexels.com/photos/1181686/pexels-photo-1181686.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
    "12345678": "https://images.pexels.com/photos/6457305/pexels-photo-6457305.jpeg?auto=compress&cs=tinysrgb&w=256&h=256&fit=crop",
}


def avatar_url_for_user_id(user_id: str) -> str | None:
    if user_id in PERSON_AVATAR_URLS:
        return PERSON_AVATAR_URLS[user_id]
    if user_id.startswith("seller-"):
        slug = user_id[len("seller-") :]
        return PERSON_AVATAR_URLS.get(slug)
    return None