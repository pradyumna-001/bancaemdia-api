"""Aggregate billing health; never label metrics with customer or event identities."""

from prometheus_client import Counter, Gauge

reconciliations = Counter(
    "billing_reconciliations", "Billing accounts reconciled by scan outcome", ["outcome"]
)
inbox = Gauge(
    "billing_inbox_events",
    "Durable billing inbox size by state",
    ["state"],
    multiprocess_mode="mostrecent",
)
oldest_pending = Gauge(
    "billing_oldest_pending_seconds",
    "Age of the oldest pending billing notification",
    multiprocess_mode="mostrecent",
)
oldest_scan = Gauge(
    "billing_oldest_scan_seconds",
    "Age of the least recently scanned mapped active account",
    multiprocess_mode="mostrecent",
)
heartbeat = Gauge(
    "billing_scan_timestamp_seconds",
    "Last completed billing scan, including provider failures",
    multiprocess_mode="mostrecent",
)
