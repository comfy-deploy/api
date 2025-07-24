CREATE DATABASE IF NOT EXISTS default;

CREATE TABLE default.workflow_events
(
    `user_id` String,
    `org_id` Nullable(String),
    `machine_id` UUID,
    `gpu_event_id` Nullable(UUID),
    `workflow_id` UUID,
    `workflow_version_id` Nullable(UUID),
    `run_id` UUID,
    `timestamp` DateTime64(3),
    `log_type` Enum8('input' = 0, 
        'not-started' = 1,
        'queued' = 2,
        'started' = 3,
        'running' = 4,
        'executing' = 5,
        'uploading' = 6,
        'success' = 7,
        'failed' = 8,
        'timeout' = 9,
        'cancelled' = 10,
        'output' = 11,
        'ws-event' = 12),
    `progress` Float32,
    `log` String,
)
ENGINE = MergeTree()
ORDER BY (workflow_id, run_id, user_id)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;

CREATE TABLE default.log_entries
(
    `event_id` UUID,
    `run_id` UUID,
    `workflow_id` UUID,
    `machine_id` UUID,
    `timestamp` DateTime64(3),
    `log_level` Enum8('debug' = 1, 'info' = 2, 'warning' = 3, 'error' = 4, 'ws_event' = 5, 'builder' = 6, 'webhook' = 7),
    `message` String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (run_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

CREATE TABLE IF NOT EXISTS default.gpu_events
(
    `user_id` String,
    `org_id` Nullable(String),
    `machine_id` UUID,
    `gpu` Nullable(String),
    `ws_gpu` Nullable(String),
    `cost_item_title` Nullable(String),
    `start_time` DateTime64(3),
    `end_time` Nullable(DateTime64(3)),
    `cost` Nullable(Float64)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(start_time)
ORDER BY (user_id, org_id, machine_id, start_time)
TTL toDateTime(start_time) + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS default.gpu_usage_daily_mv
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(day)
ORDER BY (user_id, org_id, gpu, day)
POPULATE
AS SELECT
    user_id,
    org_id,
    gpu,
    toDate(start_time) as day,
    sum(toFloat64(end_time - start_time)) as usage_seconds,
    sum(cost) as total_cost,
    count() as event_count
FROM default.gpu_events
WHERE end_time IS NOT NULL
GROUP BY user_id, org_id, gpu, day;

CREATE MATERIALIZED VIEW IF NOT EXISTS default.gpu_usage_machine_mv
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(day)
ORDER BY (user_id, org_id, machine_id, gpu, day)
POPULATE
AS SELECT
    user_id,
    org_id,
    machine_id,
    gpu,
    ws_gpu,
    cost_item_title,
    toDate(start_time) as day,
    sum(toFloat64(end_time - start_time)) as usage_seconds,
    sum(cost) as total_cost,
    count() as event_count
FROM default.gpu_events
WHERE end_time IS NOT NULL
GROUP BY user_id, org_id, machine_id, gpu, ws_gpu, cost_item_title, day;
