CREATE TABLE IF NOT EXISTS crm_operations (
    operation_key text PRIMARY KEY NOT NULL CHECK (length(trim(operation_key)) > 0),
    operation_type text NOT NULL CHECK (operation_type IN ('order', 'coverage_request', 'handoff')),
    confirmed_terms text NOT NULL,
    receipt_id text NOT NULL UNIQUE,
    session_id text NOT NULL CHECK (length(trim(session_id)) > 0),
    sequence_no bigint NOT NULL CHECK (sequence_no >= 1),
    UNIQUE (session_id, sequence_no)
);
