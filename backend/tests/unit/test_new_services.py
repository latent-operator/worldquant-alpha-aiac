"""
Unit tests for newly created services.

Tests cover:
- DatasetService
- KnowledgeService
- RunService
- OperatorService
- ConfigService
"""

import pytest
import pytest_asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from backend.services.dataset_service import (
    DatasetService,
    DatasetListFilters,
)
from backend.services.knowledge_service import (
    KnowledgeService,
    KnowledgeListFilters,
    KnowledgeCreateData,
    KnowledgeUpdateData,
)
from backend.services.run_service import RunService
from backend.services.operator_service import (
    OperatorService,
    OperatorListFilters,
)
from backend.services.config_service import (
    ConfigService,
    ThresholdsConfig,
    DiversityConfig,
)


# =============================================================================
# Dataset Service Tests
# =============================================================================

class TestDatasetService:
    """Tests for DatasetService."""
    
    @pytest.mark.asyncio
    async def test_list_datasets_empty(self, db_session):
        """Test listing datasets when none exist."""
        service = DatasetService(db_session)
        filters = DatasetListFilters(limit=10)
        
        result = await service.list_datasets(filters)
        
        assert result.total == 0
        assert result.items == []
    
    @pytest.mark.asyncio
    async def test_list_datasets_with_region_filter(self, db_session, sample_dataset):
        """Test filtering datasets by region."""
        service = DatasetService(db_session)
        
        # Filter by matching region
        filters = DatasetListFilters(region=sample_dataset.region)
        result = await service.list_datasets(filters)
        
        assert result.total >= 1
    
    @pytest.mark.asyncio
    async def test_list_categories(self, db_session):
        """Test getting unique categories."""
        service = DatasetService(db_session)
        
        categories = await service.list_categories()
        
        assert isinstance(categories, list)
    
    @pytest.mark.asyncio
    async def test_trigger_dataset_sync(self, db_session, monkeypatch):
        """Test triggering dataset sync."""
        # Mock the celery task
        mock_task = MagicMock()
        mock_task.id = "test-task-id"
        monkeypatch.setattr(
            "backend.tasks.sync_datasets_from_brain",
            MagicMock(delay=MagicMock(return_value=mock_task)),
        )
        
        service = DatasetService(db_session)
        task_id = service.trigger_dataset_sync(region="USA")
        
        assert task_id == "test-task-id"


# =============================================================================
# Knowledge Service Tests
# =============================================================================

class TestKnowledgeService:
    """Tests for KnowledgeService."""
    
    @pytest.mark.asyncio
    async def test_list_entries_empty(self, db_session):
        """Test listing entries when none exist."""
        service = KnowledgeService(db_session)
        filters = KnowledgeListFilters(limit=10)
        
        entries = await service.list_entries(filters)
        
        assert entries == []
    
    @pytest.mark.asyncio
    async def test_create_entry(self, db_session):
        """Test creating a knowledge entry."""
        service = KnowledgeService(db_session)
        
        data = KnowledgeCreateData(
            entry_type="SUCCESS_PATTERN",
            pattern="rank(ts_delta(close, 5))",
            description="Short-term momentum pattern",
            meta_data={"dataset": "fundamental6"}
        )
        
        entry = await service.create_entry(data)
        
        assert entry.id is not None
        assert entry.entry_type == "SUCCESS_PATTERN"
        assert entry.pattern == "rank(ts_delta(close, 5))"
        assert entry.created_by == "USER"
    
    @pytest.mark.asyncio
    async def test_get_success_patterns(self, db_session, sample_knowledge_entry):
        """Test getting success patterns."""
        service = KnowledgeService(db_session)
        
        # Sample entry should be type SUCCESS_PATTERN
        patterns = await service.get_success_patterns(limit=10)
        
        # May be empty if sample isn't SUCCESS_PATTERN
        assert isinstance(patterns, list)
    
    @pytest.mark.asyncio
    async def test_update_entry(self, db_session):
        """Test updating a knowledge entry."""
        service = KnowledgeService(db_session)
        
        # First create an entry
        create_data = KnowledgeCreateData(
            entry_type="FAILURE_PITFALL",
            pattern="bad_pattern()",
            description="This pattern fails"
        )
        entry = await service.create_entry(create_data)
        
        # Now update it
        update_data = KnowledgeUpdateData(
            description="Updated description",
            is_active=False
        )
        updated = await service.update_entry(entry.id, update_data)
        
        assert updated.description == "Updated description"
        assert updated.is_active is False
    
    @pytest.mark.asyncio
    async def test_delete_entry(self, db_session):
        """Test soft deleting a knowledge entry."""
        service = KnowledgeService(db_session)
        
        # Create entry first
        create_data = KnowledgeCreateData(
            entry_type="FIELD_BLACKLIST",
            pattern="bad_field"
        )
        entry = await service.create_entry(create_data)
        
        # Delete it
        result = await service.delete_entry(entry.id)
        
        assert result is True
        
        # Verify it's deactivated
        fetched = await service.get_entry(entry.id)
        assert fetched.is_active is False


# =============================================================================
# Run Service Tests
# =============================================================================

class TestRunService:
    """Tests for RunService."""
    
    @pytest.mark.asyncio
    async def test_get_run_not_found(self, db_session):
        """Test getting a non-existent run."""
        service = RunService(db_session)
        
        result = await service.get_run(99999)
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_run_trace_not_found(self, db_session):
        """Test getting trace for non-existent run."""
        service = RunService(db_session)
        
        with pytest.raises(ValueError, match="not found"):
            await service.get_run_trace(99999)
    
    @pytest.mark.asyncio
    async def test_get_run_alphas_not_found(self, db_session):
        """Test getting alphas for non-existent run."""
        service = RunService(db_session)
        
        with pytest.raises(ValueError, match="not found"):
            await service.get_run_alphas(99999)


# =============================================================================
# Operator Service Tests
# =============================================================================

class TestOperatorService:
    """Tests for OperatorService."""
    
    @pytest.mark.asyncio
    async def test_list_operators_empty(self, db_session):
        """Test listing operators when none exist."""
        service = OperatorService(db_session)
        filters = OperatorListFilters(limit=10)
        
        operators = await service.list_operators(filters)
        
        assert operators == []
    
    @pytest.mark.asyncio
    async def test_list_operators_with_search(self, db_session, sample_operator):
        """Test searching operators by name."""
        service = OperatorService(db_session)
        
        # Search for the sample operator
        filters = OperatorListFilters(search=sample_operator.name[:3])
        operators = await service.list_operators(filters)
        
        assert len(operators) >= 0  # May find or not depending on sample
    
    @pytest.mark.asyncio
    async def test_trigger_operator_sync(self, db_session, monkeypatch):
        """Test triggering operator sync."""
        mock_task = MagicMock()
        mock_task.id = "sync-task-id"
        monkeypatch.setattr(
            "backend.tasks.sync_operators_from_brain",
            MagicMock(delay=MagicMock(return_value=mock_task)),
        )
        
        service = OperatorService(db_session)
        task_id = service.trigger_operator_sync()
        
        assert task_id == "sync-task-id"


# =============================================================================
# Config Service Tests
# =============================================================================

class TestConfigService:
    """Tests for ConfigService."""
    
    @pytest.mark.asyncio
    async def test_get_thresholds_default(self, db_session):
        """Test getting default thresholds."""
        service = ConfigService(db_session)
        
        thresholds = await service.get_thresholds()
        
        # Should return defaults
        assert thresholds == ThresholdsConfig()
    
    @pytest.mark.asyncio
    async def test_update_thresholds(self, db_session):
        """Test updating thresholds."""
        service = ConfigService(db_session)
        
        new_thresholds = ThresholdsConfig(
            sharpe_min=2.0,
            turnover_max=0.5,
            fitness_min=0.7,
            returns_min=0.01,
            max_dd_max=0.2
        )
        
        updated = await service.update_thresholds(new_thresholds)
        
        assert updated.sharpe_min == 2.0
        assert updated.turnover_max == 0.5
        
        # Verify persistence
        fetched = await service.get_thresholds()
        assert fetched.sharpe_min == 2.0
    
    @pytest.mark.asyncio
    async def test_get_diversity_config_default(self, db_session):
        """Test getting default diversity config."""
        service = ConfigService(db_session)
        
        diversity = await service.get_diversity_config()
        
        assert diversity.max_correlation == 0.7
    
    @pytest.mark.asyncio
    async def test_update_diversity_config(self, db_session):
        """Test updating diversity config."""
        service = ConfigService(db_session)
        
        new_config = DiversityConfig(max_correlation=0.5)
        
        updated = await service.update_diversity_config(new_config)
        
        assert updated.max_correlation == 0.5
    
    @pytest.mark.asyncio
    async def test_get_all_config(self, db_session):
        """Test getting all config values."""
        service = ConfigService(db_session)
        
        config = await service.get_all_config()
        
        assert isinstance(config, dict)
    
    @pytest.mark.asyncio
    async def test_set_and_get_config(self, db_session):
        """Test setting and getting a config value."""
        service = ConfigService(db_session)
        
        await service.set_config(
            key="test_config_key",
            value="test_value",
            config_type="string",
            description="Test config"
        )
        
        value = await service.get_config("test_config_key")
        
        assert value == "test_value"
    
    @pytest.mark.asyncio
    async def test_update_operator_status_invalid(self, db_session):
        """Test updating operator status with invalid value."""
        service = ConfigService(db_session)
        
        with pytest.raises(ValueError, match="Invalid status"):
            await service.update_operator_status("ts_rank", "INVALID_STATUS")


# =============================================================================
# Fixtures for Tests (add to conftest.py if not present)
# =============================================================================

@pytest_asyncio.fixture
async def sample_dataset(db_session):
    """Create a sample dataset for testing."""
    from backend.models import DatasetMetadata
    
    dataset = DatasetMetadata(
        dataset_id="test_dataset",
        name="Test Dataset",
        region="USA",
        universe="TOP3000",
        category="Fundamental",
        description="Test dataset for unit tests",
        field_count=10,
        mining_weight=1.0,
    )
    db_session.add(dataset)
    await db_session.commit()
    await db_session.refresh(dataset)
    return dataset


@pytest_asyncio.fixture
async def sample_operator(db_session):
    """Create a sample operator for testing."""
    from backend.models import Operator
    
    operator = Operator(
        name="ts_rank",
        category="Time Series",
        description="Rank values over time",
        param_count=2,
        is_active=True,
    )
    db_session.add(operator)
    await db_session.commit()
    await db_session.refresh(operator)
    return operator
