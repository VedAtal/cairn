from __future__ import annotations

import random
from dataclasses import dataclass

SEED = 20261018
PROJECTS = ("platform", "mobile", "data")


@dataclass(frozen=True)
class Node:
    path: str
    title: str
    summary: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entry:
    node: str
    type: str
    title: str
    body: str
    keywords: tuple[str, ...] = ()
    key: str = ""


@dataclass(frozen=True)
class Query:
    concepts: tuple[str, ...]
    types: tuple[str, ...] = ()
    within: str = ""

    def __str__(self) -> str:
        extra = (f" types={','.join(self.types)}" if self.types else "") + (f" in {self.within}" if self.within else "")
        return " + ".join(self.concepts) + extra


NODES = [
    Node("platform/billing", "Billing service", "Stripe integration: invoices, refunds and proration.", ("payments",)),
    Node("platform/billing/webhooks", "Webhooks", "Inbound Stripe events, signatures and retries."),
    Node("platform/auth", "Authentication", "OAuth2 login, sessions and API keys.", ("oauth", "jwt")),
    Node("platform/auth/tokens", "Tokens", "Access and refresh token lifetimes."),
    Node("platform/deploy", "Deploys", "Kubernetes, Helm charts and rollout policy.", ("k8s",)),
    Node("platform/deploy/ci", "CI pipelines", "GitHub Actions workflows and caches."),
    Node("platform/observability", "Observability", "Logs, metrics and tracing.", ("otel",)),
    Node("mobile/ios", "iOS app", "Swift app and App Store releases."),
    Node("mobile/ios/push", "Push notifications", "APNs tokens and delivery."),
    Node("mobile/android", "Android app", "Kotlin app and Play Store releases."),
    Node("mobile/android/build", "Gradle builds", "Build variants, signing and the build cache."),
    Node("mobile/sync", "Offline sync", "Local database, conflict resolution and the asset cache."),
    Node("data/warehouse", "Warehouse", "Snowflake models built with dbt.", ("snowflake",)),
    Node("data/pipelines", "Pipelines", "Airflow DAGs and backfills.", ("airflow",)),
    Node("data/pipelines/kafka", "Kafka ingestion", "Consumers, topics and offsets."),
    Node("data/ml", "Model training", "Feature store and training jobs."),
    Node("data/quality", "Data quality", "Freshness and volume checks."),
]

GOLDEN = [
    Entry("platform/billing/webhooks", "gotcha",
          "Stripe webhooks retry for up to 3 days, so handlers must be idempotent",
          "Stripe resends failed events with exponential backoff for 72 hours; dedupe on event id.", key="stripe"),
    Entry("platform/deploy", "gotcha", "ERR_TLS_CERT_ALTNAME_INVALID means the certificate lacks the service hostname",
          "Seen when Node calls the internal API through the mesh. Add the hostname to the SANs.", key="altname"),
    Entry("mobile/sync", "decision", "FileLoader caches decoded assets by absolute path",
          "The cache key is the resolved path, so symlinked bundles load twice.", key="fileloader"),
    Entry("data/pipelines/kafka", "gotcha",
          "kafkaConsumerGroup rebalances stall ingestion when max.poll.interval.ms is exceeded",
          "Long batches block the poll loop; the broker evicts the consumer and partitions rebalance.", key="consumer"),
    Entry("data/warehouse", "practice", "Run dbt with --full-refresh only after a schema change",
          "Incremental models otherwise rebuild from scratch and burn warehouse credits.", key="dbt"),
    Entry("mobile/ios/push", "open_question", "Do APNs device tokens change after restoring an iPhone from backup?",
          "We saw duplicate pushes after restores but could not reproduce it on iOS 18.", key="apns"),
    Entry("platform/auth", "decision", "API keys are stored as SHA-256 hashes and shown once",
          "Lost keys are revoked and reissued; there is no recovery path.", key="keys"),
]

SUBJECTS = {
    "platform/billing": ["invoice PDFs", "the refund job", "proration", "InvoiceRenderer", "billing_cycle_anchor",
                         "credit notes", "tax rates", "dunning emails", "coupon codes", "usage records"],
    "platform/billing/webhooks": ["webhook signatures", "the event queue", "WebhookDispatcher", "event replay",
                                  "the dead letter topic", "signature tolerance"],
    "platform/auth": ["OAuth2 login", "session cookies", "SAML metadata", "the PasswordHasher", "login throttling",
                      "magic links", "the auth_callback route"],
    "platform/auth/tokens": ["refresh tokens", "JWT access tokens", "token_exchange", "clock skew checks",
                             "the JWKS cache"],
    "platform/deploy": ["Helm releases", "rollouts", "the canary step", "readiness probes", "PodDisruptionBudgets",
                        "the deploy_lock", "image tags"],
    "platform/deploy/ci": ["the GitHub Actions cache", "flaky integration tests", "the build matrix", "test sharding",
                           "the lint job", "Docker layer caching"],
    "platform/observability": ["trace sampling", "the OTEL collector", "log retention", "p95 latency alerts",
                               "the metrics_exporter", "structured logs"],
    "mobile/ios": ["SwiftUI previews", "App Store review", "the Keychain wrapper", "background fetch",
                   "TestFlight builds", "universal links"],
    "mobile/ios/push": ["APNs tokens", "silent pushes", "notification grouping", "the PushRouter", "badge counts"],
    "mobile/android": ["Play Store review", "WorkManager jobs", "deep links", "ProGuard rules",
                       "the RoomDatabase migration", "battery optimizations"],
    "mobile/android/build": ["the Gradle build cache", "signing configs", "build variants", "the versionCode bump",
                             "R8 shrinking", "KAPT"],
    "mobile/sync": ["offline writes", "conflict resolution", "the SyncEngine", "tombstones", "the local SQLite file",
                    "delta sync", "AssetPrefetcher"],
    "data/warehouse": ["dbt snapshots", "Snowflake warehouses", "incremental models", "the dim_customers model",
                       "query tags", "clustering keys"],
    "data/pipelines": ["Airflow backfills", "DAG concurrency", "the PipelineRunner", "sensor timeouts",
                       "task retries", "the nightly batch"],
    "data/pipelines/kafka": ["consumer lag", "topic retention", "the schema registry", "offset commits",
                             "partition counts", "the ingest_worker"],
    "data/ml": ["the feature store", "training jobs", "the model registry", "GPU quotas", "drift checks",
                "the FeatureBuilder"],
    "data/quality": ["freshness checks", "volume anomalies", "Great Expectations suites", "null rate alerts",
                     "the quality_gate", "row count diffs"],
}

PREDICATES = [
    "time out after {n} seconds", "retry up to {n} times with exponential backoff", "must be idempotent",
    "are cached for {n} minutes", "fail silently when the config is missing", "need a feature flag in staging",
    "break after upgrading to v{n}", "are rate limited to {n} requests per minute", "leak memory under sustained load",
    "must run before the database migration", "ignore the request id header", "double count after a retry",
    "lose data if the pod is evicted", "need manual approval in production", "drift when clocks are skewed",
    "use a separate service account", "are invalidated on every deploy", "block the main thread on cold start",
    "skip validation for internal callers", "require the x-request-id header", "page oncall at {n} percent errors",
    "cost about {n} dollars a day", "were rewritten in Rust", "depend on the shared Redis cluster",
]
CONDITIONS = [" in staging", " on cold start", " during the nightly batch", " behind the VPN", " for EU customers",
              " on Postgres 16", " when the cache is cold", " after a failover"]
SENTENCES = [
    "Seen in {ident} logs since the last release.", "The fix is to set {ident} explicitly in the service config.",
    "Root cause was a race between the worker and the scheduler.",
    "Check the dashboard before paging anyone; it usually recovers within {n} minutes.",
    "Documented in the runbook under {word}.", "We tried increasing the pool size, which made it worse.",
    "See incident {n} for the timeline.", "The vendor confirmed this is expected behavior.",
    "Monitoring alerts on {ident} when the error rate passes {n} percent.",
    "Only affects accounts created before the {word} migration.",
    "Rolling back is safe; the change is backward compatible.", "Owners: the {word} squad.",
    "It took {n} hours to notice because {ident} hides the failure.", "Ask in the {word} channel before changing it.",
    "Load tests at {n} rps reproduce it reliably.", "The {ident} default changed in the {word} upgrade.",
]
IDENTS = ["ERR_CONN_RESET", "max_connections", "RetryPolicy", "user_id", "HttpClient", "statement_timeout",
          "AWS_REGION", "pool.size", "kafka.consumer.timeout", "OOMKilled", "SIGTERM", "deadline_exceeded",
          "Cache-Control", "request_id"]
WORDS = ["quiescence", "backpressure", "thundering herd", "split brain", "hysteresis", "jitter", "debounce",
         "fan-out", "head-of-line blocking", "write amplification", "idempotency", "tombstoning"]
KEYWORDS = ["latency", "reliability", "cost", "security", "migration", "performance", "oncall"]
TYPES = ["general", "gotcha", "gotcha", "decision", "reference", "practice", "open_question"]


def _generate(n: int) -> list[Entry]:
    rng = random.Random(SEED)
    out = []
    for _ in range(n):
        node = rng.choice(NODES).path
        subject = rng.choice(SUBJECTS[node])
        predicate = rng.choice(PREDICATES).format(n=rng.choice([2, 3, 5, 10, 15, 30, 60, 100, 500]))
        title = subject[0].upper() + subject[1:] + " " + predicate + (rng.choice(CONDITIONS) if rng.random() < 0.4
                                                                       else "")
        body = " ".join(rng.choice(SENTENCES).format(ident=rng.choice(IDENTS), word=rng.choice(WORDS),
                                                     n=rng.randrange(2, 900)) for _ in range(rng.randrange(2, 4)))
        keywords = tuple(rng.sample(KEYWORDS, rng.randrange(0, 3)))
        out.append(Entry(node, rng.choice(TYPES), title, body, keywords))
    return out


ENTRIES = GOLDEN + _generate(300)
# Near-copies of existing entries, to exercise the dedup gate.
PROBES = [Entry(e.node, e.type, e.title.replace(" the ", " a ") + ".", e.body, e.keywords) for e in ENTRIES[::15]] + [
    Entry(e.node, "general", " ".join(e.title.split()[:3]) + " notes", e.body) for e in ENTRIES[3::40]]

QUERIES = [
    Query(("stripe|webhook", "retry|backoff|idempotency")),
    Query(("altname",)),
    Query(("ALTNAME_INVALID",)),
    Query(("ERR_TLS_CERT_ALTNAME_INVALID",)),
    Query(("certaltname",)),
    Query(("file_loader",)),
    Query(("FileLoader",)),
    Query(("consumergroup",)),
    Query(("kafka", "rebalance|lag")),
    Query(("dbt", "full refresh|full-refresh")),
    Query(("APNs|push notification",), types=("open_question",)),
    Query(("xylophone|quokka",)),
    Query(("zzz_unknown_ident",)),
    Query(("timeout|time out", "retry")),
    Query(("cache|caching", "invalidate|invalidation|ttl")),
    Query(("oauth|oauth2", "token|refresh token")),
    Query(("helm", "rollout|rollback")),
    Query(("gradle", "build cache")),
    Query(("offline sync", "conflict|merge")),
    Query(("airflow|dag", "backfill")),
    Query(("snowflake|bigquery", "cost|credits")),
    Query(("rate limit|throttle",)),
    Query(("the",)),
    Query(("service", "config")),
    Query(("idempotent|idempotency",)),
    Query(("kafka",), within="data"),
    Query(("webhook|invoice",), within="platform/billing"),
    Query(("deploy|release",), types=("decision", "gotcha")),
    Query(("metrics|tracing", "sampling")),
    Query(("x-request-id|request id",)),
    Query(("schema change|migration",)),
    Query(("ios", "restore|backup")),
    Query(("memory leak|oom|OOMKilled",)),
    Query(("staging", "feature flag")),
    Query(("clock skew", "token|jwt")),
    Query(("retry", "backoff", "jitter")),
    Query(("latency|p95", "postgres", "cache")),
    Query(("upgrade|upgrading", "v2|v3|v5")),
    Query(("refund|proration",)),
    Query(("loader",)),
    Query(("pipeline_runner|PipelineRunner",)),
    Query(("nightly batch|cron",), types=("practice", "reference")),
    Query(("pool",)),
    Query(("redis", "shared"), within="mobile"),
]

GOLDEN_FIRST = [
    (QUERIES[0], "stripe"), (QUERIES[1], "altname"), (QUERIES[2], "altname"), (QUERIES[3], "altname"),
    (QUERIES[4], "altname"), (QUERIES[5], "fileloader"), (QUERIES[6], "fileloader"), (QUERIES[7], "consumer"),
    (QUERIES[9], "dbt"), (Query(("APNs", "restore|backup"), types=("open_question",)), "apns"),
    (Query(("api key|apikey", "sha256|hash")), "keys"),
]
