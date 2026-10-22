import pytest
from contextlib import asynccontextmanager
from importlib import import_module

from backend.services.credentials_service import (
    CredentialConfigurationError,
    CredentialKey,
    CredentialsService,
)


@pytest.mark.asyncio
async def test_writes_require_explicit_credentials_secret(db_session, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    from backend.config import settings
    monkeypatch.setattr(settings, "CREDENTIALS_SECRET", "")

    with pytest.raises(CredentialConfigurationError):
        await CredentialsService(db_session).set_credential(
            CredentialKey.OPENAI_API_KEY, "test-secret"
        )


@pytest.mark.asyncio
async def test_database_is_primary_and_status_reports_effective_source(db_session, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    service = CredentialsService(db_session)
    await service.set_credential(CredentialKey.OPENAI_API_KEY, "database-key")

    assert await service.get_credential(
        CredentialKey.OPENAI_API_KEY, fallback_env="OPENAI_API_KEY"
    ) == "database-key"
    status = await service.get_all_credentials_masked()
    assert status[CredentialKey.OPENAI_API_KEY]["source"] == "db"
    assert status[CredentialKey.OPENAI_API_KEY]["is_set"] is True
    assert "database-key" not in repr(status)


@pytest.mark.asyncio
async def test_save_and_delete_increment_shared_revision(db_session, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")
    service = CredentialsService(db_session)

    initial = await service.get_revision()
    await service.set_credential(CredentialKey.BRAIN_EMAIL, "user@example.test")
    after_save = await service.get_revision()
    assert after_save == initial + 1

    assert await service.delete_credential(CredentialKey.BRAIN_EMAIL) is True
    assert await service.get_revision() == after_save + 1


@pytest.mark.asyncio
async def test_llm_health_result_is_sanitized_and_persisted(db_session):
    service = CredentialsService(db_session)
    await service.record_connection_test(
        "llm", success=False, error="401 invalid api key secret-value"
    )

    status = await service.get_connection_test_status()
    assert status["llm"]["success"] is False
    assert status["llm"]["tested_at"]
    assert "secret-value" not in repr(status)


@pytest.mark.asyncio
async def test_llm_worker_refreshes_client_after_revision_change(db_session, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")
    service = CredentialsService(db_session)
    await service.set_credentials({
        CredentialKey.OPENAI_API_KEY: ("db-key-one", "test"),
        CredentialKey.OPENAI_BASE_URL: ("https://one.example/v1", "test"),
        CredentialKey.OPENAI_MODEL: ("model-one", "test"),
    })

    @asynccontextmanager
    async def session_factory():
        yield db_session

    import backend.database
    import backend.agents.services.llm_service as llm_module

    monkeypatch.setattr(backend.database, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(llm_module, "AsyncSessionLocal", session_factory, raising=False)

    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(llm_module.openai, "AsyncOpenAI", FakeClient)
    worker = llm_module.LLMService(api_key="initial", base_url="https://initial", model="initial")
    await worker._ensure_credentials_loaded()
    first_client = worker.client
    assert worker.api_key == "db-key-one"

    await service.set_credentials({
        CredentialKey.OPENAI_API_KEY: ("db-key-two", "test"),
        CredentialKey.OPENAI_BASE_URL: ("https://two.example/v1", "test"),
        CredentialKey.OPENAI_MODEL: ("model-two", "test"),
    })
    await worker._ensure_credentials_loaded()

    assert worker.api_key == "db-key-two"
    assert worker.client is not first_client
    assert first_client.closed is True


@pytest.mark.asyncio
async def test_brain_worker_refreshes_credentials_when_revision_changes(db_session, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")
    service = CredentialsService(db_session)
    await service.set_credentials({
        CredentialKey.BRAIN_EMAIL: ("first@example.test", "test"),
        CredentialKey.BRAIN_PASSWORD: ("first-password", "test"),
    })

    @asynccontextmanager
    async def session_factory():
        yield db_session

    import backend.database
    brain_module = import_module("backend.adapters.brain_adapter")

    monkeypatch.setattr(backend.database, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(brain_module, "AsyncSessionLocal", session_factory)
    adapter = brain_module.BrainAdapter()
    adapter.client = type("Client", (), {"cookies": type("Cookies", (), {"clear": lambda self: None})()})()
    invalidated = []

    class FakeRedis:
        async def delete(self, key):
            invalidated.append(key)

        async def aclose(self):
            pass

    async def get_redis():
        return FakeRedis()

    monkeypatch.setattr(adapter, "_get_redis", get_redis)
    await adapter._load_credentials_from_db()
    assert adapter.email == "first@example.test"

    await service.set_credentials({
        CredentialKey.BRAIN_EMAIL: ("second@example.test", "test"),
        CredentialKey.BRAIN_PASSWORD: ("second-password", "test"),
    })
    await adapter._load_credentials_from_db()

    assert adapter.email == "second@example.test"
    assert adapter.password == "second-password"
    assert invalidated == [adapter.REDIS_SESSION_KEY, adapter.REDIS_SESSION_KEY]


@pytest.mark.asyncio
async def test_credentials_status_api_never_returns_plaintext(db_session, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")
    await CredentialsService(db_session).set_credential(
        CredentialKey.OPENAI_API_KEY, "private-api-key-value"
    )
    async def worker_ready(_self):
        return {"backend_configured": True, "worker_configured": True, "worker_count": 1, "ready": True}
    monkeypatch.setattr(CredentialsService, "get_worker_encryption_status", worker_ready)
    router = import_module("backend.routers.config")

    response = await router.get_credentials_status(db_session)

    assert response["revision"] == 1
    assert response["credentials"][CredentialKey.OPENAI_API_KEY]["source"] == "db"
    assert response["encryption_configured"] is True
    assert response["encryption_status"]["worker_count"] == 1
    assert "private-api-key-value" not in repr(response)
    assert response["connection_tests"] == {}


@pytest.mark.asyncio
async def test_llm_save_api_rejects_missing_encryption_secret_atomically(db_session, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_SECRET", raising=False)
    monkeypatch.setattr("backend.config.settings.CREDENTIALS_SECRET", "")
    router = import_module("backend.routers.config")

    with pytest.raises(router.HTTPException) as exc:
        await router.set_llm_credentials(
            router.LLMCredentialsRequest(
                api_key="never-persisted",
                base_url="https://provider.invalid/v1",
                model="test-model",
            ),
            db_session,
        )

    assert exc.value.status_code == 503
    assert "never-persisted" not in str(exc.value.detail)
    assert await CredentialsService(db_session).get_revision() == 0


@pytest.mark.asyncio
async def test_save_api_rejects_worker_with_missing_or_different_key(db_session, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")

    async def worker_not_ready(_self):
        return {
            "backend_configured": True,
            "worker_configured": False,
            "worker_count": 1,
            "ready": False,
        }

    monkeypatch.setattr(CredentialsService, "get_worker_encryption_status", worker_not_ready)
    router = import_module("backend.routers.config")

    with pytest.raises(router.HTTPException) as exc:
        await router.set_llm_credentials(
            router.LLMCredentialsRequest(
                api_key="must-not-be-saved",
                base_url="https://provider.invalid/v1",
                model="test-model",
            ),
            db_session,
        )

    assert exc.value.status_code == 503
    assert "must-not-be-saved" not in str(exc.value.detail)
    assert await CredentialsService(db_session).get_revision() == 0


def test_worker_readiness_marker_never_contains_secret(monkeypatch):
    import backend.celery_app as celery_module

    monkeypatch.setenv("CREDENTIALS_SECRET", "unit-test-only-secret")
    monkeypatch.setattr(celery_module.settings, "CREDENTIALS_SECRET", "unit-test-only-secret")
    stored = {}

    class FakeRedis:
        def set(self, key, value):
            stored[key] = value

        def delete(self, key):
            stored.pop(key, None)

        def close(self):
            pass

    monkeypatch.setattr(celery_module.redis.Redis, "from_url", lambda *args, **kwargs: FakeRedis())
    celery_module._publish_worker_credentials_status("worker-test")

    marker = stored["aiac:credential-runtime:worker-test"]
    assert "unit-test-only-secret" not in marker
    assert '"configured": true' in marker
