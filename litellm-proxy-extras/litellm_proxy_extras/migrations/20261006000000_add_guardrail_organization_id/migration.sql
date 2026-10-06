ALTER TABLE "LiteLLM_GuardrailsTable" ADD COLUMN IF NOT EXISTS "organization_id" TEXT;

CREATE INDEX IF NOT EXISTS "LiteLLM_GuardrailsTable_organization_id_idx" ON "LiteLLM_GuardrailsTable"("organization_id");
