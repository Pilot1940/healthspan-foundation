"""HealthSpan shared ingestion/export library.

Modules:
  db        — connection (reads .db-config.json) + profile resolution
  contract  — the four-step ingestion contract (resolve → validate → write → log)
"""
