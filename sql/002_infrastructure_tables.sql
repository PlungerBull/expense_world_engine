-- 002: Infrastructure tables
-- users, user_settings, global_currencies, exchange_rates,
-- sync_checkpoints, idempotency_keys, activity_log

-- users: mirrors auth.users
CREATE TABLE IF NOT EXISTS users (
    id              uuid PRIMARY KEY,
    email           text,
    display_name    text,
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- user_settings: app preferences, one row per user
CREATE TABLE IF NOT EXISTS user_settings (
    user_id                     uuid PRIMARY KEY REFERENCES users(id),
    theme                       smallint NOT NULL DEFAULT 1,
    start_of_week               smallint NOT NULL DEFAULT 0,
    main_currency               text NOT NULL DEFAULT 'PEN',
    transaction_sort_preference smallint NOT NULL DEFAULT 1,
    display_timezone            text NOT NULL DEFAULT 'UTC',
    sidebar_show_bank_accounts  boolean NOT NULL DEFAULT true,
    sidebar_show_people         boolean NOT NULL DEFAULT true,
    sidebar_show_categories     boolean NOT NULL DEFAULT true,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now()
);

-- global_currencies: static lookup, never user-edited
CREATE TABLE IF NOT EXISTS global_currencies (
    code    text PRIMARY KEY,
    name    text NOT NULL,
    symbol  text NOT NULL
);

-- Add FK from user_settings.main_currency -> global_currencies.code
ALTER TABLE user_settings
    ADD CONSTRAINT fk_user_settings_main_currency
    FOREIGN KEY (main_currency) REFERENCES global_currencies(code);

-- exchange_rates: append-only, one row per pair per day
CREATE TABLE IF NOT EXISTS exchange_rates (
    id               uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    base_currency    text NOT NULL REFERENCES global_currencies(code),
    target_currency  text NOT NULL REFERENCES global_currencies(code),
    rate             numeric NOT NULL,
    rate_date        date NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (base_currency, target_currency, rate_date)
);

-- sync_checkpoints: tracks each client's sync position
CREATE TABLE IF NOT EXISTS sync_checkpoints (
    id               uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id          uuid NOT NULL REFERENCES users(id),
    client_id        text NOT NULL,
    last_sync_token  text NOT NULL,
    last_sync_at     timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, client_id)
);

-- idempotency_keys: 24h deduplication
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id                 uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    key                text NOT NULL,
    user_id            uuid NOT NULL REFERENCES users(id),
    processed_at       timestamptz NOT NULL,
    response_snapshot  jsonb NOT NULL,
    expires_at         timestamptz NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, key)
);

-- activity_log: immutable audit trail
CREATE TABLE IF NOT EXISTS activity_log (
    id               uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id          uuid NOT NULL REFERENCES users(id),
    resource_type    text NOT NULL,
    resource_id      uuid NOT NULL,
    action           smallint NOT NULL,
    before_snapshot  jsonb,
    after_snapshot   jsonb,
    changed_by       uuid NOT NULL REFERENCES users(id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
