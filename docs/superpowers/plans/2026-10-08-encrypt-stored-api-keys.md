# Encrypt stored API keys (#602) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans.

**Goal:** the backend's secret settings (`bambuddy_api_key`, `bambuddy_render_api_key`,
and `google_fonts_api_key`, i.e. `core/settings.py` `SECRET_FIELDS`) are envelope-encrypted
at rest in the `settings` table under the KEK in `SCADBUDDY_SECRET_KEY_FILE`, in the
agent's sealed format (`agent/src/secrets.ts`).

**Architecture:** `scadbuddy/core/secrets.py` is a Python reading of `secrets.ts`
(seal and open, v2 written, v1 opened; `Kek`, `Envelope`, key file loading), modelled on
`agent-durable/src/scadbuddy_durable/secrets.py`. `library/settings_store.py` seals a
secret on write as the jsonb object `{"sealed": {"secret", "dek", "kek_id"}}` (base64),
with AAD `settings:<name>`, and opens it on every read (`snapshot`,
`load_render_store_settings`). No SQL migration: the column is already jsonb. At start
(`SettingsStore.open`), under the migration advisory lock, every secret row that is still
a plain string is sealed; a second run finds none.

**Without the key file** nothing changes: rows stay (and are written) as plain strings, and
`open` logs one warning. A sealed row that cannot be opened (no key, another key, altered)
is skipped with a warning naming only the setting, so the field follows the environment,
as a refused row does today. A set but unreadable or malformed key file fails the start.

**New setting:** `secret_key_file: Path | None` (bootstrap: the key cannot live in the
database it protects).

## Tasks

1. `tests/test_secrets.py` (red), then `core/secrets.py`: round trip; AAD binds the
   setting (another context fails); wrong key reports both ids; a vector sealed by
   `agent/src/secrets.ts` with fixed bytes opens and is reproduced byte for byte;
   key-file errors name the variable, never the path or contents; `repr` hides keys.
2. `tests/test_settings_secrets.py` (Postgres, red), then the store: a saved key is
   sealed in the row and loads back; the render settings open it; plaintext rows are
   sealed at `open` and a second `open` changes nothing; without a key the row stays
   plaintext and one warning is logged; a sealed row with no key or another key is
   skipped with a warning and no secret in any log record.
3. `secret_key_file` in `Settings` / `BOOTSTRAP_FIELDS`; `repr=False` on the secret
   fields.
4. README "Deploying": the key file for the API, the render worker and the print worker,
   mounted together; a follow-up issue for the clusters change.
