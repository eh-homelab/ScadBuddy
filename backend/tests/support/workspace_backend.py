"""The API on moto's S3, for the agent's end-to-end test of workspace blobs (#4789,
``agent/test/workspaceBlobs.backend.e2e.test.ts``): the agent stores real files through
the real hidden routes, in the database it names, beside its own tables.

``uv run --frozen python -m tests.support.workspace_backend`` (in ``backend/``) with
``SCADBUDDY_DATABASE_URL`` set: it starts a Temporal dev server (as the API tests do:
SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or ``temporal`` on PATH) and moto's S3, serves the app
on a free loopback port with ``store_backend`` s3 and a KEK of its own, and prints
``READY <base url>`` once ``/healthz`` answers. It runs until it is signalled or its
stdin closes; the test kills its process group."""

from __future__ import annotations

import base64
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import uvicorn

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.support.store import moto_s3
from tests.support.temporal import temporal_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def main() -> None:
    database_url = os.environ["SCADBUDDY_DATABASE_URL"]
    with (
        tempfile.TemporaryDirectory(prefix="workspace-backend-") as scratch,
        temporal_server() as temporal,
        moto_s3() as s3,
    ):
        root = Path(scratch)
        kek = root / "kek"
        kek.write_text(base64.b64encode(os.urandom(32)).decode())
        (root / "seed").mkdir()
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        queue = f"ws-{uuid.uuid4().hex[:12]}"
        settings = Settings(
            openscad=str(root / "no-openscad"),
            data_dir=root / "data",
            seed_models_dir=root / "seed",
            frontend_dir=Path("/nonexistent"),
            database_url=database_url,
            temporal_address=temporal,
            temporal_namespace="default",
            temporal_task_queue_render=queue,
            temporal_task_queue_bambuddy=f"{queue}-bambuddy",
            temporal_task_queue_library=f"{queue}-library",
            temporal_task_queue_projects=f"{queue}-projects",
            temporal_task_queue_slicer=f"{queue}-slicer",
            temporal_worker_inprocess=True,
            asset_sweep_interval=0,
            spool_overlay_refresh_seconds=0,
            preview_renders=False,
            store_backend="s3",
            store_s3_endpoint=s3.endpoint,
            store_s3_bucket=s3.bucket,
            store_s3_prefix=s3.prefix,
            store_s3_access_key_id=s3.access_key_id,
            store_s3_secret_access_key=s3.secret_access_key,
            secret_key_file=kek,
            api_internal_url=base,
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 120
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if not thread.is_alive() or time.monotonic() > deadline:
                sys.exit("the API did not start")
            time.sleep(0.5)
        print(f"READY {base}", flush=True)
        # Until the test closes stdin (or kills the group).
        sys.stdin.read()
        server.should_exit = True
        thread.join(timeout=30)


if __name__ == "__main__":
    main()
