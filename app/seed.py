from datetime import UTC, datetime, timedelta

from app.main import SessionLocal, User, Vault, passwords

with SessionLocal() as db:
    demo = db.query(User).filter_by(email="demo@example.test").first()
    if demo is None:
        demo = User(email="demo@example.test", password_hash=passwords.hash("demo-password-123"))
        db.add(demo)
        db.flush()
        db.add_all(
            [
                Vault(
                    user_id=demo.id,
                    name="Active goal",
                    target_amount=1000,
                    balance=125,
                    lock_until=datetime.now(UTC) + timedelta(days=30),
                    state="active",
                ),
                Vault(
                    user_id=demo.id,
                    name="Matured goal",
                    target_amount=500,
                    balance=500,
                    lock_until=datetime.now(UTC) - timedelta(days=1),
                    state="active",
                ),
                Vault(
                    user_id=demo.id,
                    name="Broken payout",
                    target_amount=300,
                    balance=300,
                    lock_until=datetime.now(UTC) - timedelta(days=2),
                    state="active",
                ),
            ]
        )
        db.commit()
print("Demo login: demo@example.test / demo-password-123")
