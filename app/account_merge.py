"""Safety checks shared by self-service and administrator account merges."""

from sqlalchemy.orm import Session

from app.models import (
    AdminConversation,
    AdminSupportMessage,
    Address,
    AnonymousSession,
    BlocklistEntry,
    Conversation,
    Coupon,
    DailyActiveUserHit,
    DevicePushToken,
    Favorite,
    Follow,
    FollowedCategory,
    Listing,
    LoginAuditLog,
    MediaAsset,
    Message,
    NotificationDispatch,
    NotificationPreference,
    Order,
    PaymentMethod,
    PayoutMethod,
    PendingAction,
    PrivateOffer,
    PromotionClickEvent,
    Review,
    SafetyReport,
    SearchLog,
    ShareAttributionEvent,
    ShareRecord,
    SystemNotification,
    VerificationSubmission,
    ViewHistory,
    UploadSession,
    UserSettings,
)


def historical_merge_conflicts(
    db: Session,
    *,
    source_user_id: str,
    destination_user_id: str,
) -> list[str]:
    """Identify counterpart relationships that would become invalid self-trades."""
    conflicts: list[str] = []
    if db.query(Order.id).filter(
        ((Order.buyer_id == source_user_id) & (Order.seller_id == destination_user_id))
        | ((Order.buyer_id == destination_user_id) & (Order.seller_id == source_user_id))
    ).first():
        conflicts.append("order_counterpart")
    if db.query(Conversation.id).filter(
        ((Conversation.buyer_id == source_user_id) & (Conversation.seller_id == destination_user_id))
        | ((Conversation.buyer_id == destination_user_id) & (Conversation.seller_id == source_user_id))
    ).first():
        conflicts.append("conversation_counterpart")
    if db.query(PrivateOffer.id).filter(
        ((PrivateOffer.buyer_id == source_user_id) & (PrivateOffer.seller_id == destination_user_id))
        | ((PrivateOffer.buyer_id == destination_user_id) & (PrivateOffer.seller_id == source_user_id))
    ).first():
        conflicts.append("offer_counterpart")
    return conflicts


def migrate_historical_account_state(
    db: Session,
    *,
    source_user_id: str,
    destination_user_id: str,
) -> dict[str, int]:
    """Move all non-authentication state during an administrator-authorized merge.

    Self-service merges deliberately remain restricted to empty duplicates.  This
    helper is for the audited administrator path and must run in the caller's one
    database transaction.  Rows with user-scoped uniqueness are deduplicated;
    historical financial and communication rows are reassigned, never deleted.
    """
    moved: dict[str, int] = {}

    def move(model, field: str) -> None:
        count = (
            db.query(model)
            .filter(getattr(model, field) == source_user_id)
            .update({field: destination_user_id}, synchronize_session=False)
        )
        if count:
            moved[f"{model.__tablename__}.{field}"] = count

    def dedupe_single_key(model, field: str, key_field: str) -> None:
        rows = db.query(model).filter(getattr(model, field) == source_user_id).all()
        count = 0
        for row in rows:
            existing = (
                db.query(model)
                .filter(
                    getattr(model, field) == destination_user_id,
                    getattr(model, key_field) == getattr(row, key_field),
                )
                .first()
            )
            if existing:
                if model is Favorite:
                    listing = db.query(Listing).filter(Listing.id == row.listing_id).first()
                    if listing and listing.favorite_count > 0:
                        listing.favorite_count -= 1
                db.delete(row)
            else:
                setattr(row, field, destination_user_id)
            count += 1
        if count:
            moved[f"{model.__tablename__}.{field}"] = count

    # Rows with no user-scoped uniqueness conflict.
    for model, field in (
        (Listing, "seller_id"),
        (MediaAsset, "owner_id"),
        (UploadSession, "owner_id"),
        (Order, "buyer_id"),
        (Order, "seller_id"),
        (Conversation, "buyer_id"),
        (Conversation, "seller_id"),
        (Message, "sender_id"),
        (PrivateOffer, "buyer_id"),
        (PrivateOffer, "seller_id"),
        (AdminConversation, "user_id"),
        (AdminSupportMessage, "sender_id"),
        (Address, "user_id"),
        (PaymentMethod, "user_id"),
        (PayoutMethod, "user_id"),
        (Coupon, "user_id"),
        (SafetyReport, "reporter_id"),
        (SystemNotification, "user_id"),
        (NotificationDispatch, "user_id"),
        (VerificationSubmission, "user_id"),
        (PromotionClickEvent, "user_id"),
        (ShareRecord, "sharer_user_id"),
        (ShareAttributionEvent, "user_id"),
        (AnonymousSession, "linked_user_id"),
        (PendingAction, "user_id"),
        (SearchLog, "user_id"),
        (LoginAuditLog, "user_id"),
    ):
        move(model, field)

    # Retired-account devices must authenticate again.  Moving their push tokens
    # would continue delivering private destination-account notifications after
    # the corresponding sessions were revoked.
    removed_push_tokens = db.query(DevicePushToken).filter(
        DevicePushToken.user_id == source_user_id
    ).delete(synchronize_session=False)
    if removed_push_tokens:
        moved["device_push_tokens.revoked"] = removed_push_tokens

    # User-scoped unique rows: the keeper's explicit state wins on collision.
    dedupe_single_key(Favorite, "user_id", "listing_id")
    dedupe_single_key(ViewHistory, "user_id", "listing_id")
    dedupe_single_key(FollowedCategory, "user_id", "category_key")
    dedupe_single_key(DailyActiveUserHit, "user_id", "day")

    for row in db.query(NotificationPreference).filter(
        NotificationPreference.user_id == source_user_id
    ).all():
        existing = db.query(NotificationPreference).filter(
            NotificationPreference.user_id == destination_user_id,
            NotificationPreference.user_role_context == row.user_role_context,
            NotificationPreference.category == row.category,
        ).first()
        if existing:
            db.delete(row)
        else:
            row.user_id = destination_user_id
        moved["notification_preferences.user_id"] = moved.get(
            "notification_preferences.user_id", 0
        ) + 1

    source_settings = db.query(UserSettings).filter(
        UserSettings.user_id == source_user_id
    ).first()
    destination_settings = db.query(UserSettings).filter(
        UserSettings.user_id == destination_user_id
    ).first()
    if source_settings:
        if destination_settings:
            db.delete(source_settings)
        else:
            source_settings.user_id = destination_user_id
        moved["user_settings.user_id"] = 1

    for row in db.query(Review).filter(Review.reviewer_id == source_user_id).all():
        existing = db.query(Review).filter(
            Review.order_id == row.order_id,
            Review.reviewer_id == destination_user_id,
        ).first()
        if existing:
            db.delete(row)
        else:
            row.reviewer_id = destination_user_id
        moved["reviews.reviewer_id"] = moved.get("reviews.reviewer_id", 0) + 1

    # Relationship pairs require collision and self-reference handling.
    for row in db.query(Follow).filter(
        (Follow.follower_id == source_user_id) | (Follow.followed_id == source_user_id)
    ).all():
        follower_id = destination_user_id if row.follower_id == source_user_id else row.follower_id
        followed_id = destination_user_id if row.followed_id == source_user_id else row.followed_id
        duplicate = db.query(Follow).filter(
            Follow.id != row.id,
            Follow.follower_id == follower_id,
            Follow.followed_id == followed_id,
        ).first()
        if follower_id == followed_id or duplicate:
            db.delete(row)
        else:
            row.follower_id, row.followed_id = follower_id, followed_id
        moved["follows"] = moved.get("follows", 0) + 1

    for row in db.query(BlocklistEntry).filter(
        (BlocklistEntry.blocker_id == source_user_id)
        | (BlocklistEntry.blocked_id == source_user_id)
    ).all():
        blocker_id = destination_user_id if row.blocker_id == source_user_id else row.blocker_id
        blocked_id = destination_user_id if row.blocked_id == source_user_id else row.blocked_id
        duplicate = db.query(BlocklistEntry).filter(
            BlocklistEntry.id != row.id,
            BlocklistEntry.blocker_id == blocker_id,
            BlocklistEntry.blocked_id == blocked_id,
        ).first()
        if blocker_id == blocked_id or duplicate:
            db.delete(row)
        else:
            row.blocker_id, row.blocked_id = blocker_id, blocked_id
        moved["blocklist"] = moved.get("blocklist", 0) + 1

    # Safety reports store their target polymorphically rather than with a FK.
    moved_targets = db.query(SafetyReport).filter(
        SafetyReport.target_type == "user",
        SafetyReport.target_id == source_user_id,
    ).update({"target_id": destination_user_id}, synchronize_session=False)
    if moved_targets:
        moved["safety_reports.target_id"] = moved_targets

    db.flush()
    return moved


def account_has_cross_domain_state(db: Session, user_id: str) -> bool:
    """Return whether retiring this account would strand user-visible state.

    Complex state needs a purpose-built transactional migration and deduplication
    policy. Until that exists, rejecting the merge is safer than silently leaving
    preferences, support history, notifications, or attribution on an inaccessible
    source account.
    """
    return any(
        (
            db.query(NotificationPreference.id)
            .filter(NotificationPreference.user_id == user_id)
            .first(),
            db.query(SystemNotification.id)
            .filter(SystemNotification.user_id == user_id)
            .first(),
            db.query(NotificationDispatch.id)
            .filter(NotificationDispatch.user_id == user_id)
            .first(),
            db.query(AdminConversation.id)
            .filter(AdminConversation.user_id == user_id)
            .first(),
            db.query(AdminSupportMessage.id)
            .filter(AdminSupportMessage.sender_id == user_id)
            .first(),
            db.query(AnonymousSession.id)
            .filter(AnonymousSession.linked_user_id == user_id)
            .first(),
            db.query(ShareRecord.id)
            .filter(ShareRecord.sharer_user_id == user_id)
            .first(),
            db.query(ShareAttributionEvent.id)
            .filter(ShareAttributionEvent.user_id == user_id)
            .first(),
            db.query(Favorite.id).filter(Favorite.user_id == user_id).first(),
            db.query(ViewHistory.id).filter(ViewHistory.user_id == user_id).first(),
            db.query(Follow.id)
            .filter((Follow.follower_id == user_id) | (Follow.followed_id == user_id))
            .first(),
            db.query(FollowedCategory.id)
            .filter(FollowedCategory.user_id == user_id)
            .first(),
            db.query(PrivateOffer.id)
            .filter(
                (PrivateOffer.buyer_id == user_id)
                | (PrivateOffer.seller_id == user_id)
            )
            .first(),
            db.query(Address.id).filter(Address.user_id == user_id).first(),
            db.query(PaymentMethod.id)
            .filter(PaymentMethod.user_id == user_id)
            .first(),
            db.query(PayoutMethod.id)
            .filter(PayoutMethod.user_id == user_id)
            .first(),
            db.query(DevicePushToken.id)
            .filter(DevicePushToken.user_id == user_id)
            .first(),
            db.query(SafetyReport.id)
            .filter(SafetyReport.reporter_id == user_id)
            .first(),
            db.query(BlocklistEntry.id)
            .filter(
                (BlocklistEntry.blocker_id == user_id)
                | (BlocklistEntry.blocked_id == user_id)
            )
            .first(),
            db.query(Review.id).filter(Review.reviewer_id == user_id).first(),
            db.query(VerificationSubmission.id)
            .filter(VerificationSubmission.user_id == user_id)
            .first(),
            db.query(PromotionClickEvent.id)
            .filter(PromotionClickEvent.user_id == user_id)
            .first(),
            db.query(PendingAction.id)
            .filter(PendingAction.user_id == user_id)
            .first(),
            db.query(SearchLog.id).filter(SearchLog.user_id == user_id).first(),
        )
    )
