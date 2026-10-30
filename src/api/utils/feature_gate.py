from typing import List, Dict, Any


# Centralized Autumn feature-gate rules.
# Each rule supports:
# - pattern: glob matching against the request path (e.g., "/api/run*"),
# - methods: list of HTTP methods to apply the rule,
# - exclude: optional list of glob patterns to skip within the pattern,
# - only_free: apply only for free plan users (default True),
# - mode: "all" (default) requires all checks to pass; "any" requires one,
# - checks: list of { feature_id: str, required_balance: int } items.

AUTUMN_GUARD_RULES: List[Dict[str, Any]] = [
    {
        "pattern": "/api/workflow/*",
        "methods": ["POST"],
        "only_free": False,
        "mode": "all",
        "checks": [
            {"feature_id": "workflow_limit"},
        ],
    },
    {
        # Gate creating self-hosted/custom machines behind a boolean feature
        "pattern": "/api/machine/custom",
        "methods": ["POST"],
        "only_free": False,  # applies to all plans; Autumn decides
        "mode": "all",
        "checks": [
            {"feature_id": "self_hosted_machines"},  # boolean feature
        ],
    },
    {
        # Gate updating self-hosted/custom machines behind the same boolean feature
        "pattern": "/api/machine/custom/*",
        "methods": ["PATCH"],
        "only_free": False,
        "mode": "all",
        "checks": [
            {"feature_id": "self_hosted_machines"},
        ],
    },
    {
        "pattern": "/api/machine/serverless",
        "methods": ["POST"],
        "only_free": False,
        "mode": "all",
        "checks": [
            # Static pre-check; the route performs a dynamic count-aware check.
            {"feature_id": "machine_limit", "required_balance": 1},
        ],
    },
    {
        "pattern": "/api/session",
        "methods": ["POST"],
        "only_free": True,
        "mode": "all",
        "checks": [
            {"feature_id": "gpu-credit-topup", "required_balance": 500},
        ],
    }
]
