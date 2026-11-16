"""
Alpha Models - Alpha entities and related models

Contains Alpha, AlphaFailure, and AlphaPnl models.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class Alpha(SQLAlchemyBase):
    """
    Alpha - Represents a generated alpha expression with its metrics.
    
    This is the core entity for storing alpha expressions, their
    simulation results, and quality status.
    """
    __tablename__ = "alphas"
    __table_args__ = (
        UniqueConstraint('alpha_id', name='uq_alpha_id'),
        {'extend_existing': True}
    )
    
    id = Column(Integer, primary_key=True, index=True)
    alpha_id = Column(String(20), unique=True, index=True)
    type = Column(String(20), default="REGULAR")  # REGULAR, SUPER
    
    # Associations
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=True)
    trace_step_id = Column(Integer, ForeignKey("trace_steps.id"), nullable=True)
    template_id = Column(Integer, ForeignKey("templates.id"))
    run_id = Column(Integer, ForeignKey("experiment_runs.id"), nullable=True)
    
    # Core Info
    expression = Column(Text, nullable=False)
    expression_hash = Column(String(64))
    author = Column(String(50))
    name = Column(String(200))
    region = Column(String(10), nullable=False)
    universe = Column(String(50), nullable=False)
    dataset_id = Column(String(50), nullable=True)
    
    # Settings
    delay = Column(Integer, default=1)
    decay = Column(Integer, default=0)
    neutralization = Column(String(50), default="NONE")
    truncation = Column(Float, default=0.08)
    instrument_type = Column(String(20), default="EQUITY")
    
    # Status
    status = Column(String(20), default="created")  # created, simulated, submitted
    stage = Column(String(10), default="IS")  # IS, OS
    quality_status = Column(String(50), default="PENDING")
    
    # Metrics (Flattened)
    is_sharpe = Column(Float)
    is_turnover = Column(Float)
    is_fitness = Column(Float)
    is_returns = Column(Float)
    is_drawdown = Column(Float)
    is_margin = Column(Float)
    is_long_count = Column(Integer)
    is_short_count = Column(Integer)
    
    # Rich Metadata
    settings = Column(JSONB)
    tags = Column(ARRAY(String))
    checks = Column(JSONB)
    
    # Full Metrics Objects
    is_metrics = Column(JSONB)
    os_metrics = Column(JSONB)
    metrics = Column(JSONB, default={})
    
    # Dates
    date_created = Column(DateTime)
    date_modified = Column(DateTime)
    date_submitted = Column(DateTime)
    
    # Human Feedback
    human_feedback = Column(String(50), default="NONE")
    feedback_comment = Column(Text)
    
    # Context
    hypothesis = Column(Text)
    logic_explanation = Column(Text)
    fields_used = Column(JSONB, default=[])
    operators_used = Column(JSONB, default=[])
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    task = relationship("MiningTask", back_populates="alphas")
    trace_step = relationship("TraceStep", back_populates="alpha")


class AlphaFailure(SQLAlchemyBase):
    """
    Alpha Failure - Records failed alpha attempts for the feedback loop.
    
    Used by Feedback Agent to learn and improve.
    """
    __tablename__ = "alpha_failures"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=True)
    trace_step_id = Column(Integer, ForeignKey("trace_steps.id"), nullable=True)
    run_id = Column(Integer, ForeignKey("experiment_runs.id"), nullable=True)
    
    expression = Column(Text, nullable=True)
    error_type = Column(String(100), nullable=True)  # SYNTAX_ERROR, FIELD_NOT_FOUND, TIMEOUT
    error_message = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)
    
    # For feedback analysis
    is_analyzed = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AlphaPnl(SQLAlchemyBase):
    """
    Alpha PnL - Daily PnL records for an alpha.
    """
    __tablename__ = "alpha_pnl"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    alpha_id = Column(Integer, index=True)
    trade_date = Column(DateTime, nullable=False)
    pnl = Column(Float)
    cumulative_pnl = Column(Float)
    created_at = Column(DateTime, server_default=func.now())
