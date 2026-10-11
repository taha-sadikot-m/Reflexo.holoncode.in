-- 0001 baseline schema, generated from poc/POC_02_DATA_MODEL.md. Do not edit by hand; add new migrations.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TYPE channel_type AS ENUM ('whatsapp','instagram_dm','instagram_comment','facebook_messenger','facebook_comment',
  'email','sms','voice','web_widget','telegram','slack','discord','teams','generic_webhook');
CREATE TYPE channel_provider AS ENUM ('meta_whatsapp','meta_pages','meta_instagram','telegram','simulator');
CREATE TYPE business_status AS ENUM ('onboarding','active','paused');
CREATE TYPE team_role AS ENUM ('owner','manager','staff');
CREATE TYPE participant_role AS ENUM ('customer','owner','team');
CREATE TYPE customer_mode AS ENUM ('tiered','draft_all','off');
CREATE TYPE connection_status AS ENUM ('active','degraded','disconnected');
CREATE TYPE message_direction AS ENUM ('inbound','outbound');
CREATE TYPE message_author AS ENUM ('customer','ai','owner','team','system');
CREATE TYPE message_status AS ENUM ('received','queued','pending_approval','sending','sent','delivered','read','failed','blocked','unconfirmed');
CREATE TYPE desk AS ENUM ('customer','owner','builder','voice');
CREATE TYPE turn_status AS ENUM ('pending','running','superseded','completed','failed','cancelled');
CREATE TYPE turn_outcome AS ENUM ('replied','drafted','asked_owner','handoff','no_reply','blocked','routed_to_call','fallback');
CREATE TYPE queue_status AS ENUM ('pending','running','done','failed','dead');
CREATE TYPE outbound_status AS ENUM ('queued','sending','sent','failed','blocked','unconfirmed');
CREATE TYPE priority_class AS ENUM ('p0','p1','p2','p3');
CREATE TYPE topic AS ENUM ('greeting_smalltalk','hours_location_contact','catalog_info','pricing','availability_stock',
  'delivery_shipping','payments','order_status','booking_appointments','returns_refunds_cancellation',
  'discount_negotiation','complaint','custom_request','human_request','spam_abuse','other');
CREATE TYPE risk_tier AS ENUM ('t0','t1','t2');
CREATE TYPE approval_kind AS ENUM ('outbound_draft','automation_activation','automation_step','tool_call','knowledge_conflict','change_confirmation');
CREATE TYPE approval_status AS ENUM ('pending','approved','rejected','expired','cancelled');
CREATE TYPE owner_question_status AS ENUM ('open','asked','answered','relayed','expired','cancelled');
CREATE TYPE answer_scope AS ENUM ('global','customer','temporary','none');
CREATE TYPE offering_kind AS ENUM ('product','service','menu_item','package','other');
CREATE TYPE stock_mode AS ENUM ('untracked','tracked','integration');
CREATE TYPE record_status AS ENUM ('active','hidden','archived');
CREATE TYPE change_set_origin AS ENUM ('owner_command','import','onboarding','admin','learning','seed');
CREATE TYPE change_set_status AS ENUM ('draft','applied','reverted');
CREATE TYPE change_op AS ENUM ('create','update','delete');
CREATE TYPE import_status AS ENUM ('queued','extracting','review','committed','failed','cancelled');
CREATE TYPE import_item_resolution AS ENUM ('pending','accept','reject','edit');
CREATE TYPE file_kind AS ENUM ('image','pdf','spreadsheet','document','audio','other');
CREATE TYPE order_status AS ENUM ('draft','awaiting_payment','confirmed','preparing','out_for_delivery','delivered','cancelled');
CREATE TYPE payment_method AS ENUM ('razorpay_link','cod');
CREATE TYPE payment_status AS ENUM ('unpaid','pending','paid','failed','refunded','cod_pending','cod_collected');
CREATE TYPE payment_link_status AS ENUM ('created','paid','expired','cancelled','failed');
CREATE TYPE automation_status AS ENUM ('draft','active','paused','archived');
CREATE TYPE automation_executor AS ENUM ('instant','durable');
CREATE TYPE automation_run_status AS ENUM ('running','waiting','completed','failed','cancelled','shadow');
CREATE TYPE step_status AS ENUM ('pending','running','done','failed','skipped');
CREATE TYPE call_direction AS ENUM ('inbound','outbound');
CREATE TYPE call_status AS ENUM ('ringing','connecting','active','ended','failed');
CREATE TYPE call_path AS ENUM ('sip','webrtc');
CREATE TYPE call_event_kind AS ENUM ('text','image','document','audio','location','system');
CREATE TYPE call_event_status AS ENUM ('pending','delivered','processed','failed');
CREATE TYPE link_code_status AS ENUM ('issued','consumed','expired');
CREATE TYPE notification_status AS ENUM ('pending','sent','failed','suppressed');
CREATE TYPE integration_provider AS ENUM ('razorpay','shopify');
CREATE TYPE pipeline_event_status AS ENUM ('start','ok','error','skip');

CREATE TABLE businesses (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  slug text NOT NULL UNIQUE,
  timezone text NOT NULL DEFAULT 'Asia/Kolkata',
  default_language text NOT NULL DEFAULT 'hi-Latn',
  status business_status NOT NULL DEFAULT 'onboarding',
  kill_switch boolean NOT NULL DEFAULT false,
  settings jsonb NOT NULL DEFAULT '{}',     -- model: BusinessSettings (cod_rule, holding_messages, persona, autonomy overrides)
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE team_members (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  display_name text NOT NULL,
  role team_role NOT NULL,
  scopes text[] NOT NULL DEFAULT '{}',
  phone_e164 text,
  status record_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX team_one_owner ON team_members(business_id) WHERE role = 'owner' AND status = 'active';

CREATE TABLE owner_channel_bindings (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  member_id uuid NOT NULL REFERENCES team_members(id) ON DELETE CASCADE,
  channel channel_type NOT NULL,
  external_id text NOT NULL,                -- WhatsApp E.164 / Telegram user id / Messenger PSID
  verified_at timestamptz,
  revoked_at timestamptz,
  is_primary boolean NOT NULL DEFAULT false,
  simulated_only boolean NOT NULL DEFAULT false,   -- Lab owner binding: honoured only for signed simulated inbox events
  created_at timestamptz NOT NULL DEFAULT now()
);
-- why: inbound role decision looks up (channel, external_id) before the business is known
CREATE UNIQUE INDEX bindings_active ON owner_channel_bindings(business_id, channel, external_id) WHERE revoked_at IS NULL;
CREATE INDEX bindings_lookup ON owner_channel_bindings(channel, external_id) WHERE revoked_at IS NULL AND verified_at IS NOT NULL;

CREATE TABLE verification_codes (
  id uuid PRIMARY KEY,
  business_id uuid REFERENCES businesses(id) ON DELETE CASCADE,  -- nullable for pre-business codes
  purpose text NOT NULL CHECK (purpose IN ('binding_code','signup_otp')),
  target text NOT NULL,                     -- member_id for binding codes, phone for OTP
  code_hash text NOT NULL,                  -- sha256(code + server pepper)
  expires_at timestamptz NOT NULL,
  attempts int NOT NULL DEFAULT 0,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE business_counters (
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name text NOT NULL,                       -- 'order_number'
  value bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (business_id, name)
);

CREATE TABLE channel_connections (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  channel channel_type NOT NULL,
  provider channel_provider NOT NULL,
  external_account_id text NOT NULL,        -- phone_number_id / Telegram bot id / Page id / IG account id / 'sim-<slug>'
  display_name text NOT NULL,
  roles participant_role[] NOT NULL DEFAULT '{customer,owner}',
  customer_mode customer_mode NOT NULL DEFAULT 'tiered',
  job_description text,                     -- owner's instruction for this channel ("On Instagram, only sell...")
  credential_ref text,                      -- env var prefix holding tokens (POC); vault id in production
  settings jsonb NOT NULL DEFAULT '{}',     -- model: ConnectionSettings
  status connection_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, external_account_id, channel)
);

CREATE TABLE customers (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  display_name text,
  phone_e164 text,
  email text,
  language text,
  tags text[] NOT NULL DEFAULT '{}',
  memory jsonb NOT NULL DEFAULT '{}',       -- model: CustomerMemory (preferences, sizes, dietary, address hints; each with source+at)
  summary text,
  merged_into uuid REFERENCES customers(id) ON DELETE SET NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX customers_phone ON customers(business_id, phone_e164) WHERE phone_e164 IS NOT NULL;

CREATE TABLE identity_map (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  channel channel_type NOT NULL,
  external_id text NOT NULL,
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  channel_connection_id uuid REFERENCES channel_connections(id) ON DELETE SET NULL,
  display_name text,
  linked_via text NOT NULL DEFAULT 'first_contact'
    CHECK (linked_via IN ('first_contact','link_code','payment','same_wa_user','owner_confirmed')),
  last_inbound_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, channel, external_id)
);
CREATE INDEX identity_customer ON identity_map(customer_id);

CREATE TABLE customer_link_codes (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  code text NOT NULL UNIQUE,                -- 'LINK-7K3Q9' (base32, 5 chars after prefix)
  source_customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  source_channel channel_type NOT NULL,
  target_channel channel_type NOT NULL,     -- 'whatsapp'
  status link_code_status NOT NULL DEFAULT 'issued',
  expires_at timestamptz NOT NULL,
  consumed_by_identity_id uuid REFERENCES identity_map(id) ON DELETE SET NULL,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  channel channel_type NOT NULL,
  channel_connection_id uuid NOT NULL REFERENCES channel_connections(id) ON DELETE CASCADE,
  participant_role participant_role NOT NULL,
  customer_id uuid REFERENCES customers(id) ON DELETE CASCADE,
  member_id uuid REFERENCES team_members(id) ON DELETE CASCADE,
  thread_key text NOT NULL,                 -- per channel: WA user id, TG chat id, PSID, comment thread id
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  human_active_until timestamptz,
  needs_attention boolean NOT NULL DEFAULT false,   -- set on handoff; cleared when a team member replies
  simulated boolean NOT NULL DEFAULT false,         -- created from a simulated inbound event; outbound goes to the simulator transport (POC_03 §10.7)
  last_inbound_at timestamptz,
  last_outbound_at timestamptz,
  option_map jsonb NOT NULL DEFAULT '{}',   -- model: OptionMap; durable fallback for Redis opts:{conversation_id}
  summary text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel_connection_id, thread_key),
  CHECK ((participant_role = 'customer') = (customer_id IS NOT NULL))
);
-- why: owner timeline = all owner conversations of one member, newest first
CREATE INDEX conversations_member ON conversations(business_id, member_id, last_inbound_at DESC) WHERE member_id IS NOT NULL;
CREATE INDEX conversations_customer ON conversations(business_id, customer_id) WHERE customer_id IS NOT NULL;

CREATE TABLE media_objects (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  storage_key text NOT NULL,                -- local volume path in POC (s3 key in production)
  mime text NOT NULL,
  size_bytes bigint NOT NULL,
  sha256 text NOT NULL,
  source text NOT NULL CHECK (source IN ('inbound','owner_upload','generated','seed')),
  provider_media_id text,
  description text,                         -- vision/STT summary, filled by media jobs
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  direction message_direction NOT NULL,
  author message_author NOT NULL,
  channel channel_type NOT NULL,
  channel_connection_id uuid NOT NULL REFERENCES channel_connections(id) ON DELETE CASCADE,
  external_message_id text,
  content_text text,                        -- plain text (inbound text, transcription, or rendered fallback)
  content_blocks jsonb,                     -- model: list[ContentBlock] (outbound)
  attachments jsonb NOT NULL DEFAULT '[]',  -- model: list[AttachmentRef] {media_object_id, kind}
  interactive_reply jsonb,                  -- model: InteractiveReply {option_id, title} for button/list replies
  language text,
  status message_status NOT NULL,
  turn_id uuid,                             -- outbound: producing turn; inbound: consuming turn (set on claim)
  call_session_id uuid,                     -- set when sent/received during a call
  sent_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
  error jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  read_at timestamptz,
  CHECK (content_text IS NOT NULL OR content_blocks IS NOT NULL OR attachments <> '[]'::jsonb)
);
-- why: inbound dedupe at message level (backs up inbox dedupe)
CREATE UNIQUE INDEX messages_external ON messages(channel_connection_id, external_message_id) WHERE external_message_id IS NOT NULL;
CREATE INDEX messages_conversation ON messages(conversation_id, created_at);

CREATE TABLE conversation_turns (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  desk desk NOT NULL,
  priority priority_class NOT NULL,
  status turn_status NOT NULL DEFAULT 'pending',
  outcome turn_outcome,
  message_ids uuid[] NOT NULL,              -- inbound messages this turn answers (grows while pending)
  inherited_turn_ids uuid[] NOT NULL DEFAULT '{}',  -- superseded predecessors whose side effects carry over
  first_message_at timestamptz NOT NULL,
  run_after timestamptz NOT NULL,
  started_at timestamptz,
  finished_at timestamptz,
  lease_owner text,
  lease_until timestamptz,
  attempts int NOT NULL DEFAULT 0,
  superseded_by uuid,
  hints jsonb NOT NULL DEFAULT '{}',        -- model: TurnHints {relay_question_id?, sandbox?: bool}
  trace_context jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- why: D-021 one pending and one running turn per conversation
CREATE UNIQUE INDEX turns_one_pending ON conversation_turns(conversation_id) WHERE status = 'pending';
CREATE UNIQUE INDEX turns_one_running ON conversation_turns(conversation_id) WHERE status = 'running';
-- why: claim query
CREATE INDEX turns_due ON conversation_turns(priority, run_after) WHERE status = 'pending';
CREATE INDEX turns_lease ON conversation_turns(lease_until) WHERE status = 'running';

CREATE TABLE tool_effects (                 -- idempotency ledger for side-effecting tools (POC_04 §3.1)
  idempotency_key text PRIMARY KEY,         -- turn:{turn_id}:{step}:{i} | call:{call_session_id}:{seq}:{i}
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  tool text NOT NULL,
  result jsonb NOT NULL,                    -- the tool's result envelope, returned verbatim on replay
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_runs (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  turn_id uuid,                             -- null for builder/voice/sandbox runs
  call_session_id uuid,
  desk desk NOT NULL,
  prompt_version text NOT NULL,
  model_primary text NOT NULL,
  model_used text,
  hedged boolean NOT NULL DEFAULT false,
  model_calls int NOT NULL DEFAULT 0,
  input_tokens int NOT NULL DEFAULT 0,
  output_tokens int NOT NULL DEFAULT 0,
  latency_ms int,
  latency_breakdown jsonb,                  -- model: LatencyBreakdown {context, model_1, tools, model_2, gate}
  topic topic,
  tier risk_tier,
  grounded boolean,
  gate_result jsonb,                        -- model: GateResult
  outcome turn_outcome,
  error jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE inbox_events (                 -- pre-tenant: no business_id
  id uuid PRIMARY KEY,
  provider channel_provider NOT NULL,
  route_key text,                           -- connection hint from the URL or payload (phone_number_id, page id)
  dedupe_key text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  headers jsonb NOT NULL,                   -- filtered: signature headers + content-type only
  payload jsonb NOT NULL,
  simulated boolean NOT NULL DEFAULT false,
  status queue_status NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  lease_owner text,
  lease_until timestamptz,
  last_error text,
  trace_context jsonb,
  UNIQUE (provider, dedupe_key)
);
CREATE INDEX inbox_due ON inbox_events(received_at) WHERE status = 'pending';

CREATE TABLE outbound_messages (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  channel_connection_id uuid NOT NULL REFERENCES channel_connections(id) ON DELETE CASCADE,
  part_index int NOT NULL DEFAULT 0,
  idempotency_key text NOT NULL UNIQUE,
  rendered_payload jsonb NOT NULL,          -- provider API request body
  priority priority_class NOT NULL,
  status outbound_status NOT NULL DEFAULT 'queued',
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  provider_message_id text,
  lease_owner text,
  lease_until timestamptz,
  last_error text,
  trace_context jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
-- why: strict per-conversation ordering; claim only the oldest unsent part per conversation
CREATE INDEX outbound_due ON outbound_messages(conversation_id, created_at, part_index) WHERE status IN ('queued','sending');

CREATE TABLE jobs (
  id uuid PRIMARY KEY,
  business_id uuid REFERENCES businesses(id) ON DELETE CASCADE,
  queue text NOT NULL,                      -- registry in POC_03 §11.1
  payload jsonb NOT NULL,
  priority priority_class NOT NULL DEFAULT 'p3',
  dedupe_key text,
  run_after timestamptz NOT NULL DEFAULT now(),
  status queue_status NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  lease_owner text,
  lease_until timestamptz,
  last_error text,
  trace_context jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX jobs_dedupe ON jobs(queue, dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending','running');
CREATE INDEX jobs_due ON jobs(priority, run_after) WHERE status = 'pending';

CREATE TABLE domain_events (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  type text NOT NULL,                       -- registry in POC_03 §11.2
  occurred_at timestamptz NOT NULL DEFAULT now(),
  source text NOT NULL,
  customer_id uuid,
  conversation_id uuid,
  correlation_id uuid NOT NULL,
  causation_id uuid,
  causation_depth int NOT NULL DEFAULT 0,
  payload jsonb NOT NULL,
  trace_context jsonb
);
CREATE INDEX domain_events_type ON domain_events(business_id, type, occurred_at DESC);

CREATE TABLE pipeline_events (              -- X-ray feed
  id uuid PRIMARY KEY,
  business_id uuid REFERENCES businesses(id) ON DELETE CASCADE,
  trace_id text NOT NULL,
  stage text NOT NULL,                      -- POC_01 §6 stage list
  status pipeline_event_status NOT NULL,
  duration_ms int,
  conversation_id uuid,
  turn_id uuid,
  attrs jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX pipeline_events_trace ON pipeline_events(trace_id, created_at);
CREATE INDEX pipeline_events_recent ON pipeline_events(created_at DESC);

CREATE TABLE business_profiles (
  business_id uuid PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,
  version int NOT NULL DEFAULT 1,
  profile jsonb NOT NULL,                   -- model: BusinessProfile (identity, contact, hours, delivery, policies, payments, persona)
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_detail_schemas (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name text NOT NULL,                       -- 'Cake order details'
  applies_to jsonb NOT NULL,                -- model: DetailScope {categories:[], offering_ids:[], all:bool}
  fields jsonb NOT NULL,                    -- model: list[DetailField] {key,label,type,required,options,hint}
  source_message_id uuid,
  status record_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE offerings (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  kind offering_kind NOT NULL DEFAULT 'product',
  name text NOT NULL,
  description text,
  category text,
  attributes jsonb NOT NULL DEFAULT '{}',   -- model: OfferingAttributes (eggless_available, flavours, sizes...)
  base_price_paise bigint,                  -- NULL = price on request (owner quote)
  currency char(3) NOT NULL DEFAULT 'INR',
  stock_mode stock_mode NOT NULL DEFAULT 'untracked',
  stock_qty int,
  status record_status NOT NULL DEFAULT 'active',
  aliases text[] NOT NULL DEFAULT '{}',     -- Hinglish/Devanagari/misspellings, generated at import
  image_media_id uuid REFERENCES media_objects(id) ON DELETE SET NULL,
  source text NOT NULL DEFAULT 'owner',
  external_ref text,                        -- Shopify product id
  version int NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX offerings_name_trgm ON offerings USING gin (name gin_trgm_ops);

CREATE TABLE offering_variants (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  offering_id uuid NOT NULL REFERENCES offerings(id) ON DELETE CASCADE,
  name text NOT NULL,                       -- '1 kg', 'Box of 6', 'M / Red'
  sku text,
  option_values jsonb NOT NULL DEFAULT '{}',
  price_paise bigint NOT NULL,
  stock_qty int,
  status record_status NOT NULL DEFAULT 'active',
  external_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX variants_offering ON offering_variants(offering_id);

CREATE TABLE qa_entries (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  question text NOT NULL,
  question_variants text[] NOT NULL DEFAULT '{}',
  answer text NOT NULL,
  scope answer_scope NOT NULL DEFAULT 'global',
  customer_id uuid REFERENCES customers(id) ON DELETE CASCADE,   -- scope='customer'
  valid_until timestamptz,                  -- scope='temporary'
  topic topic,
  source text NOT NULL CHECK (source IN ('owner_chat','onboarding','import','admin','seed')),
  source_message_id uuid,
  status record_status NOT NULL DEFAULT 'active',
  version int NOT NULL DEFAULT 1,
  superseded_by uuid REFERENCES qa_entries(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (scope <> 'customer' OR customer_id IS NOT NULL)
);

CREATE TABLE knowledge_change_sets (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  origin change_set_origin NOT NULL,
  summary text NOT NULL,                    -- human-readable, shown in read-backs and undo
  status change_set_status NOT NULL DEFAULT 'draft',
  created_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
  source_file_id uuid,                      -- owner_files.id when built from a file
  revert_of uuid REFERENCES knowledge_change_sets(id) ON DELETE SET NULL,
  applied_at timestamptz,
  reverted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX change_sets_recent ON knowledge_change_sets(business_id, applied_at DESC) WHERE status = 'applied';

CREATE TABLE knowledge_change_items (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  change_set_id uuid NOT NULL REFERENCES knowledge_change_sets(id) ON DELETE CASCADE,
  entity_type text NOT NULL CHECK (entity_type IN ('offering','variant','qa','profile','detail_schema')),
  entity_id uuid NOT NULL,
  op change_op NOT NULL,
  before jsonb,
  after jsonb
);

CREATE TABLE search_documents (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  entity_type text NOT NULL CHECK (entity_type IN ('offering','qa','profile','owner_file')),
  entity_id uuid NOT NULL,
  title text NOT NULL,                      -- name + aliases (trigram target)
  content text NOT NULL,                    -- description/answer text; never prices or stock (hydrated at read time)
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', title || ' ' || content)) STORED,
  embedding vector(768),                    -- Gemini embedding with output_dimensionality=768
  embedding_model text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_type, entity_id)
);
CREATE INDEX search_tsv ON search_documents USING gin (tsv);
CREATE INDEX search_title_trgm ON search_documents USING gin (title gin_trgm_ops);
CREATE INDEX search_business ON search_documents(business_id, entity_type);
-- no ANN index: exact per-business vector search (small catalogues), as in specs_v2 D-042

CREATE TABLE import_runs (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  source_kind text NOT NULL CHECK (source_kind IN ('image','pdf','excel','csv','text')),
  source_file_id uuid NOT NULL,             -- owner_files.id
  status import_status NOT NULL DEFAULT 'queued',
  stats jsonb NOT NULL DEFAULT '{}',        -- model: ImportStats {found, new, changed, needs_review}
  change_set_id uuid REFERENCES knowledge_change_sets(id) ON DELETE SET NULL,
  workflow_id text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE TABLE import_items (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  import_run_id uuid NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
  extracted jsonb NOT NULL,                 -- model: ExtractedItem {name, variants[{name, price_paise}], category, description, source_text}
  confidence numeric(3,2) NOT NULL,
  issues text[] NOT NULL DEFAULT '{}',      -- 'missing_price','price_not_in_source','possible_duplicate'
  resolution import_item_resolution NOT NULL DEFAULT 'pending',
  target_offering_id uuid REFERENCES offerings(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE owner_files (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
  media_object_id uuid NOT NULL REFERENCES media_objects(id) ON DELETE CASCADE,
  filename text,
  kind file_kind NOT NULL,
  description text,                         -- auto: "Price list, 3 items, Excel" (filled by media.describe job)
  received_via channel_type NOT NULL,
  source_message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
  tags text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
-- why: "the sheet I sent this morning" = latest by kind + time, then text match on description
CREATE INDEX owner_files_recent ON owner_files(business_id, kind, created_at DESC);

CREATE TABLE owner_questions (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,  -- the customer conversation
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  source_turn_id uuid,
  question text NOT NULL,                   -- customer's question, normalized
  ai_guess text,
  options jsonb,                            -- quick replies offered to the owner
  status owner_question_status NOT NULL DEFAULT 'open',
  short_code char(4) NOT NULL,              -- owner can reply "1234 <answer>" on any channel
  asked_at timestamptz,
  asked_via_message_id uuid,
  answer text,
  answered_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
  answer_scope answer_scope,
  answered_at timestamptz,
  relayed_at timestamptz,
  sla_due_at timestamptz NOT NULL,
  qa_entry_id uuid REFERENCES qa_entries(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX owner_questions_code ON owner_questions(business_id, short_code) WHERE status IN ('open','asked');

CREATE TABLE approval_requests (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  kind approval_kind NOT NULL,
  subject jsonb NOT NULL,                   -- model: ApprovalSubject (refs to message/automation/order/change set)
  preview jsonb NOT NULL,                   -- model: list[ContentBlock] shown to the owner
  short_code char(4) NOT NULL,
  status approval_status NOT NULL DEFAULT 'pending',
  on_expire text NOT NULL DEFAULT 'reject' CHECK (on_expire IN ('reject','approve')),
  expires_at timestamptz NOT NULL,
  requested_by text NOT NULL,               -- 'agent:customer', 'automation:<id>', 'builder'
  decided_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
  decided_via channel_type,
  decided_at timestamptz,
  decision jsonb,                           -- edits, reason
  workflow_id text,                         -- Temporal workflow to signal
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX approvals_code ON approval_requests(business_id, short_code) WHERE status = 'pending';

CREATE TABLE owner_notifications (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  member_id uuid REFERENCES team_members(id) ON DELETE CASCADE,   -- NULL = route to owner
  kind text NOT NULL,                       -- 'order_confirmed','question','approval','automation_paused','call_summary'
  priority priority_class NOT NULL DEFAULT 'p1',
  payload jsonb NOT NULL,                   -- model: NotificationPayload (blocks + refs)
  dedupe_key text NOT NULL,
  status notification_status NOT NULL DEFAULT 'pending',
  deliver_after timestamptz NOT NULL DEFAULT now(),
  delivered_via channel_type,
  delivered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, dedupe_key)
);

CREATE TABLE onboarding_sessions (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL UNIQUE REFERENCES businesses(id) ON DELETE CASCADE,
  state text NOT NULL CHECK (state IN ('sources_requested','extracting','gap_questions','preview','live')),
  data jsonb NOT NULL DEFAULT '{}',         -- model: OnboardingData (asked, skipped, preview_qas)
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE TABLE orders (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  order_number text NOT NULL,               -- 'MB-1042' (business_counters)
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE NO ACTION,   -- checked at statement end, so business cascade deletes work
  conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
  status order_status NOT NULL DEFAULT 'draft',
  payment_method payment_method,
  payment_status payment_status NOT NULL DEFAULT 'unpaid',
  subtotal_paise bigint NOT NULL DEFAULT 0,
  delivery_fee_paise bigint NOT NULL DEFAULT 0,
  total_paise bigint NOT NULL DEFAULT 0,
  advance_required_paise bigint NOT NULL DEFAULT 0,
  details jsonb NOT NULL DEFAULT '{}',      -- collected values for order_detail_schemas fields
  missing_details text[] NOT NULL DEFAULT '{}',
  delivery_date date,
  delivery_slot text,
  address text,
  notes text,
  source_channel channel_type,
  external_ref text,                        -- Shopify order id
  created_by text NOT NULL DEFAULT 'ai' CHECK (created_by IN ('ai','owner','voice','shopify','admin')),
  confirmed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, order_number)
);
-- why: daily-capacity automation and "orders today" stats
CREATE INDEX orders_delivery_date ON orders(business_id, delivery_date) WHERE status <> 'cancelled';
CREATE INDEX orders_customer ON orders(business_id, customer_id, created_at DESC);

CREATE TABLE order_items (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  offering_id uuid REFERENCES offerings(id) ON DELETE SET NULL,
  variant_id uuid REFERENCES offering_variants(id) ON DELETE SET NULL,
  name_snapshot text NOT NULL,
  unit_price_paise bigint NOT NULL,         -- copied from the variant row at add time (server-side)
  quantity int NOT NULL CHECK (quantity > 0),
  line_total_paise bigint NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'       -- per-item detail values (e.g. message on cake)
);

CREATE TABLE payment_links (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  provider integration_provider NOT NULL DEFAULT 'razorpay',
  provider_link_id text,
  short_url text,
  amount_paise bigint NOT NULL,             -- computed from order_items (+ fee) or advance; never model-supplied
  purpose text NOT NULL DEFAULT 'full' CHECK (purpose IN ('full','advance','balance')),
  status payment_link_status NOT NULL DEFAULT 'created',
  idempotency_key text NOT NULL UNIQUE,
  expires_at timestamptz,
  provider_payment_id text,
  paid_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX payment_links_provider ON payment_links(provider, provider_link_id) WHERE provider_link_id IS NOT NULL;

CREATE TABLE automations (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name text NOT NULL,
  description_nl text NOT NULL,             -- owner's words
  status automation_status NOT NULL DEFAULT 'draft',
  current_version_id uuid,
  priority int NOT NULL DEFAULT 100,        -- consume-mode arbitration (lower wins)
  created_by_member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE automation_versions (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  automation_id uuid NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
  version int NOT NULL,
  definition jsonb NOT NULL,                -- model: AutomationDefinition (POC_06)
  executor automation_executor NOT NULL,
  trigger_index jsonb NOT NULL,             -- model: TriggerIndex {type, channel, keywords[], post_id}
  readback text NOT NULL,
  validation jsonb NOT NULL,                -- model: ValidationReport
  created_at timestamptz NOT NULL DEFAULT now(),
  activated_at timestamptz,
  UNIQUE (automation_id, version)
);

CREATE TABLE automation_runs (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  automation_version_id uuid NOT NULL REFERENCES automation_versions(id) ON DELETE CASCADE,
  instance_key text NOT NULL,               -- e.g. order id
  trigger_event_id uuid NOT NULL,
  status automation_run_status NOT NULL DEFAULT 'running',
  workflow_id text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error jsonb,
  UNIQUE (automation_version_id, trigger_event_id)       -- one run per event per version
);

CREATE TABLE automation_run_steps (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  run_id uuid NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
  step_id text NOT NULL,
  status step_status NOT NULL DEFAULT 'pending',
  idempotency_key text NOT NULL UNIQUE,     -- auto:{version_id}:{event_id}:{step_id}
  input jsonb,
  output jsonb,
  error jsonb,
  started_at timestamptz,
  finished_at timestamptz
);

CREATE TABLE automation_counters (
  automation_id uuid NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  counter_key text NOT NULL,                -- e.g. 'cakes:2026-10-15'
  value int NOT NULL DEFAULT 0,
  limit_value int NOT NULL,
  PRIMARY KEY (automation_id, counter_key),
  CHECK (value <= limit_value)
);

CREATE TABLE automation_waits (             -- durable runs waiting for an event (POC_06 §7.2)
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  run_id uuid NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
  workflow_id text NOT NULL,
  event_type text NOT NULL,                 -- 'message.received', 'payment.captured', ...
  correlation_key text NOT NULL,            -- instance key (customer id / order id)
  match_cel text NOT NULL,
  consume boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting','matched','expired','cancelled')),
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX automation_waits_lookup ON automation_waits(business_id, event_type, correlation_key) WHERE status = 'waiting';

CREATE TABLE automation_claims (
  automation_id uuid NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  claim_key text NOT NULL,                  -- e.g. customer id for once-per-person
  run_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (automation_id, claim_key)
);

CREATE TABLE call_sessions (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  customer_id uuid REFERENCES customers(id) ON DELETE SET NULL,
  member_id uuid REFERENCES team_members(id) ON DELETE SET NULL,   -- owner calling in
  conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL, -- the WhatsApp chat conversation
  direction call_direction NOT NULL,
  path call_path NOT NULL,
  provider_call_id text,                    -- Meta call id or SIP call id
  livekit_room text NOT NULL,
  bridge_host text,                         -- voice-bridge instance owning the WebRTC peer (restart recovery)
  caller_e164 text NOT NULL,
  status call_status NOT NULL DEFAULT 'ringing',
  started_at timestamptz NOT NULL DEFAULT now(),
  connected_at timestamptz,
  ended_at timestamptz,
  end_reason text,                          -- 'caller_hangup' | 'agent_end' | 'silence' | 'bridge_restart' | 'setup_failed' | 'error'
  summary text,
  transcript jsonb NOT NULL DEFAULT '[]',   -- model: list[TranscriptTurn]
  trace_context jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- why: in-call routing lookup ("is a call active for this customer?") and one live call per customer
CREATE UNIQUE INDEX call_one_live ON call_sessions(business_id, customer_id) WHERE status IN ('ringing','connecting','active');

CREATE TABLE call_events (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  call_session_id uuid NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
  kind call_event_kind NOT NULL,
  message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
  payload jsonb NOT NULL DEFAULT '{}',      -- model: CallEventPayload {text, media_object_id, mime, location}
  status call_event_status NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);
CREATE INDEX call_events_session ON call_events(call_session_id, created_at);

CREATE TABLE integration_connections (
  id uuid PRIMARY KEY,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  provider integration_provider NOT NULL,
  credential_ref text NOT NULL,             -- env var prefix (POC)
  settings jsonb NOT NULL DEFAULT '{}',
  status connection_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, provider)
);

CREATE TABLE integration_events_log (
  id uuid PRIMARY KEY,
  business_id uuid REFERENCES businesses(id) ON DELETE CASCADE,
  provider integration_provider NOT NULL,
  external_event_id text NOT NULL,
  type text NOT NULL,
  payload jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  UNIQUE (provider, external_event_id)
);

CREATE TABLE runtime_flags (                -- global; used by chaos (slow model, kill worker)
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  expires_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chaos_events (
  id uuid PRIMARY KEY,
  business_id uuid REFERENCES businesses(id) ON DELETE CASCADE,
  kind text NOT NULL,                       -- duplicate_webhook|burst|kill_worker|slow_model|prompt_injection|cross_tenant_read
  params jsonb NOT NULL DEFAULT '{}',
  outcome jsonb,                            -- filled by the check that verifies the expected behaviour
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eval_runs (
  id uuid PRIMARY KEY,
  build_ref text NOT NULL,                  -- git sha
  suite text NOT NULL,
  cases int NOT NULL,
  passed int NOT NULL,
  metrics jsonb NOT NULL,
  report jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------- privileges ----------
GRANT USAGE ON SCHEMA public TO reflexo_app, reflexo_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO reflexo_app, reflexo_service;
REVOKE ALL ON alembic_version FROM reflexo_app, reflexo_service;

-- LangGraph checkpointer creates its own tables at worker start (POC_02 §2)
CREATE SCHEMA IF NOT EXISTS langgraph;
GRANT USAGE, CREATE ON SCHEMA langgraph TO reflexo_service;

-- ---------- row-level security (POC_02 §6) ----------
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname,
           EXISTS (SELECT 1 FROM information_schema.columns ic
                   WHERE ic.table_schema = 'public' AND ic.table_name = c.relname AND ic.column_name = 'business_id') AS has_bid
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', r.relname);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', r.relname);
    EXECUTE format('CREATE POLICY %I ON %I TO reflexo_service USING (true) WITH CHECK (true)', r.relname || '_service', r.relname);
    IF r.has_bid THEN
      EXECUTE format('CREATE POLICY %I ON %I TO reflexo_app '
                     'USING (business_id = NULLIF(current_setting(''app.current_business_id'', true), '''')::uuid) '
                     'WITH CHECK (business_id = NULLIF(current_setting(''app.current_business_id'', true), '''')::uuid)',
                     r.relname || '_app', r.relname);
    END IF;
  END LOOP;
END $$;

CREATE POLICY businesses_app ON businesses TO reflexo_app
  USING (id = NULLIF(current_setting('app.current_business_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.current_business_id', true), '')::uuid);
