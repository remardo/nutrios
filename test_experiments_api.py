import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from admin.api import app, get_db
from admin.ab_service import get_ab_service
from admin.models import Base, Experiment, ExperimentRevision, ExperimentVariant


class DummyABService:
    def __init__(self) -> None:
        self.published: list[dict] = []
        self.paused: list[str] = []
        self.resumed: list[dict] = []

    def publish_experiment(self, experiment_key: str, rollout_percentage: float, variant_weights, preserve_sticky_assignments: bool = True):
        self.published.append(
            {
                "experiment_key": experiment_key,
                "rollout_percentage": rollout_percentage,
                "variant_weights": dict(variant_weights),
                "preserve_sticky_assignments": preserve_sticky_assignments,
            }
        )

    def pause_experiment(self, experiment_key: str):
        self.paused.append(experiment_key)

    def resume_experiment(self, experiment_key: str, rollout_percentage: float, variant_weights):
        self.resumed.append(
            {
                "experiment_key": experiment_key,
                "rollout_percentage": rollout_percentage,
                "variant_weights": dict(variant_weights),
            }
        )


@pytest.fixture()
def experiment_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    service = DummyABService()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_ab_service] = lambda: service

    with TestingSessionLocal() as session:
        session.add(Experiment(key="exp_signup", rollout_percentage=10.0))
        session.commit()

    with TestClient(app) as client:
        yield client, service, TestingSessionLocal

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_ab_service, None)


def _auth_headers(*roles: str) -> dict[str, str]:
    role_value = ",".join(roles)
    return {"x-api-key": "supersecret", "x-admin-roles": role_value}


def test_experiment_workflow_publish_pause_resume(experiment_client):
    client, service, SessionLocal = experiment_client

    headers = _auth_headers("experiments:write", "experiments:publish")

    # Invalid weights should be rejected.
    invalid_resp = client.put(
        "/experiments/exp_signup/config",
        json={
            "rollout_percentage": 20,
            "variants": [
                {"name": "control", "weight": 60},
                {"name": "test", "weight": 10},
            ],
        },
        headers=headers,
    )
    assert invalid_resp.status_code == 422

    # Update with valid weights (percentages that normalise to 1.0)
    update_resp = client.put(
        "/experiments/exp_signup/config",
        json={
            "rollout_percentage": 25,
            "variants": [
                {"name": "control", "weight": 60},
                {"name": "test", "weight": 40},
            ],
        },
        headers=headers,
    )
    assert update_resp.status_code == 200
    payload = update_resp.json()
    assert payload["experiment"]["rollout_percentage"] == 25.0
    weights = {v["name"]: v["weight"] for v in payload["experiment"]["variants"]}
    assert pytest.approx(sum(weights.values()), rel=1e-6) == 1.0
    assert pytest.approx(weights["control"], rel=1e-6) == 0.6
    assert pytest.approx(weights["test"], rel=1e-6) == 0.4

    with SessionLocal() as session:
        experiment = session.query(Experiment).filter_by(key="exp_signup").first()
        assert experiment.rollout_percentage == 25.0
        stored_weights = {v.name: v.weight for v in session.query(ExperimentVariant).filter_by(experiment_id=experiment.id)}
        assert pytest.approx(stored_weights["control"], rel=1e-6) == 0.6
        assert pytest.approx(stored_weights["test"], rel=1e-6) == 0.4

    publish_resp = client.post("/experiments/exp_signup/publish", headers=headers)
    assert publish_resp.status_code == 200
    publish_body = publish_resp.json()
    assert publish_body["experiment"]["status"] == "running"
    assert publish_body["revision"]["revision"] == 1
    assert service.published[-1]["experiment_key"] == "exp_signup"
    assert service.published[-1]["preserve_sticky_assignments"] is True

    with SessionLocal() as session:
        revision = session.query(ExperimentRevision).filter_by(experiment_id=experiment.id).first()
        assert revision is not None
        assert revision.status == "running"

    pause_resp = client.post("/experiments/exp_signup/pause", headers=headers)
    assert pause_resp.status_code == 200
    assert pause_resp.json()["experiment"]["status"] == "paused"
    assert service.paused[-1] == "exp_signup"

    resume_resp = client.post("/experiments/exp_signup/resume", headers=headers)
    assert resume_resp.status_code == 200
    assert resume_resp.json()["experiment"]["status"] == "running"
    assert service.resumed[-1]["experiment_key"] == "exp_signup"
    assert pytest.approx(service.resumed[-1]["variant_weights"]["control"], rel=1e-6) == 0.6


def test_experiment_requires_roles(experiment_client):
    client, _, _ = experiment_client

    resp = client.put(
        "/experiments/exp_signup/config",
        json={
            "rollout_percentage": 10,
            "variants": [{"name": "control", "weight": 1}],
        },
        headers={"x-api-key": "supersecret"},
    )
    assert resp.status_code == 403
