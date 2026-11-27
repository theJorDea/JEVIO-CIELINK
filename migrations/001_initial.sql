BEGIN;

CREATE TABLE sessions (
    session_id text PRIMARY KEY,
    channel text NOT NULL CHECK (channel IN ('web', 'sip')),
    phone_number text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
    session_id text NOT NULL REFERENCES sessions (session_id),
    sequence_no bigint NOT NULL CHECK (sequence_no >= 0),
    offset_ms bigint NOT NULL CHECK (offset_ms >= 0),
    event_type text NOT NULL,
    revision integer CHECK (revision >= 0),
    payload jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, sequence_no)
);

CREATE TABLE operations (
    operation_id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES sessions (session_id),
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL
);

COMMIT;
