import hashlib
import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
main.Base.metadata.create_all(engine)
Factory = sessionmaker(engine, expire_on_commit=False)


def override_db():
    with Factory() as db:
        yield db


main.app.dependency_overrides[main.db_session] = override_db
client = TestClient(main.app)


def account(email):
    r = client.post("/auth/register", json={"email": email, "password": "correct-horse-123"})
    assert r.status_code == 200
    return r.json()["access_token"]


def auth(token):
    return {"Authorization": "Bearer " + token}


def vault(token, name="goal"):
    return client.post(
        "/vaults", headers=auth(token), json={"name": name, "target_amount": "100", "lock_days": 10}
    ).json()["id"]


def test_duplicate_webhook_is_rejected_without_double_credit():
    token = account("dupe@example.test")
    user_id = main.jwt.decode(token, main.JWT_SECRET, algorithms=["HS256"])["sub"]
    vid = vault(token)
    body = json.dumps(
        {
            "event": "charge.success",
            "data": {
                "reference": "evt-once",
                "amount": 2500,
                "metadata": {"vault_id": vid, "user_id": user_id},
            },
        },
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(main.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    headers = {"x-paystack-signature": sig}
    assert client.post("/webhooks/paystack", content=body, headers=headers).status_code == 200
    assert client.post("/webhooks/paystack", content=body, headers=headers).status_code == 409
    assert client.get("/vaults", headers=auth(token)).json()[0]["balance"] == "25.00"


def test_bad_signature_is_rejected_before_json_parse():
    response = client.post(
        "/webhooks/paystack", content=b"not-json", headers={"x-paystack-signature": "0"}
    )
    assert response.status_code == 401


def test_user_cannot_read_or_mature_another_users_vault():
    owner = account("owner@example.test")
    stranger = account("stranger@example.test")
    vid = vault(owner)
    assert client.get("/vaults", headers=auth(stranger)).json() == []
    response = client.post(f"/vaults/{vid}/mature", headers=auth(stranger))
    assert response.status_code == 409


def test_concurrent_maturity_claim_calls_provider_once(monkeypatch):
    token = account("race@example.test")
    vid = vault(token)
    with Factory() as db:
        row = db.get(main.Vault, vid)
        row.lock_until = datetime.now(UTC) - timedelta(days=1)
        db.commit()
    entered = threading.Event()
    release = threading.Event()

    class SlowProvider(main.Provider):
        async def transfer(self, reference, amount):
            entered.set()
            release.wait(2)
            return {"status": "success", "reference": reference}

        async def create_dva(self, user_id, email):
            return {}

    monkeypatch.setattr(main, "provider", lambda: SlowProvider())
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, f"/vaults/{vid}/mature", headers=auth(token))
        assert entered.wait(2)
        second = client.post(f"/vaults/{vid}/mature", headers=auth(token))
        release.set()
        first_result = first.result()
    assert second.status_code == 409
    assert first_result.status_code == 200


def test_mock_transfer_failure_is_deterministic():
    token = account("broken@example.test")
    vid = vault(token, "Broken payout")
    with Factory() as db:
        row = db.get(main.Vault, vid)
        row.lock_until = datetime.now(UTC) - timedelta(days=1)
        db.commit()
    response = client.post(f"/vaults/{vid}/mature", headers=auth(token))
    assert response.status_code == 200
    assert response.json()["status"] == "failed", response.json()
