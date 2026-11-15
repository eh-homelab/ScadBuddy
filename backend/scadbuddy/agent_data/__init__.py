"""The agent's data behind the CRUD service (#5299, #5342): its settings, its Claude
credentials and its payload data keys, read and written for ``api/crud_agent_secrets.py``.

The agent's tables (``ai_settings``, ``ai_credentials``, ``ai_payload_keys``,
``ai_forgotten_subjects``, ``ai_audit``) are the agent's migrations' own; each function
here is the SQL the agent ran itself before (``agent/src/credentials.ts``,
``agent/src/temporal/payloadCodec.ts``), as one transaction. The KEK opens and seals here
(D5): the agent gets a credential's plaintext or a subject's data key from a route for
that purpose, audited in ``ai_audit``, and never the KEK.
"""
