#!/usr/bin/env bash
# Kafka topic initialization.
# Runs once before gateway/agents start. Idempotent: creates missing topics and
# ensures config on existing topics. Fails fast on any error so downstream
# services do not start with a broken Kafka topology.
set -euo pipefail

BROKER="${KAFKA_BROKERS:-kafka:9092}"
REPLICATION="${KAFKA_REPLICATION_FACTOR:-1}"
DEFAULT_RETENTION_MS="${KAFKA_DEFAULT_RETENTION_MS:-604800000}"  # 7 days
AUDIT_RETENTION_MS="${KAFKA_AUDIT_RETENTION_MS:-2592000000}"      # 30 days
DLQ_RETENTION_MS="${KAFKA_DLQ_RETENTION_MS:-1209600000}"          # 14 days
LEADER_TIMEOUT="${KAFKA_LEADER_TIMEOUT:-120}"                     # seconds
LEADER_INTERVAL="${KAFKA_LEADER_INTERVAL:-2}"                     # seconds

# Topic declarations: "name:partitions:retention_ms"
TOPICS=(
  # CRM domain events (high volume)
  "crm.leads.created:6:$DEFAULT_RETENTION_MS"
  "crm.leads.updated:6:$DEFAULT_RETENTION_MS"
  "crm.leads.qualified:6:$DEFAULT_RETENTION_MS"
  "crm.leads.events:6:$DEFAULT_RETENTION_MS"
  "crm.deals.created:6:$DEFAULT_RETENTION_MS"
  "crm.deals.updated:6:$DEFAULT_RETENTION_MS"
  "crm.deals.stage-changed:6:$DEFAULT_RETENTION_MS"
  "crm.deals.closed:6:$DEFAULT_RETENTION_MS"
  "crm.tickets.created:6:$DEFAULT_RETENTION_MS"
  "crm.tickets.updated:6:$DEFAULT_RETENTION_MS"
  "crm.tickets.resolved:6:$DEFAULT_RETENTION_MS"
  "crm.tickets.sla-breached:6:$DEFAULT_RETENTION_MS"
  "crm.tickets.events:6:$DEFAULT_RETENTION_MS"
  "crm.tickets.escalate:6:$DEFAULT_RETENTION_MS"
  "crm.customers.created:6:$DEFAULT_RETENTION_MS"
  "crm.customers.updated:6:$DEFAULT_RETENTION_MS"
  "crm.payments.recorded:3:$DEFAULT_RETENTION_MS"
  "crm.conversations.closed:3:$DEFAULT_RETENTION_MS"
  "crm.tasks.updated:3:$DEFAULT_RETENTION_MS"
  "crm.user.activity:3:$DEFAULT_RETENTION_MS"
  "crm.invoices.updated:3:$DEFAULT_RETENTION_MS"

  # Approvals
  "crm.approvals.required:3:$DEFAULT_RETENTION_MS"
  "crm.approvals.decision:3:$DEFAULT_RETENTION_MS"

  # Agent events
  "crm.agents.task-assigned:6:$DEFAULT_RETENTION_MS"
  "crm.agents.action-proposed:6:$DEFAULT_RETENTION_MS"
  "crm.agents.action-executed:6:$DEFAULT_RETENTION_MS"
  "crm.agents.reasoning:3:$DEFAULT_RETENTION_MS"
  "crm.agents.dlq:6:$DLQ_RETENTION_MS"
  "crm.agents.deal-analyzed:3:$DEFAULT_RETENTION_MS"
  "crm.agents.next-action-recommended:3:$DEFAULT_RETENTION_MS"
  "crm.agents.metric-recorded:3:$DEFAULT_RETENTION_MS"
  "crm.agents.anomaly-detected:3:$DEFAULT_RETENTION_MS"
  "crm.agents.data-validated:3:$DEFAULT_RETENTION_MS"
  "crm.agents.ticket-triaged:6:$DEFAULT_RETENTION_MS"
  "crm.agents.resolution-suggested:6:$DEFAULT_RETENTION_MS"

  # Audit / GDPR / Security
  "crm.audit.events:6:$AUDIT_RETENTION_MS"
  "crm.audit.accessed:3:$AUDIT_RETENTION_MS"
  "crm.security.events:3:$AUDIT_RETENTION_MS"
  "crm.killswitch.activated:3:$AUDIT_RETENTION_MS"
  "crm.gdpr.forget:3:$AUDIT_RETENTION_MS"

  # Productivity
  "crm.productivity.signal:6:$DEFAULT_RETENTION_MS"
  "crm.productivity.action-suggested:3:$DEFAULT_RETENTION_MS"
  "crm.productivity.action-approved:3:$DEFAULT_RETENTION_MS"
  "crm.productivity.action-rejected:3:$DEFAULT_RETENTION_MS"

  # Journey + Predictions
  "crm.journey.updated:6:$DEFAULT_RETENTION_MS"
  "crm.analytics.prediction-generated:3:$DEFAULT_RETENTION_MS"
  "crm.analytics.forecast-requested:3:$DEFAULT_RETENTION_MS"

  # Automations
  "crm.automation.policy-created:3:$DEFAULT_RETENTION_MS"
  "crm.automation.policy-updated:3:$DEFAULT_RETENTION_MS"
  "crm.automation.simulation.requested:3:$DEFAULT_RETENTION_MS"
  "crm.automation.simulation.result:3:$DEFAULT_RETENTION_MS"
  "crm.automation.executed:3:$DEFAULT_RETENTION_MS"
  "crm.automation.action.requested:3:$DEFAULT_RETENTION_MS"

  # Knowledge Base
  "crm.knowledge.draft.created:3:$DEFAULT_RETENTION_MS"
  "crm.knowledge.published:3:$DEFAULT_RETENTION_MS"

  # Intelligence (search, chat, voice, twins, devx)
  "crm.intelligence.user-query:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.search-performed:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.search-clicked:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.search-abandoned:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.agent-decision:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.tool-called:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.action-suggested:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.voice-received:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.language-detected:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.twin-simulation-executed:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.twin-profile-updated:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.dev-insight-generated:3:$DEFAULT_RETENTION_MS"
  "crm.intelligence.dev-anomaly-detected:3:$DEFAULT_RETENTION_MS"

  # Dead Letter Queues (longer retention for investigation)
  "crm.dlq.leads:6:$DLQ_RETENTION_MS"
  "crm.dlq.agents:6:$DLQ_RETENTION_MS"
  "crm.dlq.approvals:3:$DLQ_RETENTION_MS"
)

echo "=== Kafka topic init against $BROKER (replication=$REPLICATION) ==="

# Wait until the broker answers API versions (up to KAFKA_LEADER_TIMEOUT).
_bootstrap_deadline=$(( $(date +%s) + LEADER_TIMEOUT ))
while ! kafka-broker-api-versions --bootstrap-server "$BROKER" >/dev/null 2>&1; do
  if [[ $(date +%s) -gt $_bootstrap_deadline ]]; then
    echo "ERROR: broker $BROKER did not become reachable within ${LEADER_TIMEOUT}s" >&2
    exit 1
  fi
  echo "Waiting for broker $BROKER..."
  sleep "$LEADER_INTERVAL"
done

for spec in "${TOPICS[@]}"; do
  IFS=':' read -r topic partitions retention <<< "$spec"

  if kafka-topics --bootstrap-server "$BROKER" --describe --topic "$topic" >/dev/null 2>&1; then
    echo "Topic $topic exists — ensuring retention.ms=$retention"
    kafka-configs --bootstrap-server "$BROKER" --entity-type topics --entity-name "$topic" \
      --alter --add-config "retention.ms=$retention,cleanup.policy=delete"
  else
    echo "Creating topic $topic (partitions=$partitions, replication=$REPLICATION, retention=$retention)"
    kafka-topics --bootstrap-server "$BROKER" --create \
      --topic "$topic" \
      --partitions "$partitions" \
      --replication-factor "$REPLICATION" \
      --config "retention.ms=$retention" \
      --config "cleanup.policy=delete"
  fi
done

# Verify every partition has a leader before declaring success.
echo "=== Waiting for all partition leaders (timeout=${LEADER_TIMEOUT}s) ==="
_deadline=$(( $(date +%s) + LEADER_TIMEOUT ))
while true; do
  _bad=0
  _describe=$(kafka-topics --bootstrap-server "$BROKER" --describe 2>/dev/null || true)
  if [[ -n "$_describe" ]]; then
    # Count partitions whose leader is -1 or none.
    _bad=$(echo "$_describe" | awk '/Partition:/{p=1} /Leader:/{if ($2 ~ /^(-1|none)$/) bad++} END{print bad+0}')
  fi

  if [[ "$_bad" -eq 0 && -n "$_describe" ]]; then
    echo "=== All partition leaders ready ==="
    break
  fi

  if [[ $(date +%s) -gt $_deadline ]]; then
    echo "ERROR: $_bad partition(s) still without a leader after ${LEADER_TIMEOUT}s" >&2
    echo "Describe output:" >&2
    echo "$_describe" >&2
    exit 1
  fi
  echo "Waiting for leaders... ($_bad partition(s) without leader)"
  sleep "$LEADER_INTERVAL"
done

echo "=== Kafka topic init OK ($((${#TOPICS[@]})) topics) ==="
