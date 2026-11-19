import uuid

from dataharbor_api.auth import hash_token
from dataharbor_api.models import ApiToken, User


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def add_user(client, email: str) -> tuple[uuid.UUID, str]:
    with client.app.state.database.new_session() as session:
        user = User(email=email, display_name=email.split("@")[0])
        session.add(user)
        session.flush()
        token = f"dh_test_{uuid.uuid4().hex}"
        session.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="test"))
        session.commit()
        return user.id, token
