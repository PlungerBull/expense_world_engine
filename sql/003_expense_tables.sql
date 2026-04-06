-- 003: Expense tables
-- expense_bank_accounts, expense_categories, expense_transaction_inbox,
-- expense_transactions, expense_hashtags, expense_transaction_hashtags,
-- expense_reconciliations

CREATE TABLE IF NOT EXISTS expense_bank_accounts (
    id                     uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                uuid NOT NULL REFERENCES users(id),
    name                   text NOT NULL,
    currency_code          text NOT NULL DEFAULT 'PEN' REFERENCES global_currencies(code),
    is_person              boolean NOT NULL DEFAULT false,
    color                  text NOT NULL DEFAULT '#3b82f6',
    current_balance_cents  bigint NOT NULL DEFAULT 0,
    is_archived            boolean NOT NULL DEFAULT false,
    sort_order             integer NOT NULL DEFAULT 0,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    version                integer NOT NULL DEFAULT 1,
    deleted_at             timestamptz,
    UNIQUE (user_id, name, currency_code)
);

CREATE TABLE IF NOT EXISTS expense_categories (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     uuid NOT NULL REFERENCES users(id),
    name        text NOT NULL,
    color       text NOT NULL DEFAULT '#6b7280',
    is_system   boolean NOT NULL DEFAULT false,
    sort_order  integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    version     integer NOT NULL DEFAULT 1,
    deleted_at  timestamptz,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS expense_reconciliations (
    id                       uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                  uuid NOT NULL REFERENCES users(id),
    account_id               uuid NOT NULL REFERENCES expense_bank_accounts(id),
    name                     text NOT NULL,
    date_start               timestamptz,
    date_end                 timestamptz,
    status                   smallint NOT NULL DEFAULT 1,
    beginning_balance_cents  bigint NOT NULL DEFAULT 0,
    ending_balance_cents     bigint NOT NULL DEFAULT 0,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    version                  integer NOT NULL DEFAULT 1,
    deleted_at               timestamptz
);

CREATE TABLE IF NOT EXISTS expense_transaction_inbox (
    id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       uuid NOT NULL REFERENCES users(id),
    title         text,
    description   text,
    amount_cents  bigint,
    date          timestamptz,
    account_id    uuid REFERENCES expense_bank_accounts(id),
    category_id   uuid REFERENCES expense_categories(id),
    exchange_rate numeric DEFAULT 1.0,
    status        smallint NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    version       integer NOT NULL DEFAULT 1,
    deleted_at    timestamptz
);

CREATE TABLE IF NOT EXISTS expense_transactions (
    id                        uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                   uuid NOT NULL REFERENCES users(id),
    title                     text NOT NULL,
    description               text,
    amount_cents              bigint NOT NULL,
    amount_home_cents         bigint,
    transaction_type          smallint NOT NULL,
    transfer_direction        smallint,
    date                      timestamptz NOT NULL DEFAULT now(),
    account_id                uuid NOT NULL REFERENCES expense_bank_accounts(id),
    category_id               uuid NOT NULL REFERENCES expense_categories(id),
    exchange_rate             numeric NOT NULL DEFAULT 1.0,
    cleared                   boolean NOT NULL DEFAULT false,
    transfer_transaction_id   uuid REFERENCES expense_transactions(id),
    parent_transaction_id     uuid REFERENCES expense_transactions(id),
    inbox_id                  uuid REFERENCES expense_transaction_inbox(id),
    reconciliation_id         uuid REFERENCES expense_reconciliations(id),
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    version                   integer NOT NULL DEFAULT 1,
    deleted_at                timestamptz
);

CREATE TABLE IF NOT EXISTS expense_hashtags (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     uuid NOT NULL REFERENCES users(id),
    name        text NOT NULL,
    sort_order  integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    version     integer NOT NULL DEFAULT 1,
    deleted_at  timestamptz,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS expense_transaction_hashtags (
    id                  uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    transaction_id      uuid NOT NULL,
    transaction_source  smallint NOT NULL,
    hashtag_id          uuid NOT NULL REFERENCES expense_hashtags(id),
    user_id             uuid NOT NULL REFERENCES users(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    version             integer NOT NULL DEFAULT 1,
    deleted_at          timestamptz,
    UNIQUE (transaction_id, hashtag_id)
);
