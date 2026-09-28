"""Print analyzers and fixers (#284; AI spec §11).

Roslyn-style checks over the print request #84 already builds: each analyzer has an
id, a severity, a category and cited sources, and reports diagnostics with evidence.
A fixer proposes a concrete diff against the base, expressed as the Bambuddy object
it would land in (AI spec §11). Decisions (accept, ignore, suppress) are stored per
scope and resolved narrowest-first.

Script-mode only: these run deterministically, with or without AI. Agent-mode
analyzers, ``checks.yaml`` and CEL conditions are later stories of #284.
"""
