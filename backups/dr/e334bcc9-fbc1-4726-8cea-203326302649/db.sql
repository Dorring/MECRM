--
-- PostgreSQL database dump
--

\restrict S0XCVuVjovOX7fqQNq260QaNEsG0zsxP0yt6Y0AEpSULi0yUGkQ0qc2pS6oAq8u

-- Dumped from database version 16.11
-- Dumped by pg_dump version 16.11

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

DROP POLICY IF EXISTS users_tenant_isolation ON public.users;
DROP POLICY IF EXISTS user_roles_tenant_isolation ON public.user_roles;
DROP POLICY IF EXISTS tickets_tenant_isolation ON public.tickets;
DROP POLICY IF EXISTS security_events_tenant_isolation ON public.security_events;
DROP POLICY IF EXISTS roles_tenant_isolation ON public.roles;
DROP POLICY IF EXISTS replay_jobs_tenant_isolation ON public.replay_jobs;
DROP POLICY IF EXISTS processed_events_tenant_isolation ON public.processed_events;
DROP POLICY IF EXISTS policies_tenant_isolation ON public.policies;
DROP POLICY IF EXISTS outbox_events_tenant_isolation ON public.outbox_events;
DROP POLICY IF EXISTS leads_tenant_isolation ON public.leads;
DROP POLICY IF EXISTS lead_read_model_tenant_isolation ON public.lead_read_model;
DROP POLICY IF EXISTS events_tenant_isolation ON public.events;
DROP POLICY IF EXISTS event_streams_tenant_isolation ON public.event_streams;
DROP POLICY IF EXISTS event_log_tenant_isolation ON public.event_log;
DROP POLICY IF EXISTS domain_events_tenant_isolation ON public.domain_events;
DROP POLICY IF EXISTS deals_tenant_isolation ON public.deals;
DROP POLICY IF EXISTS deal_pipeline_view_tenant_isolation ON public.deal_pipeline_view;
DROP POLICY IF EXISTS customers_tenant_isolation ON public.customers;
DROP POLICY IF EXISTS customer_timeline_view_tenant_isolation ON public.customer_timeline_view;
DROP POLICY IF EXISTS audit_logs_tenant_isolation ON public.audit_logs;
DROP POLICY IF EXISTS approvals_tenant_isolation ON public.approvals;
DROP POLICY IF EXISTS ai_memory_tenant_isolation ON public.ai_memory;
DROP POLICY IF EXISTS aggregate_snapshots_tenant_isolation ON public.aggregate_snapshots;
DROP POLICY IF EXISTS agent_tasks_tenant_isolation ON public.agent_tasks;
DROP POLICY IF EXISTS agent_events_tenant_isolation ON public.agent_events;
DROP POLICY IF EXISTS agent_decisions_tenant_isolation ON public.agent_decisions;
ALTER TABLE IF EXISTS ONLY public.users DROP CONSTRAINT IF EXISTS users_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_fkey;
ALTER TABLE IF EXISTS ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_role_id_fkey;
ALTER TABLE IF EXISTS ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_assigned_by_fkey;
ALTER TABLE IF EXISTS ONLY public.tickets DROP CONSTRAINT IF EXISTS tickets_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.tickets DROP CONSTRAINT IF EXISTS tickets_customer_id_fkey;
ALTER TABLE IF EXISTS ONLY public.tickets DROP CONSTRAINT IF EXISTS tickets_created_by_fkey;
ALTER TABLE IF EXISTS ONLY public.tickets DROP CONSTRAINT IF EXISTS tickets_assigned_to_fkey;
ALTER TABLE IF EXISTS ONLY public.security_events DROP CONSTRAINT IF EXISTS security_events_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.security_events DROP CONSTRAINT IF EXISTS security_events_resolved_by_fkey;
ALTER TABLE IF EXISTS ONLY public.roles DROP CONSTRAINT IF EXISTS roles_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.policies DROP CONSTRAINT IF EXISTS policies_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.leads DROP CONSTRAINT IF EXISTS leads_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.leads DROP CONSTRAINT IF EXISTS leads_created_by_fkey;
ALTER TABLE IF EXISTS ONLY public.leads DROP CONSTRAINT IF EXISTS leads_assigned_to_fkey;
ALTER TABLE IF EXISTS ONLY public.domain_events DROP CONSTRAINT IF EXISTS domain_events_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.deals DROP CONSTRAINT IF EXISTS deals_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.deals DROP CONSTRAINT IF EXISTS deals_lead_id_fkey;
ALTER TABLE IF EXISTS ONLY public.deals DROP CONSTRAINT IF EXISTS deals_customer_id_fkey;
ALTER TABLE IF EXISTS ONLY public.deals DROP CONSTRAINT IF EXISTS deals_created_by_fkey;
ALTER TABLE IF EXISTS ONLY public.deals DROP CONSTRAINT IF EXISTS deals_assigned_to_fkey;
ALTER TABLE IF EXISTS ONLY public.data_retention_policies DROP CONSTRAINT IF EXISTS data_retention_policies_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.customers DROP CONSTRAINT IF EXISTS customers_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.customers DROP CONSTRAINT IF EXISTS customers_created_by_fkey;
ALTER TABLE IF EXISTS ONLY public.audit_logs DROP CONSTRAINT IF EXISTS audit_logs_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.approvals DROP CONSTRAINT IF EXISTS approvals_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.approvals DROP CONSTRAINT IF EXISTS approvals_decided_by_fkey;
ALTER TABLE IF EXISTS ONLY public.ai_memory DROP CONSTRAINT IF EXISTS ai_memory_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.ai_memory DROP CONSTRAINT IF EXISTS ai_memory_agent_id_fkey;
ALTER TABLE IF EXISTS ONLY public.agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_agent_id_fkey;
ALTER TABLE IF EXISTS ONLY public.agent_events DROP CONSTRAINT IF EXISTS agent_events_tenant_id_fkey;
ALTER TABLE IF EXISTS ONLY public.agent_events DROP CONSTRAINT IF EXISTS agent_events_task_id_fkey;
ALTER TABLE IF EXISTS ONLY public.agent_events DROP CONSTRAINT IF EXISTS agent_events_agent_id_fkey;
DROP INDEX IF EXISTS public.users_tenant_id_email_key;
DROP INDEX IF EXISTS public.user_roles_tenant_id_user_id_role_id_key;
DROP INDEX IF EXISTS public.tickets_tenant_id_status_idx;
DROP INDEX IF EXISTS public.tenants_slug_key;
DROP INDEX IF EXISTS public.security_events_created_at_severity_idx;
DROP INDEX IF EXISTS public.roles_tenant_id_name_key;
DROP INDEX IF EXISTS public.replay_jobs_tenant_status_idx;
DROP INDEX IF EXISTS public.replay_jobs_tenant_aggregate_idx;
DROP INDEX IF EXISTS public.outbox_pending_idx;
DROP INDEX IF EXISTS public.outbox_events_tenant_event_id_key;
DROP INDEX IF EXISTS public.leads_tenant_id_status_idx;
DROP INDEX IF EXISTS public.lead_read_model_status_idx;
DROP INDEX IF EXISTS public.idx_agent_decisions_tenant_time;
DROP INDEX IF EXISTS public.idx_agent_decisions_tenant_approval;
DROP INDEX IF EXISTS public.idx_agent_decisions_tenant_agent_time;
DROP INDEX IF EXISTS public.events_tenant_stream_version_key;
DROP INDEX IF EXISTS public.events_tenant_idempotency_key_uniq;
DROP INDEX IF EXISTS public.events_tenant_event_id_key;
DROP INDEX IF EXISTS public.events_stream_lookup_idx;
DROP INDEX IF EXISTS public.event_log_tenant_ts_idx;
DROP INDEX IF EXISTS public.event_log_tenant_aggregate_version_idx;
DROP INDEX IF EXISTS public.event_log_tenant_aggregate_ts_idx;
DROP INDEX IF EXISTS public.domain_events_tenant_id_created_at_idx;
DROP INDEX IF EXISTS public.domain_events_aggregate_type_aggregate_id_idx;
DROP INDEX IF EXISTS public.deals_tenant_id_stage_idx;
DROP INDEX IF EXISTS public.data_retention_policies_tenant_id_entity_type_key;
DROP INDEX IF EXISTS public.data_retention_policies_tenant_id_entity_type_idx;
DROP INDEX IF EXISTS public.customers_tenant_id_idx;
DROP INDEX IF EXISTS public.audit_logs_tenant_id_created_at_idx;
DROP INDEX IF EXISTS public.approvals_tenant_id_status_idx;
DROP INDEX IF EXISTS public.ai_agents_name_key;
DROP INDEX IF EXISTS public.aggregate_snapshots_ts_idx;
DROP INDEX IF EXISTS public.aggregate_snapshots_latest_idx;
DROP INDEX IF EXISTS public.agent_tasks_tenant_id_status_idx;
DROP INDEX IF EXISTS public.agent_events_tenant_id_created_at_idx;
ALTER TABLE IF EXISTS ONLY public.users DROP CONSTRAINT IF EXISTS users_pkey;
ALTER TABLE IF EXISTS ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_pkey;
ALTER TABLE IF EXISTS ONLY public.tickets DROP CONSTRAINT IF EXISTS tickets_pkey;
ALTER TABLE IF EXISTS ONLY public.tenants DROP CONSTRAINT IF EXISTS tenants_pkey;
ALTER TABLE IF EXISTS ONLY public.security_events DROP CONSTRAINT IF EXISTS security_events_pkey;
ALTER TABLE IF EXISTS ONLY public.roles DROP CONSTRAINT IF EXISTS roles_pkey;
ALTER TABLE IF EXISTS ONLY public.replay_jobs DROP CONSTRAINT IF EXISTS replay_jobs_pkey;
ALTER TABLE IF EXISTS ONLY public.processed_events DROP CONSTRAINT IF EXISTS processed_events_pkey;
ALTER TABLE IF EXISTS ONLY public.policies DROP CONSTRAINT IF EXISTS policies_pkey;
ALTER TABLE IF EXISTS ONLY public.outbox_events DROP CONSTRAINT IF EXISTS outbox_events_pkey;
ALTER TABLE IF EXISTS ONLY public.leads DROP CONSTRAINT IF EXISTS leads_pkey;
ALTER TABLE IF EXISTS ONLY public.lead_read_model DROP CONSTRAINT IF EXISTS lead_read_model_pkey;
ALTER TABLE IF EXISTS ONLY public.events DROP CONSTRAINT IF EXISTS events_pkey;
ALTER TABLE IF EXISTS ONLY public.event_streams DROP CONSTRAINT IF EXISTS event_streams_pkey;
ALTER TABLE IF EXISTS ONLY public.event_log DROP CONSTRAINT IF EXISTS event_log_pkey;
ALTER TABLE IF EXISTS ONLY public.event_log DROP CONSTRAINT IF EXISTS event_log_event_id_key;
ALTER TABLE IF EXISTS ONLY public.event_log DROP CONSTRAINT IF EXISTS event_log_aggregate_version_key;
ALTER TABLE IF EXISTS ONLY public.domain_events DROP CONSTRAINT IF EXISTS domain_events_pkey;
ALTER TABLE IF EXISTS ONLY public.deals DROP CONSTRAINT IF EXISTS deals_pkey;
ALTER TABLE IF EXISTS ONLY public.deal_pipeline_view DROP CONSTRAINT IF EXISTS deal_pipeline_view_pkey;
ALTER TABLE IF EXISTS ONLY public.data_retention_policies DROP CONSTRAINT IF EXISTS data_retention_policies_pkey;
ALTER TABLE IF EXISTS ONLY public.customers DROP CONSTRAINT IF EXISTS customers_pkey;
ALTER TABLE IF EXISTS ONLY public.customer_timeline_view DROP CONSTRAINT IF EXISTS customer_timeline_view_pkey;
ALTER TABLE IF EXISTS ONLY public.audit_logs DROP CONSTRAINT IF EXISTS audit_logs_pkey;
ALTER TABLE IF EXISTS ONLY public.approvals DROP CONSTRAINT IF EXISTS approvals_pkey;
ALTER TABLE IF EXISTS ONLY public.ai_memory DROP CONSTRAINT IF EXISTS ai_memory_pkey;
ALTER TABLE IF EXISTS ONLY public.ai_agents DROP CONSTRAINT IF EXISTS ai_agents_pkey;
ALTER TABLE IF EXISTS ONLY public.aggregate_snapshots DROP CONSTRAINT IF EXISTS aggregate_snapshots_pkey;
ALTER TABLE IF EXISTS ONLY public.agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_pkey;
ALTER TABLE IF EXISTS ONLY public.agent_events DROP CONSTRAINT IF EXISTS agent_events_pkey;
ALTER TABLE IF EXISTS ONLY public.agent_decisions DROP CONSTRAINT IF EXISTS agent_decisions_pkey;
DROP TABLE IF EXISTS public.users;
DROP TABLE IF EXISTS public.user_roles;
DROP TABLE IF EXISTS public.tickets;
DROP TABLE IF EXISTS public.tenants;
DROP TABLE IF EXISTS public.security_events;
DROP TABLE IF EXISTS public.roles;
DROP TABLE IF EXISTS public.replay_jobs;
DROP TABLE IF EXISTS public.processed_events;
DROP TABLE IF EXISTS public.policies;
DROP TABLE IF EXISTS public.outbox_events;
DROP TABLE IF EXISTS public.leads;
DROP TABLE IF EXISTS public.lead_read_model;
DROP TABLE IF EXISTS public.events;
DROP TABLE IF EXISTS public.event_streams;
DROP TABLE IF EXISTS public.event_log;
DROP TABLE IF EXISTS public.domain_events;
DROP TABLE IF EXISTS public.deals;
DROP TABLE IF EXISTS public.deal_pipeline_view;
DROP TABLE IF EXISTS public.data_retention_policies;
DROP TABLE IF EXISTS public.customers;
DROP TABLE IF EXISTS public.customer_timeline_view;
DROP TABLE IF EXISTS public.audit_logs;
DROP TABLE IF EXISTS public.approvals;
DROP TABLE IF EXISTS public.ai_memory;
DROP TABLE IF EXISTS public.ai_agents;
DROP TABLE IF EXISTS public.aggregate_snapshots;
DROP TABLE IF EXISTS public.agent_tasks;
DROP TABLE IF EXISTS public.agent_events;
DROP TABLE IF EXISTS public.agent_decisions;
SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_decisions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    agent_id text NOT NULL,
    action_type text NOT NULL,
    risk_level text NOT NULL,
    status text NOT NULL,
    confidence numeric(4,3),
    input_context jsonb DEFAULT '{}'::jsonb NOT NULL,
    reasoning jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    tool_calls jsonb DEFAULT '[]'::jsonb NOT NULL,
    approval_id uuid,
    correlation_id uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.agent_decisions FORCE ROW LEVEL SECURITY;


--
-- Name: agent_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    task_id uuid,
    event_type character varying(100) NOT NULL,
    action_type character varying(100),
    target_entity character varying(100),
    target_id uuid,
    reasoning text,
    confidence numeric(3,2),
    is_approved boolean,
    requires_approval boolean DEFAULT false NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.agent_events FORCE ROW LEVEL SECURITY;


--
-- Name: agent_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_tasks (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    agent_id uuid NOT NULL,
    task_type character varying(100) NOT NULL,
    input_data jsonb NOT NULL,
    output_data jsonb,
    status character varying(50) DEFAULT 'pending'::character varying NOT NULL,
    priority integer DEFAULT 5 NOT NULL,
    started_at timestamp(3) without time zone,
    completed_at timestamp(3) without time zone,
    error_message text,
    correlation_id uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.agent_tasks FORCE ROW LEVEL SECURITY;


--
-- Name: aggregate_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.aggregate_snapshots (
    tenant_id uuid NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    version integer NOT NULL,
    ts timestamp with time zone NOT NULL,
    state jsonb NOT NULL,
    kafka_topic text,
    kafka_partition integer,
    kafka_offset bigint,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.aggregate_snapshots FORCE ROW LEVEL SECURITY;


--
-- Name: ai_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_agents (
    id uuid NOT NULL,
    name character varying(100) NOT NULL,
    type character varying(50) NOT NULL,
    description text,
    capabilities jsonb DEFAULT '[]'::jsonb NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);


--
-- Name: ai_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_memory (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    agent_id uuid,
    memory_type character varying(50) NOT NULL,
    content text NOT NULL,
    embedding_id character varying(255),
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.ai_memory FORCE ROW LEVEL SECURITY;


--
-- Name: approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approvals (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    request_type character varying(100) NOT NULL,
    requestor_type character varying(50) NOT NULL,
    requestor_id uuid NOT NULL,
    action_type character varying(100) NOT NULL,
    target_entity character varying(100),
    target_id uuid,
    context jsonb NOT NULL,
    status character varying(50) DEFAULT 'pending'::character varying NOT NULL,
    decided_by uuid,
    decided_at timestamp(3) without time zone,
    decision_reason text,
    expires_at timestamp(3) without time zone,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.approvals FORCE ROW LEVEL SECURITY;


--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    actor_type character varying(50) NOT NULL,
    actor_id uuid NOT NULL,
    action character varying(100) NOT NULL,
    resource_type character varying(100) NOT NULL,
    resource_id uuid,
    old_value jsonb,
    new_value jsonb,
    ip_address character varying(45),
    user_agent text,
    correlation_id uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.audit_logs FORCE ROW LEVEL SECURITY;


--
-- Name: customer_timeline_view; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customer_timeline_view (
    tenant_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    last_event_at timestamp(3) without time zone,
    open_tickets integer DEFAULT 0 NOT NULL,
    active_deals integer DEFAULT 0 NOT NULL,
    updated_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.customer_timeline_view FORCE ROW LEVEL SECURITY;


--
-- Name: customers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customers (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    email character varying(255),
    phone character varying(50),
    company character varying(255),
    segment character varying(100),
    lifetime_value numeric(15,2) DEFAULT 0 NOT NULL,
    status character varying(50) DEFAULT 'active'::character varying NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL,
    deleted_at timestamp(3) without time zone,
    deletion_type character varying(50)
);

ALTER TABLE ONLY public.customers FORCE ROW LEVEL SECURITY;


--
-- Name: data_retention_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.data_retention_policies (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    entity_type text NOT NULL,
    retention_days integer NOT NULL,
    hard_delete boolean DEFAULT false NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);


--
-- Name: deal_pipeline_view; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deal_pipeline_view (
    tenant_id uuid NOT NULL,
    stage text NOT NULL,
    deal_count integer DEFAULT 0 NOT NULL,
    total_amount numeric(15,2) DEFAULT 0 NOT NULL,
    updated_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.deal_pipeline_view FORCE ROW LEVEL SECURITY;


--
-- Name: deals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deals (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    lead_id uuid,
    customer_id uuid,
    stage character varying(100) DEFAULT 'prospecting'::character varying NOT NULL,
    amount numeric(15,2),
    currency character varying(3) DEFAULT 'USD'::character varying NOT NULL,
    probability integer DEFAULT 0 NOT NULL,
    expected_close_date date,
    actual_close_date date,
    won boolean,
    assigned_to uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);

ALTER TABLE ONLY public.deals FORCE ROW LEVEL SECURITY;


--
-- Name: domain_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.domain_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    event_type character varying(255) NOT NULL,
    aggregate_type character varying(100) NOT NULL,
    aggregate_id uuid NOT NULL,
    event_data jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    version integer NOT NULL,
    correlation_id uuid,
    causation_id uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.domain_events FORCE ROW LEVEL SECURITY;


--
-- Name: event_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_log (
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    version integer NOT NULL,
    ts timestamp with time zone NOT NULL,
    payload jsonb NOT NULL,
    kafka_topic text,
    kafka_partition integer,
    kafka_offset bigint
);

ALTER TABLE ONLY public.event_log FORCE ROW LEVEL SECURITY;


--
-- Name: event_streams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_streams (
    tenant_id uuid NOT NULL,
    stream_id text NOT NULL,
    current_version integer DEFAULT 0 NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.event_streams FORCE ROW LEVEL SECURITY;


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    stream_id text NOT NULL,
    version integer NOT NULL,
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    schema_version integer DEFAULT 1 NOT NULL,
    payload jsonb NOT NULL,
    idempotency_key text,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.events FORCE ROW LEVEL SECURITY;


--
-- Name: lead_read_model; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lead_read_model (
    tenant_id uuid NOT NULL,
    lead_id uuid NOT NULL,
    name text NOT NULL,
    email text,
    phone text,
    company text,
    status text NOT NULL,
    score integer,
    assigned_to uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    version integer DEFAULT 0 NOT NULL,
    updated_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.lead_read_model FORCE ROW LEVEL SECURITY;


--
-- Name: leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leads (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    email character varying(255),
    phone character varying(50),
    company character varying(255),
    source character varying(100),
    status character varying(50) DEFAULT 'new'::character varying NOT NULL,
    score integer,
    assigned_to uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);

ALTER TABLE ONLY public.leads FORCE ROW LEVEL SECURITY;


--
-- Name: outbox_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbox_events (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    topic text NOT NULL,
    payload jsonb NOT NULL,
    schema_version integer DEFAULT 1 NOT NULL,
    published_at timestamp(3) without time zone,
    retry_count integer DEFAULT 0 NOT NULL,
    last_error text,
    idempotency_key text,
    next_attempt_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    dead_lettered_at timestamp(3) without time zone,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.outbox_events FORCE ROW LEVEL SECURITY;


--
-- Name: policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.policies (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    policy_type character varying(50) NOT NULL,
    rego_content text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);

ALTER TABLE ONLY public.policies FORCE ROW LEVEL SECURITY;


--
-- Name: processed_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processed_events (
    tenant_id uuid NOT NULL,
    event_id uuid NOT NULL,
    processed_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.processed_events FORCE ROW LEVEL SECURITY;


--
-- Name: replay_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.replay_jobs (
    job_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    mode text NOT NULL,
    topic text NOT NULL,
    partition integer DEFAULT 0 NOT NULL,
    start_offset bigint NOT NULL,
    end_offset bigint,
    target_time timestamp with time zone,
    status text NOT NULL,
    started_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at timestamp with time zone,
    events_processed integer DEFAULT 0 NOT NULL,
    snapshot_used boolean DEFAULT false NOT NULL,
    error text
);

ALTER TABLE ONLY public.replay_jobs FORCE ROW LEVEL SECURITY;


--
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roles (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    description text,
    permissions jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);

ALTER TABLE ONLY public.roles FORCE ROW LEVEL SECURITY;


--
-- Name: security_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_events (
    id uuid NOT NULL,
    tenant_id uuid,
    event_type character varying(100) NOT NULL,
    severity character varying(50) NOT NULL,
    source character varying(100) NOT NULL,
    actor_id uuid,
    description text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved boolean DEFAULT false NOT NULL,
    resolved_at timestamp(3) without time zone,
    resolved_by uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.security_events FORCE ROW LEVEL SECURITY;


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenants (
    id uuid NOT NULL,
    name character varying(255) NOT NULL,
    slug character varying(100) NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(50) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);


--
-- Name: tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tickets (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    subject character varying(500) NOT NULL,
    description text,
    customer_id uuid,
    priority character varying(50) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(50) DEFAULT 'open'::character varying NOT NULL,
    category character varying(100),
    assigned_to uuid,
    sla_due_at timestamp(3) without time zone,
    resolved_at timestamp(3) without time zone,
    resolution text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by uuid,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL
);

ALTER TABLE ONLY public.tickets FORCE ROW LEVEL SECURITY;


--
-- Name: user_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_roles (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role_id uuid NOT NULL,
    assigned_by uuid,
    assigned_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

ALTER TABLE ONLY public.user_roles FORCE ROW LEVEL SECURITY;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255),
    name character varying(255) NOT NULL,
    status character varying(50) DEFAULT 'active'::character varying NOT NULL,
    last_login_at timestamp(3) without time zone,
    created_at timestamp(3) without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp(3) without time zone NOT NULL,
    deleted_at timestamp(3) without time zone,
    deletion_type character varying(50)
);

ALTER TABLE ONLY public.users FORCE ROW LEVEL SECURITY;


--
-- Data for Name: agent_decisions; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.agent_decisions (id, tenant_id, agent_id, action_type, risk_level, status, confidence, input_context, reasoning, evidence, tool_calls, approval_id, correlation_id, created_at) FROM stdin;
\.


--
-- Data for Name: agent_events; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.agent_events (id, tenant_id, agent_id, task_id, event_type, action_type, target_entity, target_id, reasoning, confidence, is_approved, requires_approval, metadata, created_at) FROM stdin;
\.


--
-- Data for Name: agent_tasks; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.agent_tasks (id, tenant_id, agent_id, task_type, input_data, output_data, status, priority, started_at, completed_at, error_message, correlation_id, created_at) FROM stdin;
\.


--
-- Data for Name: aggregate_snapshots; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.aggregate_snapshots (tenant_id, aggregate_type, aggregate_id, version, ts, state, kafka_topic, kafka_partition, kafka_offset, created_at) FROM stdin;
\.


--
-- Data for Name: ai_agents; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.ai_agents (id, name, type, description, capabilities, config, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: ai_memory; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.ai_memory (id, tenant_id, agent_id, memory_type, content, embedding_id, context, created_at) FROM stdin;
\.


--
-- Data for Name: approvals; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.approvals (id, tenant_id, request_type, requestor_type, requestor_id, action_type, target_entity, target_id, context, status, decided_by, decided_at, decision_reason, expires_at, created_at) FROM stdin;
\.


--
-- Data for Name: audit_logs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.audit_logs (id, tenant_id, actor_type, actor_id, action, resource_type, resource_id, old_value, new_value, ip_address, user_agent, correlation_id, created_at) FROM stdin;
\.


--
-- Data for Name: customer_timeline_view; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.customer_timeline_view (tenant_id, customer_id, last_event_at, open_tickets, active_deals, updated_at) FROM stdin;
\.


--
-- Data for Name: customers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.customers (id, tenant_id, name, email, phone, company, segment, lifetime_value, status, metadata, created_by, created_at, updated_at, deleted_at, deletion_type) FROM stdin;
\.


--
-- Data for Name: data_retention_policies; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.data_retention_policies (id, tenant_id, entity_type, retention_days, hard_delete, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: deal_pipeline_view; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.deal_pipeline_view (tenant_id, stage, deal_count, total_amount, updated_at) FROM stdin;
\.


--
-- Data for Name: deals; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.deals (id, tenant_id, name, lead_id, customer_id, stage, amount, currency, probability, expected_close_date, actual_close_date, won, assigned_to, metadata, created_by, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: domain_events; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.domain_events (id, tenant_id, event_type, aggregate_type, aggregate_id, event_data, metadata, version, correlation_id, causation_id, created_at) FROM stdin;
\.


--
-- Data for Name: event_log; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_log (id, event_id, tenant_id, aggregate_type, aggregate_id, event_type, version, ts, payload, kafka_topic, kafka_partition, kafka_offset) FROM stdin;
\.


--
-- Data for Name: event_streams; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_streams (tenant_id, stream_id, current_version, created_at, updated_at) FROM stdin;
d4933832-942f-45ce-acf9-e43413d090c4	lead:6ea9032e-8753-41a6-88a1-a3f87abe4bf4	2	2026-01-24 18:40:04.966	2026-01-24 18:40:04.988
\.


--
-- Data for Name: events; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.events (id, tenant_id, stream_id, version, event_id, event_type, schema_version, payload, idempotency_key, created_at) FROM stdin;
ec25200e-26f1-41e8-8576-786307202926	d4933832-942f-45ce-acf9-e43413d090c4	lead:6ea9032e-8753-41a6-88a1-a3f87abe4bf4	1	1731c6b6-80f4-4dcb-b25f-6ffc2b7bebaa	lead.created	1	{"name": "DR Lead", "leadId": "6ea9032e-8753-41a6-88a1-a3f87abe4bf4", "status": "new"}	\N	2026-01-24 18:40:04.916
ee7543da-d37f-4aef-b799-47d355edbb36	d4933832-942f-45ce-acf9-e43413d090c4	lead:6ea9032e-8753-41a6-88a1-a3f87abe4bf4	2	05e562c5-990b-4e10-8c8f-9a13c280ad2b	lead.updated	1	{"leadId": "6ea9032e-8753-41a6-88a1-a3f87abe4bf4", "changes": {"score": 10, "status": "qualified"}}	\N	2026-01-24 18:40:04.916
\.


--
-- Data for Name: lead_read_model; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.lead_read_model (tenant_id, lead_id, name, email, phone, company, status, score, assigned_to, metadata, version, updated_at) FROM stdin;
\.


--
-- Data for Name: leads; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.leads (id, tenant_id, name, email, phone, company, source, status, score, assigned_to, metadata, created_by, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: outbox_events; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.outbox_events (id, tenant_id, event_id, event_type, topic, payload, schema_version, published_at, retry_count, last_error, idempotency_key, next_attempt_at, dead_lettered_at, created_at) FROM stdin;
\.


--
-- Data for Name: policies; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.policies (id, tenant_id, name, description, policy_type, rego_content, is_active, version, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: processed_events; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.processed_events (tenant_id, event_id, processed_at) FROM stdin;
\.


--
-- Data for Name: replay_jobs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.replay_jobs (job_id, tenant_id, aggregate_type, aggregate_id, mode, topic, partition, start_offset, end_offset, target_time, status, started_at, finished_at, events_processed, snapshot_used, error) FROM stdin;
\.


--
-- Data for Name: roles; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.roles (id, tenant_id, name, description, permissions, is_system, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: security_events; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.security_events (id, tenant_id, event_type, severity, source, actor_id, description, metadata, resolved, resolved_at, resolved_by, created_at) FROM stdin;
\.


--
-- Data for Name: tenants; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.tenants (id, name, slug, settings, status, created_at, updated_at) FROM stdin;
d4933832-942f-45ce-acf9-e43413d090c4	tenant-d4933832-942f-45ce-acf9-e43413d090c4	tenant-d4933832-942f-45ce-acf9-e43413d090c4	{}	active	2026-01-24 18:40:04.916	2026-01-24 18:40:04.916
\.


--
-- Data for Name: tickets; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.tickets (id, tenant_id, subject, description, customer_id, priority, status, category, assigned_to, sla_due_at, resolved_at, resolution, metadata, created_by, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: user_roles; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.user_roles (id, tenant_id, user_id, role_id, assigned_by, assigned_at) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.users (id, tenant_id, email, password_hash, name, status, last_login_at, created_at, updated_at, deleted_at, deletion_type) FROM stdin;
\.


--
-- Name: agent_decisions agent_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_decisions
    ADD CONSTRAINT agent_decisions_pkey PRIMARY KEY (id);


--
-- Name: agent_events agent_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_pkey PRIMARY KEY (id);


--
-- Name: agent_tasks agent_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tasks
    ADD CONSTRAINT agent_tasks_pkey PRIMARY KEY (id);


--
-- Name: aggregate_snapshots aggregate_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aggregate_snapshots
    ADD CONSTRAINT aggregate_snapshots_pkey PRIMARY KEY (tenant_id, aggregate_type, aggregate_id, version);


--
-- Name: ai_agents ai_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_agents
    ADD CONSTRAINT ai_agents_pkey PRIMARY KEY (id);


--
-- Name: ai_memory ai_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_memory
    ADD CONSTRAINT ai_memory_pkey PRIMARY KEY (id);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: customer_timeline_view customer_timeline_view_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customer_timeline_view
    ADD CONSTRAINT customer_timeline_view_pkey PRIMARY KEY (tenant_id, customer_id);


--
-- Name: customers customers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);


--
-- Name: data_retention_policies data_retention_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_retention_policies
    ADD CONSTRAINT data_retention_policies_pkey PRIMARY KEY (id);


--
-- Name: deal_pipeline_view deal_pipeline_view_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deal_pipeline_view
    ADD CONSTRAINT deal_pipeline_view_pkey PRIMARY KEY (tenant_id, stage);


--
-- Name: deals deals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_pkey PRIMARY KEY (id);


--
-- Name: domain_events domain_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_events
    ADD CONSTRAINT domain_events_pkey PRIMARY KEY (id);


--
-- Name: event_log event_log_aggregate_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_log
    ADD CONSTRAINT event_log_aggregate_version_key UNIQUE (tenant_id, aggregate_type, aggregate_id, version);


--
-- Name: event_log event_log_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_log
    ADD CONSTRAINT event_log_event_id_key UNIQUE (event_id);


--
-- Name: event_log event_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_log
    ADD CONSTRAINT event_log_pkey PRIMARY KEY (id);


--
-- Name: event_streams event_streams_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_streams
    ADD CONSTRAINT event_streams_pkey PRIMARY KEY (tenant_id, stream_id);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: lead_read_model lead_read_model_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lead_read_model
    ADD CONSTRAINT lead_read_model_pkey PRIMARY KEY (tenant_id, lead_id);


--
-- Name: leads leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);


--
-- Name: outbox_events outbox_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT outbox_events_pkey PRIMARY KEY (id);


--
-- Name: policies policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policies
    ADD CONSTRAINT policies_pkey PRIMARY KEY (id);


--
-- Name: processed_events processed_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_events
    ADD CONSTRAINT processed_events_pkey PRIMARY KEY (tenant_id, event_id);


--
-- Name: replay_jobs replay_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.replay_jobs
    ADD CONSTRAINT replay_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: security_events security_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tickets tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_pkey PRIMARY KEY (id);


--
-- Name: user_roles user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: agent_events_tenant_id_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_events_tenant_id_created_at_idx ON public.agent_events USING btree (tenant_id, created_at);


--
-- Name: agent_tasks_tenant_id_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_tasks_tenant_id_status_idx ON public.agent_tasks USING btree (tenant_id, status);


--
-- Name: aggregate_snapshots_latest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX aggregate_snapshots_latest_idx ON public.aggregate_snapshots USING btree (tenant_id, aggregate_type, aggregate_id, version DESC);


--
-- Name: aggregate_snapshots_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX aggregate_snapshots_ts_idx ON public.aggregate_snapshots USING btree (tenant_id, aggregate_type, aggregate_id, ts DESC);


--
-- Name: ai_agents_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ai_agents_name_key ON public.ai_agents USING btree (name);


--
-- Name: approvals_tenant_id_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX approvals_tenant_id_status_idx ON public.approvals USING btree (tenant_id, status);


--
-- Name: audit_logs_tenant_id_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_logs_tenant_id_created_at_idx ON public.audit_logs USING btree (tenant_id, created_at);


--
-- Name: customers_tenant_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX customers_tenant_id_idx ON public.customers USING btree (tenant_id);


--
-- Name: data_retention_policies_tenant_id_entity_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX data_retention_policies_tenant_id_entity_type_idx ON public.data_retention_policies USING btree (tenant_id, entity_type);


--
-- Name: data_retention_policies_tenant_id_entity_type_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX data_retention_policies_tenant_id_entity_type_key ON public.data_retention_policies USING btree (tenant_id, entity_type);


--
-- Name: deals_tenant_id_stage_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX deals_tenant_id_stage_idx ON public.deals USING btree (tenant_id, stage);


--
-- Name: domain_events_aggregate_type_aggregate_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX domain_events_aggregate_type_aggregate_id_idx ON public.domain_events USING btree (aggregate_type, aggregate_id);


--
-- Name: domain_events_tenant_id_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX domain_events_tenant_id_created_at_idx ON public.domain_events USING btree (tenant_id, created_at);


--
-- Name: event_log_tenant_aggregate_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_tenant_aggregate_ts_idx ON public.event_log USING btree (tenant_id, aggregate_type, aggregate_id, ts);


--
-- Name: event_log_tenant_aggregate_version_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_tenant_aggregate_version_idx ON public.event_log USING btree (tenant_id, aggregate_type, aggregate_id, version);


--
-- Name: event_log_tenant_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_tenant_ts_idx ON public.event_log USING btree (tenant_id, ts);


--
-- Name: events_stream_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX events_stream_lookup_idx ON public.events USING btree (tenant_id, stream_id, version);


--
-- Name: events_tenant_event_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX events_tenant_event_id_key ON public.events USING btree (tenant_id, event_id);


--
-- Name: events_tenant_idempotency_key_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX events_tenant_idempotency_key_uniq ON public.events USING btree (tenant_id, idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: events_tenant_stream_version_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX events_tenant_stream_version_key ON public.events USING btree (tenant_id, stream_id, version);


--
-- Name: idx_agent_decisions_tenant_agent_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_decisions_tenant_agent_time ON public.agent_decisions USING btree (tenant_id, agent_id, created_at DESC);


--
-- Name: idx_agent_decisions_tenant_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_decisions_tenant_approval ON public.agent_decisions USING btree (tenant_id, approval_id);


--
-- Name: idx_agent_decisions_tenant_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_decisions_tenant_time ON public.agent_decisions USING btree (tenant_id, created_at DESC);


--
-- Name: lead_read_model_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lead_read_model_status_idx ON public.lead_read_model USING btree (tenant_id, status);


--
-- Name: leads_tenant_id_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_tenant_id_status_idx ON public.leads USING btree (tenant_id, status);


--
-- Name: outbox_events_tenant_event_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX outbox_events_tenant_event_id_key ON public.outbox_events USING btree (tenant_id, event_id);


--
-- Name: outbox_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX outbox_pending_idx ON public.outbox_events USING btree (tenant_id, created_at) WHERE ((published_at IS NULL) AND (dead_lettered_at IS NULL));


--
-- Name: replay_jobs_tenant_aggregate_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX replay_jobs_tenant_aggregate_idx ON public.replay_jobs USING btree (tenant_id, aggregate_type, aggregate_id, started_at DESC);


--
-- Name: replay_jobs_tenant_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX replay_jobs_tenant_status_idx ON public.replay_jobs USING btree (tenant_id, status, started_at DESC);


--
-- Name: roles_tenant_id_name_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX roles_tenant_id_name_key ON public.roles USING btree (tenant_id, name);


--
-- Name: security_events_created_at_severity_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_events_created_at_severity_idx ON public.security_events USING btree (created_at, severity);


--
-- Name: tenants_slug_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX tenants_slug_key ON public.tenants USING btree (slug);


--
-- Name: tickets_tenant_id_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tickets_tenant_id_status_idx ON public.tickets USING btree (tenant_id, status);


--
-- Name: user_roles_tenant_id_user_id_role_id_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX user_roles_tenant_id_user_id_role_id_key ON public.user_roles USING btree (tenant_id, user_id, role_id);


--
-- Name: users_tenant_id_email_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX users_tenant_id_email_key ON public.users USING btree (tenant_id, email);


--
-- Name: agent_events agent_events_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.ai_agents(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: agent_events agent_events_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.agent_tasks(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: agent_events agent_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: agent_tasks agent_tasks_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tasks
    ADD CONSTRAINT agent_tasks_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.ai_agents(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: agent_tasks agent_tasks_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tasks
    ADD CONSTRAINT agent_tasks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: ai_memory ai_memory_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_memory
    ADD CONSTRAINT ai_memory_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.ai_agents(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: ai_memory ai_memory_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_memory
    ADD CONSTRAINT ai_memory_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: approvals approvals_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: approvals approvals_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: audit_logs audit_logs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: customers customers_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: customers customers_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: data_retention_policies data_retention_policies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_retention_policies
    ADD CONSTRAINT data_retention_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: deals deals_assigned_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_assigned_to_fkey FOREIGN KEY (assigned_to) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: deals deals_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: deals deals_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: deals deals_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: deals deals_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deals
    ADD CONSTRAINT deals_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: domain_events domain_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_events
    ADD CONSTRAINT domain_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: leads leads_assigned_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_assigned_to_fkey FOREIGN KEY (assigned_to) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: leads leads_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: leads leads_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: policies policies_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.policies
    ADD CONSTRAINT policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: roles roles_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: security_events security_events_resolved_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: security_events security_events_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: tickets tickets_assigned_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_assigned_to_fkey FOREIGN KEY (assigned_to) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: tickets tickets_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: tickets tickets_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customers(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: tickets tickets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: user_roles user_roles_assigned_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_assigned_by_fkey FOREIGN KEY (assigned_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: user_roles user_roles_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: user_roles user_roles_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: user_roles user_roles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: users users_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: agent_decisions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_decisions ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_decisions agent_decisions_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_decisions_tenant_isolation ON public.agent_decisions USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: agent_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_events ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_events agent_events_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_events_tenant_isolation ON public.agent_events USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: agent_tasks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_tasks ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_tasks agent_tasks_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_tasks_tenant_isolation ON public.agent_tasks USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: aggregate_snapshots; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.aggregate_snapshots ENABLE ROW LEVEL SECURITY;

--
-- Name: aggregate_snapshots aggregate_snapshots_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY aggregate_snapshots_tenant_isolation ON public.aggregate_snapshots USING ((tenant_id = (current_setting('app.tenant_id'::text))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text))::uuid));


--
-- Name: ai_memory; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_memory ENABLE ROW LEVEL SECURITY;

--
-- Name: ai_memory ai_memory_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY ai_memory_tenant_isolation ON public.ai_memory USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: approvals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.approvals ENABLE ROW LEVEL SECURITY;

--
-- Name: approvals approvals_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY approvals_tenant_isolation ON public.approvals USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: audit_logs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: audit_logs audit_logs_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY audit_logs_tenant_isolation ON public.audit_logs USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: customer_timeline_view; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.customer_timeline_view ENABLE ROW LEVEL SECURITY;

--
-- Name: customer_timeline_view customer_timeline_view_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY customer_timeline_view_tenant_isolation ON public.customer_timeline_view USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: customers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;

--
-- Name: customers customers_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY customers_tenant_isolation ON public.customers USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: deal_pipeline_view; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.deal_pipeline_view ENABLE ROW LEVEL SECURITY;

--
-- Name: deal_pipeline_view deal_pipeline_view_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY deal_pipeline_view_tenant_isolation ON public.deal_pipeline_view USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: deals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.deals ENABLE ROW LEVEL SECURITY;

--
-- Name: deals deals_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY deals_tenant_isolation ON public.deals USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: domain_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.domain_events ENABLE ROW LEVEL SECURITY;

--
-- Name: domain_events domain_events_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY domain_events_tenant_isolation ON public.domain_events USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: event_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.event_log ENABLE ROW LEVEL SECURITY;

--
-- Name: event_log event_log_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY event_log_tenant_isolation ON public.event_log USING ((tenant_id = (current_setting('app.tenant_id'::text))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text))::uuid));


--
-- Name: event_streams; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.event_streams ENABLE ROW LEVEL SECURITY;

--
-- Name: event_streams event_streams_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY event_streams_tenant_isolation ON public.event_streams USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;

--
-- Name: events events_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY events_tenant_isolation ON public.events USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: lead_read_model; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.lead_read_model ENABLE ROW LEVEL SECURITY;

--
-- Name: lead_read_model lead_read_model_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY lead_read_model_tenant_isolation ON public.lead_read_model USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: leads; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

--
-- Name: leads leads_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY leads_tenant_isolation ON public.leads USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: outbox_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox_events outbox_events_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY outbox_events_tenant_isolation ON public.outbox_events USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: policies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.policies ENABLE ROW LEVEL SECURITY;

--
-- Name: policies policies_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY policies_tenant_isolation ON public.policies USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: processed_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.processed_events ENABLE ROW LEVEL SECURITY;

--
-- Name: processed_events processed_events_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY processed_events_tenant_isolation ON public.processed_events USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: replay_jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.replay_jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: replay_jobs replay_jobs_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY replay_jobs_tenant_isolation ON public.replay_jobs USING ((tenant_id = (current_setting('app.tenant_id'::text))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.tenant_id'::text))::uuid));


--
-- Name: roles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.roles ENABLE ROW LEVEL SECURITY;

--
-- Name: roles roles_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY roles_tenant_isolation ON public.roles USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: security_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.security_events ENABLE ROW LEVEL SECURITY;

--
-- Name: security_events security_events_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY security_events_tenant_isolation ON public.security_events USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: tickets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tickets ENABLE ROW LEVEL SECURITY;

--
-- Name: tickets tickets_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tickets_tenant_isolation ON public.tickets USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: user_roles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_roles ENABLE ROW LEVEL SECURITY;

--
-- Name: user_roles user_roles_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY user_roles_tenant_isolation ON public.user_roles USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- Name: users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

--
-- Name: users users_tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY users_tenant_isolation ON public.users USING (((tenant_id)::text = current_setting('app.tenant_id'::text, true))) WITH CHECK (((tenant_id)::text = current_setting('app.tenant_id'::text, true)));


--
-- PostgreSQL database dump complete
--

\unrestrict S0XCVuVjovOX7fqQNq260QaNEsG0zsxP0yt6Y0AEpSULi0yUGkQ0qc2pS6oAq8u

