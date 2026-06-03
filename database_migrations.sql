-- ================================================================
-- Database Migrations for Week 1 Enhancements
-- Run this script on your Azure PostgreSQL database
-- ================================================================

-- 1. Processing History Table
CREATE TABLE IF NOT EXISTS processing_history (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    file_size BIGINT,
    file_type TEXT,
    status TEXT CHECK (status IN ('in_progress', 'success', 'failed')),
    characters_extracted INTEGER,
    chunks_created INTEGER,
    chunks_inserted INTEGER,
    error_message TEXT,
    processing_time_ms INTEGER,
    user_id TEXT DEFAULT 'default',
    graphrag_input_path TEXT,
    raw_blob_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_processing_history_status ON processing_history(status);
CREATE INDEX IF NOT EXISTS idx_processing_history_created ON processing_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_processing_history_filename ON processing_history(filename);

-- 2. API Keys Table
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    key_hash TEXT UNIQUE NOT NULL,
    key_name TEXT NOT NULL,
    created_by TEXT DEFAULT 'admin',
    is_active BOOLEAN DEFAULT true,
    rate_limit INTEGER DEFAULT 100,  -- requests per hour
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,
    usage_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active);

-- 3. API Requests (Audit Log)
CREATE TABLE IF NOT EXISTS api_requests (
    id SERIAL PRIMARY KEY,
    api_key_id INTEGER REFERENCES api_keys(id),
    endpoint TEXT NOT NULL,
    method TEXT NOT NULL,
    status_code INTEGER,
    response_time_ms INTEGER,
    ip_address TEXT,
    user_agent TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_api_requests_created ON api_requests(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_requests_key_id ON api_requests(api_key_id);
CREATE INDEX IF NOT EXISTS idx_api_requests_endpoint ON api_requests(endpoint);

-- 4. System Metrics (for internal tracking)
CREATE TABLE IF NOT EXISTS system_metrics (
    id SERIAL PRIMARY KEY,
    metric_name TEXT NOT NULL,
    metric_value NUMERIC NOT NULL,
    metric_type TEXT,  -- 'counter', 'gauge', 'histogram'
    tags JSONB,  -- flexible tag storage
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_system_metrics_name ON system_metrics(metric_name);
CREATE INDEX IF NOT EXISTS idx_system_metrics_created ON system_metrics(created_at DESC);

-- 5. Insert default API key (for initial testing)
-- Key: "docint-default-key-2024" (plaintext, hash it in production!)
INSERT INTO api_keys (key_hash, key_name, created_by, is_active, rate_limit)
VALUES (
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',  -- placeholder hash
    'Default Development Key',
    'system',
    true,
    1000
) ON CONFLICT (key_hash) DO NOTHING;

-- 6. Create view for processing statistics
CREATE OR REPLACE VIEW processing_stats AS
SELECT 
    COUNT(*) as total_documents,
    COUNT(*) FILTER (WHERE status = 'success') as successful,
    COUNT(*) FILTER (WHERE status = 'failed') as failed,
    COUNT(*) FILTER (WHERE status = 'in_progress') as in_progress,
    AVG(processing_time_ms) FILTER (WHERE status = 'success') as avg_processing_time_ms,
    AVG(characters_extracted) FILTER (WHERE status = 'success') as avg_characters,
    AVG(chunks_created) FILTER (WHERE status = 'success') as avg_chunks,
    MAX(created_at) as last_processing_time,
    DATE_TRUNC('day', created_at)::date as processing_date
FROM processing_history
GROUP BY processing_date
ORDER BY processing_date DESC;

-- 7. Create view for API usage statistics
CREATE OR REPLACE VIEW api_usage_stats AS
SELECT 
    COUNT(*) as total_requests,
    COUNT(DISTINCT api_key_id) as unique_keys,
    AVG(response_time_ms) as avg_response_time,
    COUNT(*) FILTER (WHERE status_code >= 400) as error_count,
    COUNT(*) FILTER (WHERE status_code < 400) as success_count,
    endpoint,
    method,
    DATE_TRUNC('hour', created_at) as request_hour
FROM api_requests
GROUP BY endpoint, method, request_hour
ORDER BY request_hour DESC;

-- 8. GraphRAG Jobs Table (for persistent job tracking)
CREATE TABLE IF NOT EXISTS graphrag_jobs (
    job_id UUID PRIMARY KEY,
    status TEXT CHECK (status IN ('running', 'completed', 'failed')) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    stdout TEXT,
    stderr TEXT,
    error_message TEXT,
    return_code INTEGER,
    index_exists BOOLEAN DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_graphrag_jobs_status ON graphrag_jobs(status);
CREATE INDEX IF NOT EXISTS idx_graphrag_jobs_created ON graphrag_jobs(created_at DESC);

-- Function to auto-cleanup old jobs (older than 24 hours)
CREATE OR REPLACE FUNCTION cleanup_old_graphrag_jobs()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM graphrag_jobs 
    WHERE created_at < NOW() - INTERVAL '24 hours'
    AND status IN ('completed', 'failed');
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Verification queries (run these to check)
-- SELECT * FROM processing_history LIMIT 5;
-- SELECT * FROM api_keys WHERE is_active = true;
-- SELECT * FROM processing_stats WHERE processing_date >= CURRENT_DATE - INTERVAL '7 days';
-- SELECT * FROM api_usage_stats LIMIT 10;
-- SELECT * FROM graphrag_jobs ORDER BY created_at DESC LIMIT 10;
-- SELECT cleanup_old_graphrag_jobs(); -- Clean up old jobs
