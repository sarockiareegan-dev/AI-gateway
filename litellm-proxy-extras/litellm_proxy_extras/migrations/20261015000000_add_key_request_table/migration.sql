-- CreateTable
CREATE TABLE IF NOT EXISTS "LiteLLM_KeyRequestTable" (
    "request_id" TEXT NOT NULL,
    "team_id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "key_alias" TEXT,
    "models" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "max_budget" DOUBLE PRECISION,
    "duration" TEXT,
    "reason" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "reviewed_by" TEXT,
    "reviewed_at" TIMESTAMP(3),
    "review_note" TEXT,
    "key_token" TEXT,
    "encrypted_key" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "LiteLLM_KeyRequestTable_pkey" PRIMARY KEY ("request_id")
);

-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_KeyRequestTable_team_id_status_idx" ON "LiteLLM_KeyRequestTable"("team_id", "status");

-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_KeyRequestTable_user_id_idx" ON "LiteLLM_KeyRequestTable"("user_id");
