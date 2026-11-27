BEGIN;
-- Identity assigns a number to existing rows as well as subsequent inserts.
ALTER TABLE crm_operations ADD COLUMN IF NOT EXISTS spoken_reference integer
    GENERATED ALWAYS AS IDENTITY (MINVALUE 1 MAXVALUE 99999999 NO CYCLE);
CREATE UNIQUE INDEX IF NOT EXISTS crm_operations_spoken_reference_idx ON crm_operations (spoken_reference);
COMMIT;
