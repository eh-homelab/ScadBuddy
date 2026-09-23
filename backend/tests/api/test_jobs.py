from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import FAIL_WIDTH, wait_for_job


def test_render_is_accepted_and_the_job_completes(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status_url"] == f"/api/v1/jobs/{accepted['job_id']}"

    job = wait_for_job(client, accepted["job_id"])
    assert job["status"] == "done"
    assert job["slug"] == model
    assert job["params"] == {"width": 12}
    assert job["colors"] == ["#FF0000"]
    assert job["warnings"] == ["a warning"]
    assert job["bbox_mm"]["size"] == [10.0, 10.0, 5.0]
    assert job["parts"][0]["extruder"] == 1
    assert job["log_tail"] == ["rendered fine"]
    assert job["preview_url"] == f"/api/v1/jobs/{accepted['job_id']}/preview.glb"


def test_render_with_no_params_uses_the_model_defaults(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={})
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["job_id"])["params"] == {}


def test_an_unknown_parameter_is_rejected_by_name(client: TestClient, model: str) -> None:
    response = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": 1, "nope": 2, "also": 3}}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["parameters"] == ["also", "nope"]
    assert "also, nope" in body["detail"]
    assert response.headers["content-type"] == "application/problem+json"


def test_a_parameter_of_the_wrong_type_is_rejected(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": "wide"}})
    assert response.status_code == 422
    assert "expects a number" in response.json()["detail"]


def test_rendering_an_unknown_model_is_a_404(client: TestClient) -> None:
    assert client.post("/api/v1/models/missing/render", json={}).status_code == 404


def test_a_failed_render_carries_the_log_tail(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": FAIL_WIDTH}})
    job = wait_for_job(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"] == "openscad exited with 1"
    assert job["log_tail"] == ["ERROR: something broke"]
    assert job["preview_url"] is None
    assert job["bbox_mm"] is None


def test_the_preview_glb_is_served_with_the_gltf_media_type(client: TestClient, model: str) -> None:
    job_id = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 1}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)

    response = client.get(f"/api/v1/jobs/{job_id}/preview.glb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.content.startswith(b"glTF")


def test_a_failed_job_has_no_preview(client: TestClient, model: str) -> None:
    job_id = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": FAIL_WIDTH}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    assert client.get(f"/api/v1/jobs/{job_id}/preview.glb").status_code == 404


def test_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/api/v1/jobs/" + "0" * 32).status_code == 404
    assert client.get("/api/v1/jobs/not-a-job-id").status_code == 422
