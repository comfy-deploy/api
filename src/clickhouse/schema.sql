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
PARTITION BY toYYYYMM(timestamp)
ORDER BY (run_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 14 DAY DELETE
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
