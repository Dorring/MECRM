# Kafka topic initialization (PowerShell / Windows host).
# Idempotent: creates missing topics and ensures config on existing topics.
# Fails fast if any broker or partition leader check times out.
param(
    [string]$Broker = $env:KAFKA_BROKERS
)

$ErrorActionPreference = "Stop"

if (-not $Broker) { $Broker = "localhost:9092" }

$ReplicationFactor = if ($env:KAFKA_REPLICATION_FACTOR) { [int]$env:KAFKA_REPLICATION_FACTOR } else { 1 }
$DefaultRetention = if ($env:KAFKA_DEFAULT_RETENTION_MS) { [int]$env:KAFKA_DEFAULT_RETENTION_MS } else { 604800000 }
$AuditRetention = if ($env:KAFKA_AUDIT_RETENTION_MS) { [int]$env:KAFKA_AUDIT_RETENTION_MS } else { 2592000000 }
$DlqRetention = if ($env:KAFKA_DLQ_RETENTION_MS) { [int]$env:KAFKA_DLQ_RETENTION_MS } else { 1209600000 }
$LeaderTimeout = if ($env:KAFKA_LEADER_TIMEOUT) { [int]$env:KAFKA_LEADER_TIMEOUT } else { 120 }
$LeaderInterval = if ($env:KAFKA_LEADER_INTERVAL) { [int]$env:KAFKA_LEADER_INTERVAL } else { 2 }

$Topics = @(
    # CRM domain events
    "crm.leads.created:6:$DefaultRetention"
    "crm.leads.updated:6:$DefaultRetention"
    "crm.leads.qualified:6:$DefaultRetention"
    "crm.leads.events:6:$DefaultRetention"
    "crm.deals.created:6:$DefaultRetention"
    "crm.deals.updated:6:$DefaultRetention"
    "crm.deals.stage-changed:6:$DefaultRetention"
    "crm.deals.closed:6:$DefaultRetention"
    "crm.tickets.created:6:$DefaultRetention"
    "crm.tickets.updated:6:$DefaultRetention"
    "crm.tickets.resolved:6:$DefaultRetention"
    "crm.tickets.sla-breached:6:$DefaultRetention"
    "crm.tickets.events:6:$DefaultRetention"
    "crm.tickets.escalate:6:$DefaultRetention"
    "crm.customers.created:6:$DefaultRetention"
    "crm.customers.updated:6:$DefaultRetention"
    "crm.payments.recorded:3:$DefaultRetention"
    "crm.conversations.closed:3:$DefaultRetention"
    "crm.tasks.updated:3:$DefaultRetention"
    "crm.user.activity:3:$DefaultRetention"
    "crm.invoices.updated:3:$DefaultRetention"

    # Approvals
    "crm.approvals.required:3:$DefaultRetention"
    "crm.approvals.decision:3:$DefaultRetention"

    # Agent events
    "crm.agents.task-assigned:6:$DefaultRetention"
    "crm.agents.action-proposed:6:$DefaultRetention"
    "crm.agents.action-executed:6:$DefaultRetention"
    "crm.agents.reasoning:3:$DefaultRetention"
    "crm.agents.dlq:6:$DlqRetention"
    "crm.agents.deal-analyzed:3:$DefaultRetention"
    "crm.agents.next-action-recommended:3:$DefaultRetention"
    "crm.agents.metric-recorded:3:$DefaultRetention"
    "crm.agents.anomaly-detected:3:$DefaultRetention"
    "crm.agents.data-validated:3:$DefaultRetention"
    "crm.agents.ticket-triaged:6:$DefaultRetention"
    "crm.agents.resolution-suggested:6:$DefaultRetention"

    # Audit / GDPR / Security
    "crm.audit.events:6:$AuditRetention"
    "crm.audit.accessed:3:$AuditRetention"
    "crm.security.events:3:$AuditRetention"
    "crm.killswitch.activated:3:$AuditRetention"
    "crm.gdpr.forget:3:$AuditRetention"

    # Productivity
    "crm.productivity.signal:6:$DefaultRetention"
    "crm.productivity.action-suggested:3:$DefaultRetention"
    "crm.productivity.action-approved:3:$DefaultRetention"
    "crm.productivity.action-rejected:3:$DefaultRetention"

    # Journey + Predictions
    "crm.journey.updated:6:$DefaultRetention"
    "crm.analytics.prediction-generated:3:$DefaultRetention"
    "crm.analytics.forecast-requested:3:$DefaultRetention"

    # Automations
    "crm.automation.policy-created:3:$DefaultRetention"
    "crm.automation.policy-updated:3:$DefaultRetention"
    "crm.automation.simulation.requested:3:$DefaultRetention"
    "crm.automation.simulation.result:3:$DefaultRetention"
    "crm.automation.executed:3:$DefaultRetention"
    "crm.automation.action.requested:3:$DefaultRetention"

    # Knowledge Base
    "crm.knowledge.draft.created:3:$DefaultRetention"
    "crm.knowledge.published:3:$DefaultRetention"

    # Intelligence
    "crm.intelligence.user-query:3:$DefaultRetention"
    "crm.intelligence.search-performed:3:$DefaultRetention"
    "crm.intelligence.search-clicked:3:$DefaultRetention"
    "crm.intelligence.search-abandoned:3:$DefaultRetention"
    "crm.intelligence.agent-decision:3:$DefaultRetention"
    "crm.intelligence.tool-called:3:$DefaultRetention"
    "crm.intelligence.action-suggested:3:$DefaultRetention"
    "crm.intelligence.voice-received:3:$DefaultRetention"
    "crm.intelligence.language-detected:3:$DefaultRetention"
    "crm.intelligence.twin-simulation-executed:3:$DefaultRetention"
    "crm.intelligence.twin-profile-updated:3:$DefaultRetention"
    "crm.intelligence.dev-insight-generated:3:$DefaultRetention"
    "crm.intelligence.dev-anomaly-detected:3:$DefaultRetention"

    # Dead Letter Queues
    "crm.dlq.leads:6:$DlqRetention"
    "crm.dlq.agents:6:$DlqRetention"
    "crm.dlq.approvals:3:$DlqRetention"
)

Write-Host "=== Kafka topic init against $Broker (replication=$ReplicationFactor) ==="

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($LeaderTimeout).ToUnixTimeSeconds()
while ($true) {
    & kafka-broker-api-versions --bootstrap-server $Broker 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $deadline) {
        throw "ERROR: broker $Broker did not become reachable within ${LeaderTimeout}s"
    }
    Write-Host "Waiting for broker $Broker..."
    Start-Sleep -Seconds $LeaderInterval
}

foreach ($spec in $Topics) {
    $parts = $spec -split ':'
    $topic = $parts[0]
    $partitionCount = [int]$parts[1]
    $retention = [int]$parts[2]

    $exists = $false
    try {
        & kafka-topics --bootstrap-server $Broker --describe --topic $topic 2>&1 | Out-Null
        $exists = $LASTEXITCODE -eq 0
    } catch { $exists = $false }

    if ($exists) {
        Write-Host "Topic $topic exists — ensuring retention.ms=$retention"
        & kafka-configs --bootstrap-server $Broker --entity-type topics --entity-name $topic `
            --alter --add-config "retention.ms=$retention,cleanup.policy=delete" | Out-Null
    } else {
        Write-Host "Creating topic $topic (partitions=$partitionCount, replication=$ReplicationFactor, retention=$retention)"
        & kafka-topics --bootstrap-server $Broker --create `
            --topic $topic `
            --partitions $partitionCount `
            --replication-factor $ReplicationFactor `
            --config "retention.ms=$retention" `
            --config "cleanup.policy=delete" | Out-Null
    }
}

Write-Host "=== Waiting for all partition leaders (timeout=${LeaderTimeout}s) ==="
$deadline = [DateTimeOffset]::UtcNow.AddSeconds($LeaderTimeout).ToUnixTimeSeconds()
while ($true) {
    $describe = & kafka-topics --bootstrap-server $Broker --describe 2>&1
    $bad = 0
    if ($LASTEXITCODE -eq 0) {
        foreach ($line in $describe) {
            if ($line -match 'Leader:\s*(-1|none)') {
                $bad++
            }
        }
    } else {
        $bad = 1
    }

    if ($bad -eq 0) {
        Write-Host "=== All partition leaders ready ==="
        break
    }

    if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $deadline) {
        throw "ERROR: $bad partition(s) still without a leader after ${LeaderTimeout}s`nDescribe output:`n$($describe -join "`n")"
    }
    Write-Host "Waiting for leaders... ($bad partition(s) without leader)"
    Start-Sleep -Seconds $LeaderInterval
}

Write-Host "=== Kafka topic init OK ($($Topics.Count) topics) ==="
