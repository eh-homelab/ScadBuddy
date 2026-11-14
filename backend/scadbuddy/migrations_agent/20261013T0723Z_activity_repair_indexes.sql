-- The backend's activity repair pass reads ai_operations for rows created in its window
-- every 5 minutes; `finished_at` is already indexed (ai_operations_finished).
CREATE INDEX IF NOT EXISTS ai_operations_created_at ON ai_operations (created_at);
