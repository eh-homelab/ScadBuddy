"""The pipeline API (spec 2026-09-27 §8.2, §10)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.workflows.models import MigrateResult
from scadbuddy.workflows.outputs import output_key

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

PIPELINE = (
    "INPUTS_VERSION = 1\n\ndef migrate(inputs, v):\n    return {**inputs, 'house': {}}\n\n"
    "async def run(ctx, inputs):\n    part = await ctx.render('model.scad', **inputs['params'])\n"
    "    await ctx.output(plates=await ctx.pack([part]),"
    " bom=[{'piece': 'p', 'label': 'P', 'count': 1}], files={'a.txt': 'hi'})\n"
)


def with_pipeline(paths: DataPaths, slug: str, source: str = PIPELINE) -> None:
    directory = paths.model_dir(slug)
    (directory / "pipeline").mkdir(exist_ok=True)
    (directory / "pipeline" / "pipeline.py").write_text(source, encoding="utf-8")
    meta = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    meta["pipeline"] = {"module": "pipeline/pipeline.py", "api": 1}
    paths.model_meta(slug).write_text(json.dumps(meta), encoding="utf-8")


def _done(client: TestClient, model: str, inputs: dict[str, object]) -> dict[str, object]:
    accepted = client.post(f"/api/v1/models/{model}/render", json={"inputs": inputs})
    assert accepted.status_code == 202, accepted.text
    url = accepted.json()["status_url"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(url).json()
        if job["status"] in {"done", "failed"}:
            assert job["status"] == "done", job
            return dict(job)
        time.sleep(0.05)
    raise AssertionError("the render did not finish")


def test_inputs_beyond_params_make_a_different_pipeline_job(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coalescing joins only a *pending* row, so hold every row pending: refuse the
    workflow starts, as `tests/api/test_temporal_path.py` does."""
    with_pipeline(paths, model)
    service = getattr(client.app.state, STATE_ATTR).render  # type: ignore[attr-defined]

    async def unavailable(*_: object, **__: object) -> None:
        raise RuntimeError("temporal is down")

    def submit(inputs: dict[str, object]) -> str:
        accepted = client.post(f"/api/v1/models/{model}/render", json={"inputs": inputs})
        assert accepted.status_code == 202, accepted.text
        return str(accepted.json()["job_id"])

    with monkeypatch.context() as patched:
        patched.setattr(service.client, "start_workflow", unavailable)
        one = submit({"params": {}, "v": 1, "house": {"cols": 1}})
        same = submit({"params": {}, "v": 1, "house": {"cols": 1}})
        other = submit({"params": {}, "v": 1, "house": {"cols": 2}})
    assert same == one  # coalescing is on: the test can fail
    assert other != one


def test_a_pipeline_template_takes_params_its_model_scad_lacks(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    with_pipeline(paths, model, PIPELINE.replace("**inputs['params']", ""))
    job = _done(client, model, {"params": {"not_in_model_scad": 1}, "v": 1})
    outputs = job["outputs"]
    assert isinstance(outputs, list)
    assert outputs[0]["bom"][0]["piece"] == "p"


def test_an_output_saves_its_bom_record_and_files(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    with_pipeline(paths, model)
    job = _done(client, model, {"params": {}, "v": 1})
    created = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job["id"], "index": 0})
    assert created.status_code in (200, 201), created.text
    detail = client.get(f"/api/v1/outputs/{created.json()['id']}").json()
    assert detail["bom"] == [{"piece": "p", "label": "P", "count": 1, "plates": [], "part": None}]
    assert detail["files"] == ["a.txt"]
    assert detail["record"]["pipeline_api"] == 1 and len(detail["record"]["pipeline_version"]) == 64
    assert detail["record"]["inputs_v"] == 1
    body = client.get(f"/api/v1/outputs/{created.json()['id']}/files/a.txt")
    assert body.status_code == 200 and body.text == "hi"
    assert (
        client.get(f"/api/v1/outputs/{created.json()['id']}/files/..%2Fmeta.json").status_code
        == 404
    )


def test_an_output_whose_files_left_the_store_is_a_409_and_writes_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The extra files' blob is gone (final review I2): a clean refusal before anything
    is copied, never a 500 with a half-written output directory."""
    with_pipeline(paths, model)
    job = _done(client, model, {"params": {}, "v": 1})
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    state.store.blobs.remove(output_key(str(job["id"]), 0))
    before = set((paths.outputs / model).glob("*")) if (paths.outputs / model).is_dir() else set()
    refused = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job["id"], "index": 0})
    assert refused.status_code == 409, refused.text
    assert "no longer in the store" in refused.json()["detail"]
    after = set((paths.outputs / model).glob("*")) if (paths.outputs / model).is_dir() else set()
    assert after == before


def test_an_output_index_the_job_does_not_have_is_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    with_pipeline(paths, model)
    job = _done(client, model, {"params": {}, "v": 1})
    response = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job["id"], "index": 3}
    )
    assert response.status_code == 422


def test_an_output_records_only_the_inputs_its_pipeline_job_rendered(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A pipeline job is keyed on its whole inputs (§3.4), so its output's recorded inputs
    must be the job's own, not only its `params` (§8.4)."""
    with_pipeline(paths, model)
    rendered = {"params": {}, "v": 1, "house": {"cols": 1}}
    job = _done(client, model, rendered)
    url = f"/api/v1/models/{model}/outputs"
    other = {"params": {}, "v": 1, "house": {"cols": 2}}
    refused = client.post(url, json={"job_id": job["id"], "inputs": other})
    assert refused.status_code == 422, refused.text
    assert "inputs are not the ones job" in refused.json()["detail"]
    accepted = client.post(url, json={"job_id": job["id"], "inputs": rendered})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["inputs"]["house"] == {"cols": 1}


def test_a_malformed_pipeline_declaration_reaches_the_worker(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Not the API's "unknown parameter": the job is accepted, and `load_pipeline` says
    what is wrong with the declaration."""
    with_pipeline(paths, model)
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["pipeline"]["api"] = 0
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")
    accepted = client.post(
        f"/api/v1/models/{model}/render",
        json={"inputs": {"params": {"not_in_model_scad": 1}, "v": 1}},
    )
    assert accepted.status_code == 202, accepted.text
    url = accepted.json()["status_url"]
    deadline = time.monotonic() + 60
    job: dict[str, object] = {}
    while time.monotonic() < deadline:
        job = client.get(url).json()
        if job["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "failed", job
    assert "model.json's pipeline is not valid" in str(job["error"])


def test_migrate_upgrades_old_inputs(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model)
    response = client.post(
        f"/api/v1/models/{model}/inputs/migrate", json={"inputs": {"params": {}, "v": 0}}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "inputs": {"params": {}, "house": {}, "v": 1},
        "from_version": 0,
        "to_version": 1,
    }


def test_migrate_refuses_newer_inputs_with_the_reason(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    with_pipeline(paths, model)
    response = client.post(
        f"/api/v1/models/{model}/inputs/migrate", json={"inputs": {"params": {}, "v": 9}}
    )
    assert response.status_code == 422
    assert "these inputs are v9" in response.json()["detail"]


def test_the_record_carries_the_inputs_version(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    with_pipeline(paths, model)
    assert client.get(f"/api/v1/models/{model}").json()["inputs_version"] == 1


def _spy_migrations(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the service's migration with one that records its calls."""
    calls: list[object] = []
    service = getattr(client.app.state, STATE_ATTR).render  # type: ignore[attr-defined]

    async def migrate(
        slug: str, inputs: dict[str, object], *, version: str | None
    ) -> MigrateResult:
        calls.append((slug, inputs, version))
        return MigrateResult(inputs={**inputs, "v": 1}, from_version=0, to_version=1)

    monkeypatch.setattr(service, "migrate_inputs", migrate)
    return calls


def test_migrate_for_an_unknown_template_is_a_404(client: TestClient) -> None:
    response = client.post("/api/v1/models/nope/inputs/migrate", json={"inputs": {"v": 0}})
    assert response.status_code == 404


def test_migrate_refuses_a_malformed_version(client: TestClient, model: str) -> None:
    response = client.post(
        f"/api/v1/models/{model}/inputs/migrate",
        json={"inputs": {"params": {}, "v": 0}, "version": "../other"},
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "version"]


def test_migrate_at_an_unknown_revision_is_a_404(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_pipeline(paths, model)
    calls = _spy_migrations(client, monkeypatch)
    response = client.post(
        f"/api/v1/models/{model}/inputs/migrate",
        json={"inputs": {"params": {}, "v": 0}, "version": "0" * 40},
    )
    assert response.status_code == 404
    assert calls == []


def test_migrate_resolves_a_short_revision(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post("/api/v1/models", json={"name": "Pasted", "source": "cube();\n"})
    assert created.status_code == 201, created.text
    full = client.get("/api/v1/models/pasted").json()["version"]
    # A pipeline, or the route answers identity without asking (below).
    with_pipeline(paths, "pasted")
    calls = _spy_migrations(client, monkeypatch)
    response = client.post(
        "/api/v1/models/pasted/inputs/migrate",
        json={"inputs": {"params": {}, "v": 0}, "version": full[:7]},
    )
    assert response.status_code == 200, response.text
    assert calls == [("pasted", {"params": {}, "v": 0}, full)]


@pytest.mark.parametrize("inputs", [{"params": {"width": 3}}, {"params": {}, "v": 0}])
def test_current_inputs_of_a_template_without_a_pipeline_come_back_unchanged(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch, inputs: dict[str, object]
) -> None:
    calls = _spy_migrations(client, monkeypatch)
    response = client.post(f"/api/v1/models/{model}/inputs/migrate", json={"inputs": inputs})
    assert response.status_code == 200, response.text
    assert response.json() == {"inputs": inputs, "from_version": 0, "to_version": 0}
    assert calls == []  # no workflow


def test_inputs_of_a_template_without_a_pipeline_are_identity_at_any_revision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to migrate with: no worker, whatever the revision or the inputs' `v`."""
    created = client.post("/api/v1/models", json={"name": "Pasted", "source": "cube();\n"})
    assert created.status_code == 201, created.text
    full = client.get("/api/v1/models/pasted").json()["version"]
    calls = _spy_migrations(client, monkeypatch)
    inputs = {"params": {"w": 1}, "v": 3}
    response = client.post(
        "/api/v1/models/pasted/inputs/migrate", json={"inputs": inputs, "version": full}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"inputs": inputs, "from_version": 3, "to_version": 3}
    assert calls == []


def test_current_inputs_of_a_pipeline_template_come_back_unchanged(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_pipeline(paths, model)
    calls = _spy_migrations(client, monkeypatch)
    inputs = {"params": {}, "v": 1, "house": {}}
    response = client.post(f"/api/v1/models/{model}/inputs/migrate", json={"inputs": inputs})
    assert response.status_code == 200, response.text
    assert response.json() == {"inputs": inputs, "from_version": 1, "to_version": 1}
    assert calls == []
