"""A flow run's images (#2477), ``ai_flow_blobs``: the statements the run page lists and
reads them with (``flows/images.py``). The agent's routes that keep and read them are Go's
(``tools/go/core/internal/forget``, #7667). The agent seals each image under the run's payload
key in the context ``flow-blob:<subject>:<name>`` (``flows/images.py`` ``blob_context``).
"""

from __future__ import annotations

import re

BLOB_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$")

LIST = (
    "SELECT name, media_type, size FROM ai_flow_blobs WHERE subject = %s ORDER BY created_at, name"
)
GET = "SELECT media_type, data FROM ai_flow_blobs WHERE subject = %s AND name = %s"
