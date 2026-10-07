from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, cast

import httpx
import jwt
from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./vault.db")
JWT_SECRET = os.getenv("JWT_SECRET", "local-only-demo-secret-change-before-deploy")
PAYSTACK_MODE = os.getenv("PAYSTACK_MODE", "mock")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "mock-secret")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Passwords:
    """Salted PBKDF2 password hashing using Python's standard library."""

    @staticmethod
    def hash(value: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, 310000)
        return salt.hex() + "$" + digest.hex()

    @staticmethod
    def verify(value: str, encoded: str) -> bool:
        try:
            salt_hex, digest_hex = encoded.split("$", 1)
            actual = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt_hex), 310000)
            return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
        except (ValueError, TypeError):
            return False


passwords = Passwords()
oauth = OAuth2PasswordBearer(tokenUrl="/auth/token")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    provider_customer_code: Mapped[str | None] = mapped_column(String, nullable=True)
    dva_account_number: Mapped[str | None] = mapped_column(String, nullable=True)
    dva_bank_name: Mapped[str | None] = mapped_column(String, nullable=True)


class Vault(Base):
    __tablename__ = "vaults"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal(0))
    lock_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String, default="active")
    milestones_reached: Mapped[list[str]] = mapped_column(JSON, default=list)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (UniqueConstraint("provider_reference", name="uq_ledger_provider_reference"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    vault_id: Mapped[str] = mapped_column(ForeignKey("vaults.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    kind: Mapped[str] = mapped_column(String)
    provider_reference: Mapped[str] = mapped_column(String)


class Provider:
    async def create_dva(self, user_id: str, email: str, db: Session) -> dict:
        raise NotImplementedError

    async def transfer(self, reference: str, amount: Decimal) -> dict:
        raise NotImplementedError


class MockProvider(Provider):
    """Deterministic local provider; `fail:` references simulate failed transfers."""

    async def create_dva(self, user_id: str, email: str, db: Session) -> dict:
        return {
            "account_number": "999" + user_id.replace("-", "")[:7],
            "bank_name": "Sandbox Bank",
            "email": email,
        }

    async def transfer(self, reference: str, amount: Decimal) -> dict:
        return {
            "status": "failed" if reference.startswith("fail:") else "success",
            "reference": reference,
            "amount": str(amount),
        }


class PaystackProvider(Provider):
    """HTTPX Paystack adapter. Transfers require a preconfigured recipient code."""

    def __init__(self):
        if not os.getenv("PAYSTACK_SECRET_KEY"):
            raise RuntimeError("PAYSTACK_SECRET_KEY required in real mode")
        self.secret = os.environ["PAYSTACK_SECRET_KEY"]

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        async with httpx.AsyncClient(
            base_url="https://api.paystack.co",
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"},
            timeout=15,
        ) as client:
            response = await client.request(method, path, json=payload)
        body = response.json()
        if not response.is_success or not body.get("status"):
            raise RuntimeError(f"Paystack request failed: {body.get('message', 'unknown error')}")
        return body["data"]

    async def create_dva(self, user_id: str, email: str, db: Session) -> dict:
        user = db.get(User, user_id)
        if user and user.dva_account_number:
            return {"account_number": user.dva_account_number, "bank_name": user.dva_bank_name}
        if user and not user.provider_customer_code:
            customer = await self._request("POST", "/customer", {"email": email})
            user.provider_customer_code = customer["customer_code"]
            db.commit()
        if not user or not user.provider_customer_code:
            raise RuntimeError("Customer provisioning failed")
        dva = await self._request(
            "POST",
            "/dedicated_account",
            {
                "customer": user.provider_customer_code,
                "preferred_bank": os.getenv("PAYSTACK_PREFERRED_BANK", "wema-bank"),
            },
        )
        user.dva_account_number = dva["account_number"]
        user.dva_bank_name = dva.get("bank", {}).get("name")
        db.commit()
        return dva

    async def transfer(self, reference: str, amount: Decimal) -> dict:
        recipient = os.getenv("PAYSTACK_RECIPIENT_CODE")
        if not recipient:
            raise RuntimeError("PAYSTACK_RECIPIENT_CODE must identify a verified recipient")
        data = await self._request(
            "POST",
            "/transfer",
            {
                "source": "balance",
                "amount": int(amount * 100),
                "recipient": recipient,
                "reference": reference,
                "reason": "Matured vault payout",
            },
        )
        return {
            "status": data.get("status"),
            "reference": data.get("reference"),
            "amount": str(amount),
        }


def provider() -> Provider:
    return MockProvider() if PAYSTACK_MODE == "mock" else PaystackProvider()


def db_session():
    with SessionLocal() as session:
        yield session


def current_user(
    token: Annotated[str, Depends(oauth)], db: Annotated[Session, Depends(db_session)]
) -> User:
    try:
        subject = jwt.decode(token, JWT_SECRET, algorithms=["HS256"]).get("sub")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid access token")
    user = db.get(User, subject)
    if not user:
        raise HTTPException(401, "User no longer exists")
    return user


app = FastAPI(
    title="Savings Vault Ledger API",
    version="1.0.0",
    description="Standalone demo of a webhook-driven savings ledger with idempotent credits and guarded maturity payouts.",
)


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=10)


class VaultInput(BaseModel):
    name: str
    target_amount: Decimal = Field(gt=0)
    lock_days: int = Field(ge=1, le=3650)


class DepositInput(BaseModel):
    vault_id: str
    amount: Decimal = Field(gt=0)
    reference: str


def token_for(user: User) -> str:
    return jwt.encode(
        {"sub": user.id, "exp": datetime.now(UTC) + timedelta(hours=2)},
        JWT_SECRET,
        algorithm="HS256",
    )


@app.post(
    "/auth/register",
    description="Create an account with an salted PBKDF2 password hash; returns a short-lived JWT.",
)
def register(payload: Credentials, db: Annotated[Session, Depends(db_session)]):
    email = payload.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(email=email, password_hash=passwords.hash(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": token_for(user), "token_type": "bearer"}


@app.post("/auth/token", description="Exchange email/password for a signed JWT bearer token.")
def login(payload: Credentials, db: Annotated[Session, Depends(db_session)]):
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    if not user or not passwords.verify(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {"access_token": token_for(user), "token_type": "bearer"}


@app.post("/vaults", description="Create a vault owned by the authenticated user.")
def create_vault(
    payload: VaultInput,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(db_session)],
):
    vault = Vault(
        user_id=user.id,
        name=payload.name,
        target_amount=payload.target_amount,
        lock_until=datetime.now(UTC) + timedelta(days=payload.lock_days),
        state="active",
    )
    db.add(vault)
    db.commit()
    db.refresh(vault)
    return {"id": vault.id, "state": vault.state}


@app.get("/vaults", description="List only the authenticated user's vaults.")
def list_vaults(
    user: Annotated[User, Depends(current_user)], db: Annotated[Session, Depends(db_session)]
):
    return [
        {"id": v.id, "name": v.name, "balance": str(v.balance), "state": v.state}
        for v in db.scalars(select(Vault).where(Vault.user_id == user.id))
    ]


@app.get("/transactions", description="List only ledger entries owned by the authenticated user.")
def list_transactions(
    user: Annotated[User, Depends(current_user)], db: Annotated[Session, Depends(db_session)]
):
    return [
        {
            "id": row.id,
            "vault_id": row.vault_id,
            "amount": str(row.amount),
            "kind": row.kind,
            "reference": row.provider_reference,
        }
        for row in db.scalars(select(LedgerEntry).where(LedgerEntry.user_id == user.id))
    ]


@app.get(
    "/transactions/{entry_id}",
    description="Retrieve a ledger entry only when it belongs to the authenticated user.",
)
def get_transaction(
    entry_id: str,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(db_session)],
):
    row = db.scalar(
        select(LedgerEntry).where(LedgerEntry.id == entry_id, LedgerEntry.user_id == user.id)
    )
    if not row:
        raise HTTPException(404, "Transaction not found")
    return {
        "id": row.id,
        "vault_id": row.vault_id,
        "amount": str(row.amount),
        "kind": row.kind,
        "reference": row.provider_reference,
    }


@app.post(
    "/provider/dva",
    description="Provision a sandbox or live virtual account for the authenticated user.",
)
async def dva(
    user: Annotated[User, Depends(current_user)], db: Annotated[Session, Depends(db_session)]
):
    return await provider().create_dva(user.id, user.email, db)


@app.post(
    "/provider/mock/charge",
    description="Simulate a signed charge.success event through the same webhook path used by a provider.",
)
async def mock_charge(
    payload: DepositInput,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(db_session)],
):
    if PAYSTACK_MODE != "mock":
        raise HTTPException(404, "Mock provider is disabled")
    vault = db.get(Vault, payload.vault_id)
    if not vault or vault.user_id != user.id:
        raise HTTPException(404, "Vault not found")
    import json

    event = {
        "event": "charge.success",
        "data": {
            "reference": payload.reference,
            "amount": int(payload.amount * 100),
            "metadata": {"vault_id": vault.id, "user_id": user.id},
        },
    }
    raw = json.dumps(event, separators=(",", ":")).encode()
    signature = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw, hashlib.sha512).hexdigest()
    return await webhook(raw, db, signature)


@app.post(
    "/webhooks/paystack",
    description="Verify HMAC-SHA512 over raw bytes before JSON parsing, then credit a unique provider reference.",
)
async def webhook(
    raw: Annotated[bytes, Body()],
    db: Annotated[Session, Depends(db_session)],
    x_paystack_signature: Annotated[str | None, Header()] = None,
):
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), raw, hashlib.sha512).hexdigest()
    if not x_paystack_signature or not hmac.compare_digest(expected, x_paystack_signature):
        raise HTTPException(401, "Invalid signature")
    import json

    event = json.loads(raw)
    if event.get("event") != "charge.success":
        return {"accepted": True, "ignored": True}
    data = event["data"]
    reference = data["reference"]
    vault_id = data["metadata"]["vault_id"]
    user_id = data["metadata"]["user_id"]
    amount = Decimal(data["amount"]) / 100
    if db.scalar(select(LedgerEntry.id).where(LedgerEntry.provider_reference == reference)):
        raise HTTPException(409, "Duplicate provider reference")
    vault = db.get(Vault, vault_id)
    if not vault or vault.user_id != user_id:
        raise HTTPException(404, "Vault not found")
    db.add(
        LedgerEntry(
            vault_id=vault_id,
            user_id=user_id,
            amount=amount,
            kind="deposit",
            provider_reference=reference,
        )
    )
    vault.balance += amount
    fraction = Decimal(vault.balance) / Decimal(vault.target_amount)
    reached = set(vault.milestones_reached or [])
    for percent in (25, 50, 75, 100):
        if fraction >= Decimal(percent) / 100:
            reached.add(f"{percent}%")
    vault.milestones_reached = sorted(reached, key=lambda item: int(item[:-1]))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate provider reference")
    return {"accepted": True, "credited": str(amount), "milestones": vault.milestones_reached}


@app.post(
    "/vaults/{vault_id}/mature",
    description="Atomically claim a matured vault before calling the payout provider, preventing concurrent double payouts.",
)
async def mature(
    vault_id: str,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(db_session)],
):
    now = datetime.now(UTC)
    claimed = cast(
        CursorResult[Any],
        db.execute(
            update(Vault)
            .where(
                Vault.id == vault_id,
                Vault.user_id == user.id,
                Vault.state == "active",
                Vault.lock_until <= now,
            )
            .values(state="payout_initiated")
        ),
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Vault is not payable or already claimed")
    vault = db.get(Vault, vault_id)
    if vault is None:
        db.rollback()
        raise HTTPException(404, "Vault not found")
    db.commit()  # release DB connection/transaction before network I/O
    reference = (
        "fail:"
        if vault.name.lower().startswith("broken") and PAYSTACK_MODE == "mock"
        else "payout:"
    ) + vault.id
    result = await provider().transfer(reference, vault.balance)
    with SessionLocal() as update_db:
        update_db.execute(
            update(Vault)
            .where(Vault.id == vault.id, Vault.state == "payout_initiated")
            .values(state="withdrawn" if result["status"] == "success" else "payout_failed")
        )
        update_db.add(
            LedgerEntry(
                vault_id=vault.id,
                user_id=user.id,
                amount=vault.balance,
                kind="withdrawal" if result["status"] == "success" else "payout_failure",
                provider_reference=result["reference"],
            )
        )
        update_db.commit()
    return result
