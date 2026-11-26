"""The API on moto's S3, for the agent's end-to-end test of workspace blobs (#4789,
``agent/test/workspaceBlobs.backend.e2e.test.ts``): the agent stores real files through
the real hidden routes, in the database it names, beside its own tables.

``uv run --frozen python -m tests.support.workspace_backend`` (in ``backend/``) with
``SCADBUDDY_DATABASE_URL`` set: it starts a Temporal dev server (as the API tests do:
SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or ``temporal`` on PATH) and moto's S3, serves the app
on a free loopback port with ``store_backend`` s3 and a KEK of its own, and in front of it the
Go API (``tools/go/core``, #7667), which serves the workspace's metadata, deletes, reconcile
and blob holds and gives every other request to Python, as on prod (``go`` must be on PATH).
It prints ``READY <base url> <agent token>`` once ``/healthz`` answers through the Go API: the
token is an ``agent`` service credential, which the CRUD routes require.
``SCADBUDDY_TEST_WORKSPACE_KEK`` (base64), when set, is that KEK, so the test's own sealing
and the API's open each other's files (#5299 step 7).
It runs until it is signalled or its stdin closes; the test kills its process group."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn

from scadbuddy.auth.component import AUTH
from scadbuddy.auth.service_credentials import InProcessProvider
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.support.store import moto_s3
from tests.support.temporal import temporal_server

#: The repository's root, where ``go.work`` is.
REPO = Path(__file__).resolve().parents[3]
#: The areas the Go API answers here: the workspace routes the agent's test calls that are
#: Go's, and the blob holds.
CORE_AREAS = "workspace:sql,workspace-blob-refs"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _wait_healthy(base: str, alive: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 120
    while True:
        try:
            if httpx.get(f"{base}/healthz", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if not alive() or time.monotonic() > deadline:
            sys.exit(f"{base} did not start")
        time.sleep(0.5)


def _core(
    scratch: Path, database_url: str, upstream: str, kek: Path
) -> tuple[str, subprocess.Popen[bytes]]:
    """The Go API built and started in front of ``upstream``: its base URL and its process."""
    go = shutil.which("go")
    if go is None:
        sys.exit("go is not on PATH: the Go API serves the workspace's routes")
    binary = scratch / "scadbuddy-core"
    subprocess.run([go, "build", "-o", str(binary), "./tools/go/core"], cwd=REPO, check=True)
    port = _free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SCADBUDDY_DATABASE_URL": database_url,
        "SCADBUDDY_CORE_UPSTREAM": upstream,
        "SCADBUDDY_CORE_AREAS": CORE_AREAS,
        "SCADBUDDY_CORE_LISTEN": f"127.0.0.1:{port}",
        "SCADBUDDY_SECRET_KEY_FILE": str(kek),
    }
    return f"http://127.0.0.1:{port}", subprocess.Popen([str(binary)], env=env)


def main() -> None:
    database_url = os.environ["SCADBUDDY_DATABASE_URL"]
    with (
        tempfile.TemporaryDirectory(prefix="workspace-backend-") as scratch,
        temporal_server() as temporal,
        moto_s3() as s3,
    ):
        root = Path(scratch)
        kek = root / "kek"
        kek.write_text(
            os.environ.get("SCADBUDDY_TEST_WORKSPACE_KEK")
            or base64.b64encode(os.urandom(32)).decode()
        )
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
        app = create_app(settings)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _wait_healthy(base, thread.is_alive)
        front, core = _core(root, database_url, base, kek)
        _wait_healthy(front, lambda: core.poll() is None)
        credentials = app.state.scadbuddy.components.get(AUTH).credentials
        token = InProcessProvider("agent", lambda: credentials).token()
        if token is None:
            sys.exit("the API minted no agent credential")
        print(f"READY {front} {token}", flush=True)
        # Until the test closes stdin (or kills the group).
        sys.stdin.read()
        core.terminate()
        core.wait(timeout=30)
        server.should_exit = True
        thread.join(timeout=30)


if __name__ == "__main__":
    main()
