# Current Limitations

This project deliberately documents limits rather than presenting planned work
as completed capability.

- The current deployment target is Docker Desktop. Kubernetes Helm assets exist,
  but a real staging cluster is not currently part of the verified path.
- Ollama is optional and disabled by default. A local model must be explicitly
  enabled and may require compatible GPU/runtime configuration.
- Several historical agent modules are coupled directly to Ollama integrations.
  H2 introduces a provider boundary first on the canonical demo path rather
  than claiming a complete provider abstraction today.
- A one-command deterministic end-to-end demo fixture is not implemented yet.
  Safe agent-run evidence and offline evaluation artifacts are available, but
  the structured-retrieval baseline remains intentionally narrower than
  semantic retrieval or answer-quality evaluation.
- CI provides strong configuration, security, and service checks, but it is not
  a substitute for a customer production SLO or a managed incident program.
- Generated local data, credentials, and model artifacts must remain outside
  version control.

These limits provide concrete interview discussion points: what is safe to ship
locally, what would change before production, and why the current scope remains
appropriate for a portfolio project.
