"""Python mirrors of the Postgres enums (POC_02 §3). Generated from the spec; CHK-DB-2 asserts equality."""

from __future__ import annotations

from enum import StrEnum


class ChannelType(StrEnum):
    WHATSAPP = "whatsapp"
    INSTAGRAM_DM = "instagram_dm"
    INSTAGRAM_COMMENT = "instagram_comment"
    FACEBOOK_MESSENGER = "facebook_messenger"
    FACEBOOK_COMMENT = "facebook_comment"
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"
    WEB_WIDGET = "web_widget"
    TELEGRAM = "telegram"
    SLACK = "slack"
    DISCORD = "discord"
    TEAMS = "teams"
    GENERIC_WEBHOOK = "generic_webhook"


class ChannelProvider(StrEnum):
    META_WHATSAPP = "meta_whatsapp"
    META_PAGES = "meta_pages"
    META_INSTAGRAM = "meta_instagram"
    TELEGRAM = "telegram"
    SIMULATOR = "simulator"


class BusinessStatus(StrEnum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    PAUSED = "paused"


class TeamRole(StrEnum):
    OWNER = "owner"
    MANAGER = "manager"
    STAFF = "staff"


class ParticipantRole(StrEnum):
    CUSTOMER = "customer"
    OWNER = "owner"
    TEAM = "team"


class CustomerMode(StrEnum):
    TIERED = "tiered"
    DRAFT_ALL = "draft_all"
    OFF = "off"
    SHADOW = "shadow"  # beta: added in 0009, used from WP-19 (ADR-024)


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageAuthor(StrEnum):
    CUSTOMER = "customer"
    AI = "ai"
    OWNER = "owner"
    TEAM = "team"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    PENDING_APPROVAL = "pending_approval"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNCONFIRMED = "unconfirmed"


class Desk(StrEnum):
    CUSTOMER = "customer"
    OWNER = "owner"
    BUILDER = "builder"
    VOICE = "voice"


class TurnStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnOutcome(StrEnum):
    REPLIED = "replied"
    DRAFTED = "drafted"
    ASKED_OWNER = "asked_owner"
    HANDOFF = "handoff"
    NO_REPLY = "no_reply"
    BLOCKED = "blocked"
    ROUTED_TO_CALL = "routed_to_call"
    FALLBACK = "fallback"


class QueueStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class OutboundStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"
    UNCONFIRMED = "unconfirmed"


class PriorityClass(StrEnum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


class Topic(StrEnum):
    GREETING_SMALLTALK = "greeting_smalltalk"
    HOURS_LOCATION_CONTACT = "hours_location_contact"
    CATALOG_INFO = "catalog_info"
    PRICING = "pricing"
    AVAILABILITY_STOCK = "availability_stock"
    DELIVERY_SHIPPING = "delivery_shipping"
    PAYMENTS = "payments"
    ORDER_STATUS = "order_status"
    BOOKING_APPOINTMENTS = "booking_appointments"
    RETURNS_REFUNDS_CANCELLATION = "returns_refunds_cancellation"
    DISCOUNT_NEGOTIATION = "discount_negotiation"
    COMPLAINT = "complaint"
    CUSTOM_REQUEST = "custom_request"
    HUMAN_REQUEST = "human_request"
    SPAM_ABUSE = "spam_abuse"
    OTHER = "other"


class RiskTier(StrEnum):
    T0 = "t0"
    T1 = "t1"
    T2 = "t2"


class ApprovalKind(StrEnum):
    OUTBOUND_DRAFT = "outbound_draft"
    AUTOMATION_ACTIVATION = "automation_activation"
    AUTOMATION_STEP = "automation_step"
    TOOL_CALL = "tool_call"
    KNOWLEDGE_CONFLICT = "knowledge_conflict"
    CHANGE_CONFIRMATION = "change_confirmation"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OwnerQuestionStatus(StrEnum):
    OPEN = "open"
    ASKED = "asked"
    ANSWERED = "answered"
    RELAYED = "relayed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AnswerScope(StrEnum):
    GLOBAL = "global"
    CUSTOMER = "customer"
    TEMPORARY = "temporary"
    NONE = "none"


class OfferingKind(StrEnum):
    PRODUCT = "product"
    SERVICE = "service"
    MENU_ITEM = "menu_item"
    PACKAGE = "package"
    OTHER = "other"


class StockMode(StrEnum):
    UNTRACKED = "untracked"
    TRACKED = "tracked"
    INTEGRATION = "integration"


class RecordStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    ARCHIVED = "archived"


class ChangeSetOrigin(StrEnum):
    OWNER_COMMAND = "owner_command"
    IMPORT = "import"
    ONBOARDING = "onboarding"
    ADMIN = "admin"
    LEARNING = "learning"
    SEED = "seed"


class ChangeSetStatus(StrEnum):
    DRAFT = "draft"
    APPLIED = "applied"
    REVERTED = "reverted"


class ChangeOp(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ImportStatus(StrEnum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    REVIEW = "review"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ImportItemResolution(StrEnum):
    PENDING = "pending"
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"


class FileKind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    SPREADSHEET = "spreadsheet"
    DOCUMENT = "document"
    AUDIO = "audio"
    OTHER = "other"


class OrderStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentMethod(StrEnum):
    RAZORPAY_LINK = "razorpay_link"
    COD = "cod"


class PaymentStatus(StrEnum):
    UNPAID = "unpaid"
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    COD_PENDING = "cod_pending"
    COD_COLLECTED = "cod_collected"


class PaymentLinkStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AutomationStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AutomationExecutor(StrEnum):
    INSTANT = "instant"
    DURABLE = "durable"
    GUARD = "guard"


class AutomationRunStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SHADOW = "shadow"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    RINGING = "ringing"
    CONNECTING = "connecting"
    ACTIVE = "active"
    ENDED = "ended"
    FAILED = "failed"


class CallPath(StrEnum):
    SIP = "sip"
    WEBRTC = "webrtc"


class CallEventKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    LOCATION = "location"
    SYSTEM = "system"


class CallEventStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    PROCESSED = "processed"
    FAILED = "failed"


class LinkCodeStatus(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class IntegrationProvider(StrEnum):
    RAZORPAY = "razorpay"
    SHOPIFY = "shopify"


class PipelineEventStatus(StrEnum):
    START = "start"
    OK = "ok"
    ERROR = "error"
    SKIP = "skip"


class ModelCallKind(StrEnum):
    """`model_calls.kind` (BETA_02 §3): what a metered call was."""

    CHAT = "chat"
    STRUCTURED = "structured"
    VISION = "vision"
    EMBED = "embed"
    TRANSCRIBE = "transcribe"
    VOICE_STT = "voice_stt"
    VOICE_LLM = "voice_llm"
    VOICE_TTS = "voice_tts"
    VOICE_SESSION = "voice_session"


class Edition(StrEnum):
    OWNER = "owner"
    TEAM = "team"


class CredentialKind(StrEnum):
    """`credential_vault.kind` (BETA_02 §3)."""

    META_BUSINESS_TOKEN = "meta_business_token"
    META_PAGE_TOKEN = "meta_page_token"
    INSTAGRAM_USER_TOKEN = "instagram_user_token"
    TELEGRAM_BOT_TOKEN = "telegram_bot_token"
    WEBHOOK_SECRET = "webhook_secret"
    WHATSAPP_PIN = "whatsapp_pin"
    RAZORPAY_KEYS = "razorpay_keys"
    MCP_OAUTH = "mcp_oauth"
    MCP_STATIC = "mcp_static"
    STORE_API = "store_api"
    SHIPPING_API = "shipping_api"
    GOOGLE_OAUTH = "google_oauth"
    EMAIL_PROVIDER = "email_provider"
    SLACK_BOT = "slack_bot"


class ConnectFlowStatus(StrEnum):
    STARTED = "started"
    AUTHORIZED = "authorized"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class DataRequestKind(StrEnum):
    """A data-subject request (BETA_10 §7)."""

    EXPORT = "export"
    DELETION = "deletion"
    CORRECTION = "correction"


class DataRequestStatus(StrEnum):
    RECEIVED = "received"
    VERIFYING = "verifying"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"


# Postgres type name → Python enum (used by the enum mirror test)
PG_ENUMS: dict[str, type[StrEnum]] = {
    "data_request_kind": DataRequestKind,
    "data_request_status": DataRequestStatus,
    "channel_type": ChannelType,
    "channel_provider": ChannelProvider,
    "business_status": BusinessStatus,
    "team_role": TeamRole,
    "participant_role": ParticipantRole,
    "customer_mode": CustomerMode,
    "connection_status": ConnectionStatus,
    "message_direction": MessageDirection,
    "message_author": MessageAuthor,
    "message_status": MessageStatus,
    "desk": Desk,
    "turn_status": TurnStatus,
    "turn_outcome": TurnOutcome,
    "queue_status": QueueStatus,
    "outbound_status": OutboundStatus,
    "priority_class": PriorityClass,
    "topic": Topic,
    "risk_tier": RiskTier,
    "approval_kind": ApprovalKind,
    "approval_status": ApprovalStatus,
    "owner_question_status": OwnerQuestionStatus,
    "answer_scope": AnswerScope,
    "offering_kind": OfferingKind,
    "stock_mode": StockMode,
    "record_status": RecordStatus,
    "change_set_origin": ChangeSetOrigin,
    "change_set_status": ChangeSetStatus,
    "change_op": ChangeOp,
    "import_status": ImportStatus,
    "import_item_resolution": ImportItemResolution,
    "file_kind": FileKind,
    "order_status": OrderStatus,
    "payment_method": PaymentMethod,
    "payment_status": PaymentStatus,
    "payment_link_status": PaymentLinkStatus,
    "automation_status": AutomationStatus,
    "automation_executor": AutomationExecutor,
    "automation_run_status": AutomationRunStatus,
    "step_status": StepStatus,
    "call_direction": CallDirection,
    "call_status": CallStatus,
    "call_path": CallPath,
    "call_event_kind": CallEventKind,
    "call_event_status": CallEventStatus,
    "link_code_status": LinkCodeStatus,
    "notification_status": NotificationStatus,
    "integration_provider": IntegrationProvider,
    "pipeline_event_status": PipelineEventStatus,
    "model_call_kind": ModelCallKind,
    "edition": Edition,
    "credential_kind": CredentialKind,
    "connect_flow_status": ConnectFlowStatus,
}
