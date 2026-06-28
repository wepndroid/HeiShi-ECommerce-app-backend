from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiErrorBody(BaseModel):
    code: str | None = None
    message: str
    details: dict | None = None


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    page: int
    pageSize: int
    total: int
    hasMore: bool


# Auth
class RegisterRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=50)
    phone: str
    password: str = Field(min_length=6)
    verificationCode: str = Field(min_length=6, max_length=6)
    avatarUrl: str | None = Field(default=None, min_length=1, max_length=500)


class SendRegisterCodeRequest(BaseModel):
    phone: str


class SendRegisterCodeResponse(BaseModel):
    expiresIn: int
    resendAfter: int
    devCode: str | None = None


class LoginRequest(BaseModel):
    phone: str
    password: str


class SyncProfileRequest(BaseModel):
    """Create/update app profile after Supabase phone OTP verification."""

    nickname: str = Field(min_length=1, max_length=50)
    phone: str | None = None
    avatarUrl: str = Field(min_length=1, max_length=500)


class RefreshRequest(BaseModel):
    refreshToken: str


class AuthUserDto(BaseModel):
    id: str
    nickname: str
    phone: str
    avatarUrl: str | None = None
    bio: str | None = None
    city: str | None = None
    language: Literal["en", "zh"] | None = None
    heishiId: str


class AuthTokensDto(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int
    user: AuthUserDto


# Catalog
class SellerDto(BaseModel):
    id: str
    nickname: str
    avatarUrl: str | None = None
    verified: bool | None = None


class ListingSummaryDto(BaseModel):
    id: int
    type: Literal["product", "service", "bundle"]
    title: str
    description: str | None = None
    price: float
    currency: Literal["AUD"] = "AUD"
    categoryKey: str
    tagKey: str
    locationLabel: str
    imageUrl: str
    images: list[str] = Field(default_factory=list)
    seller: SellerDto
    status: Literal["active", "draft", "sold", "inactive"]
    createdAt: str
    favoriteCount: int | None = None


class ListingDetailDto(ListingSummaryDto):
    images: list[str]
    conditionKey: str | None = None
    negotiable: bool | None = None
    escrowSupported: bool | None = None
    pickupMethods: list[str] | None = None
    viewCount: int | None = None
    favoriteCount: int | None = None
    bundleMeta: dict | None = None
    purchaseAvailable: bool = True
    serviceIcon: str | None = None
    meetInPublic: bool | None = None
    escrowFee: float | None = None


class BundleItemRequest(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=120)
    sharePrice: float = Field(gt=0)
    separatePrice: float | None = Field(default=None, gt=0)
    imageUrl: str | None = None
    imageUrls: list[str] | None = None


# Listings
class CreateListingRequest(BaseModel):
    type: Literal["product", "service", "bundle"]
    title: str
    description: str
    price: float
    categoryKey: str
    conditionKey: str | None = None
    tagKey: str | None = None
    locationLabel: str
    regionState: str | None = None
    regionCity: str | None = None
    imageUrls: list[str]
    pickupMethods: list[str] | None = None
    bundleItems: list[BundleItemRequest] | None = None
    pickupDeadline: str | None = None
    allowSeparateSale: bool | None = True
    pickupWindow: str | None = None
    serviceIcon: str | None = None
    escrowSupported: bool | None = None
    negotiable: bool | None = None
    meetInPublic: bool | None = None
    status: Literal["active", "draft"] | None = None


class LocalServiceDto(BaseModel):
    id: int
    title: str
    description: str
    priceFrom: float
    currency: Literal["AUD"] = "AUD"
    area: str
    icon: Literal["truck", "broom", "cameraService"]
    imageUrl: str | None = None
    seller: SellerDto


class SuggestionDto(BaseModel):
    query: str
    listingId: int
    title: str
    subtitle: str
    imageUrl: str | None = None


class ImageSearchResponseDto(BaseModel):
    suggestedQuery: str
    matchCount: int
    items: list[ListingSummaryDto]
    page: int
    pageSize: int
    total: int
    hasMore: bool



class UploadImageResponse(BaseModel):
    url: str
    key: str


# Orders
class CreateOrderRequest(BaseModel):
    listingId: int
    deliveryMethod: str
    paymentMethodId: str | None = None
    bundleItemId: str | None = None
    couponId: str | None = None


class UpdateOrderRequest(BaseModel):
    deliveryMethod: str | None = None
    paymentMethodId: str | None = None
    couponId: str | None = None


class OrderDto(BaseModel):
    id: int
    listingId: int
    listingTitle: str
    listingImageUrl: str
    seller: SellerDto
    buyer: SellerDto | None = None
    status: Literal["pendingPay", "pendingShip", "pendingReceive", "pendingReview", "completed", "cancelled"]
    amount: float
    escrowFee: float
    currency: Literal["AUD"] = "AUD"
    deliveryMethod: str | None = None
    paymentMethodId: str | None = None
    bundleItemId: str | None = None
    couponId: str | None = None
    discountAmount: float | None = None
    createdAt: str
    updatedAt: str


class ReviewRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = None


class OrderReviewDto(BaseModel):
    rating: int
    comment: str | None = None
    createdAt: str


# User library
class FavoriteDto(BaseModel):
    listingId: int
    createdAt: str


class ViewHistoryItemDto(BaseModel):
    listingId: int
    viewedAt: str


class FollowDto(BaseModel):
    userId: str
    nickname: str
    subtitle: str | None = None
    avatarUrl: str | None = None
    followedAt: str


class CouponDto(BaseModel):
    id: str
    amount: float
    currency: Literal["AUD"] = "AUD"
    description: str
    expiresAt: str | None = None
    status: Literal["available", "used", "expired"]


# Messaging
class CounterpartDto(BaseModel):
    id: str
    nickname: str
    avatarUrl: str | None = None


class ListingRefDto(BaseModel):
    id: int
    title: str
    imageUrl: str | None = None
    price: float | None = None
    locationLabel: str | None = None
    currency: Literal["AUD"] = "AUD"
    status: Literal["active", "draft", "sold", "inactive"] | None = None


class LastMessageDto(BaseModel):
    text: str
    sentAt: str


class ConversationDto(BaseModel):
    id: str
    counterpart: CounterpartDto
    listing: ListingRefDto | None = None
    lastMessage: LastMessageDto | None = None
    unreadCount: int
    markedAsUnread: bool = False


class MarkConversationReadRequest(BaseModel):
    maxMessageId: str | None = None


class MarkConversationUnreadRequest(BaseModel):
    markedAsUnread: bool


class ChatMessageDto(BaseModel):
    id: str
    conversationId: str
    senderId: str
    text: str
    sentAt: str
    ackRead: bool = False


class OpenConversationRequest(BaseModel):
    listingId: int
    counterpartUserId: str | None = None


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SystemNotificationDto(BaseModel):
    id: str
    title: str
    body: str
    createdAt: str
    unread: bool | None = None


NotificationCategory = Literal["system", "order", "follow"]


class InboxNotificationDto(BaseModel):
    id: str
    category: NotificationCategory
    title: str
    body: str
    createdAt: str
    unread: bool
    actionType: str | None = None
    actionRef: str | None = None


class NotificationGroupDto(BaseModel):
    category: NotificationCategory
    unreadCount: int
    previewTitle: str
    previewBody: str
    lastAt: str | None = None


# User profile
class UserProfileUpdateRequest(BaseModel):
    nickname: str | None = None
    bio: str | None = None
    city: str | None = None
    language: Literal["en", "zh"] | None = None
    avatarUrl: str | None = None


class AddressDto(BaseModel):
    id: str
    label: str
    area: str
    meetupSpot: str | None = None
    isDefault: bool | None = None


class AddressCreateRequest(BaseModel):
    label: str
    area: str
    meetupSpot: str | None = None
    isDefault: bool | None = None


class AddressUpdateRequest(BaseModel):
    label: str | None = None
    area: str | None = None
    meetupSpot: str | None = None
    isDefault: bool | None = None


class CreditProfileDto(BaseModel):
    score: int
    trades: int
    completionRate: float
    violations: int
    rating: float


class ReviewSummaryDto(BaseModel):
    score: float
    pendingCount: int


class VerificationStatusDto(BaseModel):
    phoneVerified: bool
    wechatBound: bool
    alipayBound: bool
    identityVerified: bool
    businessVerified: bool


class PublicUserProfileDto(BaseModel):
    """Public seller profile — no phone, payment, or address data."""

    id: str
    nickname: str
    avatarUrl: str | None = None
    bio: str | None = None
    city: str | None = None
    memberSince: str
    rating: float
    reviewCount: int
    listingCount: int
    followerCount: int
    phoneVerified: bool
    identityVerified: bool
    businessVerified: bool
    wechatLinked: bool
    alipayLinked: bool


class PaymentMethodDto(BaseModel):
    id: str
    type: Literal["card", "apple_pay", "paypal"]
    label: str
    last4: str | None = None
    isDefault: bool | None = None


class AddPaymentMethodRequest(BaseModel):
    type: Literal["card", "apple_pay", "paypal"]
    token: str


class PayoutMethodDto(BaseModel):
    id: str
    type: Literal["bank", "paypal"]
    label: str
    last4: str | None = None
    isDefault: bool | None = None


class AddPayoutMethodRequest(BaseModel):
    type: Literal["bank", "paypal"]
    accountToken: str


class NotificationSettingsDto(BaseModel):
    intentAlerts: bool
    chatMessages: bool
    reviewResults: bool
    marketing: bool


class PrivacySettingsDto(BaseModel):
    findByPhone: bool
    showWechatBadge: bool
    personalization: bool


class TransactionReminderSettingsDto(BaseModel):
    payAlerts: bool
    shipAlerts: bool
    receiveAlerts: bool
    disputeAlerts: bool


class CacheClearResponse(BaseModel):
    freedBytes: int


class ChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str


class BindVerificationRequest(BaseModel):
    type: Literal["wechat", "alipay", "identity", "business"]


class SetDefaultMethodRequest(BaseModel):
    isDefault: bool = True


class DataExportDto(BaseModel):
    exportedAt: str
    profile: AuthUserDto
    notificationSettings: NotificationSettingsDto
    privacySettings: PrivacySettingsDto
    transactionReminderSettings: TransactionReminderSettingsDto
    addresses: list[AddressDto]
    verification: VerificationStatusDto


class RegisterPushTokenRequest(BaseModel):
    token: str
    platform: Literal["android", "ios", "web"]


class RemovePushTokenRequest(BaseModel):
    token: str


# Region & safety
class RegionCityDto(BaseModel):
    name: str
    cn: str
    areas: list[str]


class RegionDto(BaseModel):
    state: str
    stateName: str
    cities: list[RegionCityDto]


class ReportSummaryDto(BaseModel):
    id: str
    targetType: str
    status: str
    createdAt: str


class SubmitReportRequest(BaseModel):
    targetType: Literal["listing", "user"]
    targetId: str
    reason: str
    details: str | None = None


class BlocklistUserDto(BaseModel):
    userId: str
    nickname: str


class FormOptionDto(BaseModel):
    key: str
    labelEn: str
    labelZh: str


class ListingFormOptionsDto(BaseModel):
    categories: list[FormOptionDto]
    conditions: list[FormOptionDto]
    pickupMethods: list[FormOptionDto]
    deliveryMethods: list[FormOptionDto]
    serviceTypes: list[FormOptionDto]
    serviceAreas: list[FormOptionDto]
    serviceTimeSlots: list[FormOptionDto]
