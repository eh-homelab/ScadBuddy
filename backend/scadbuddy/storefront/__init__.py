"""The storefront edge (#5282, spec 2026-11-02 §5.1, §10): the public site's process.

It holds no database secret, no KEK, no store key and no Temporal client. It serves the
page and signs the visitor cookie, then calls the CRUD service with a service credential.
"""
