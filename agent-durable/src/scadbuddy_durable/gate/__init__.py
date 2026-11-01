"""The tool-call gate (spec 2026-10-01 §6.6): the Python port of the agent's gate.

Ports the agent's ``src/gate/`` (ids, the validator, the handler names); the shared
vectors in ``agent/test/fixtures/pending-input-vectors.json`` pin the two together.
``store`` and ``activities`` are the gate's only writers of ``ai_pending_input`` and
``ai_input_responses``.
"""
