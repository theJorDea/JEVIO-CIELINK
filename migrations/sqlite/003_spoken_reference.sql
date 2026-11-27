-- OperationStore executes these statements inside BEGIN IMMEDIATE, once.
CREATE TABLE crm_operations_v3 (
    spoken_reference INTEGER PRIMARY KEY AUTOINCREMENT CHECK (spoken_reference BETWEEN 1 AND 99999999),
    operation_key text NOT NULL UNIQUE CHECK (length(trim(operation_key)) > 0),
    operation_type text NOT NULL CHECK (operation_type IN ('order', 'coverage_request', 'handoff')),
    confirmed_terms text NOT NULL,
    receipt_id text NOT NULL UNIQUE,
    session_id text NOT NULL CHECK (length(trim(session_id)) > 0),
    sequence_no bigint NOT NULL CHECK (sequence_no >= 1),
    UNIQUE (session_id, sequence_no)
);
INSERT INTO crm_operations_v3 (operation_key, operation_type, confirmed_terms, receipt_id, session_id, sequence_no)
SELECT operation_key, operation_type, confirmed_terms, receipt_id, session_id, sequence_no FROM crm_operations ORDER BY rowid;
DROP TABLE crm_operations;
ALTER TABLE crm_operations_v3 RENAME TO crm_operations;
