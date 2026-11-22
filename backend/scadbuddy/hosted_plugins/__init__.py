"""Hosted plugins (#6932, spec ``docs/superpowers/specs/2026-11-21-hosted-plugin-feeds-design.md``
§6.1 to §6.3): a Claude plugin written in ScadBuddy, as a draft of files, and published as
immutable numbered versions, each one deterministic zip in the blob store.

`rules` holds the names, paths and caps a draft takes; `bundle` the publish's checks, the
generated ``plugin.json``, the zip and its content hash (the agent's ``hash.ts``); `store`
the SQL over the ``ai_hosted_plugin*`` tables. The routes are ``api/hosted_plugins.py``
(the public ones) and ``api/crud_agent_packages.py`` (the agent's reads)."""
