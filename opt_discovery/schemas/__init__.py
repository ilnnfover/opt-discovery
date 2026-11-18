"""Standardized data contracts of the discovery pipeline."""

from .bottleneck import BOTTLENECK_LIBRARY, BottleneckInfo, BottleneckType, get_bottleneck
from .evidence import (
    LEVEL_CAP,
    ConfidenceLevel,
    DiagnosisCandidate,
    EvidenceItem,
    MissingEvidence,
)
from .metrics import PROVENANCE_WEIGHT, Metric, Provenance, weakest_provenance
from .operator import (
    SCHEMA_VERSION as OPERATOR_SCHEMA_VERSION,
)
from .operator import (
    OperatorRecord,
    OperatorSchemaError,
    OperatorTable,
    ParseMeta,
    ParseWarning,
    SourceRef,
    TaskType,
)
from .opportunity import (
    SCHEMA_VERSION as OPPORTUNITY_SCHEMA_VERSION,
)
from .opportunity import (
    Opportunity,
    OpportunitySchemaError,
    OpportunityStatus,
    PriorityScore,
    ValidationPlan,
)
from .workload import (
    EnvironmentSpec,
    ExecutionMode,
    ExecutionSpec,
    GraphMode,
    HardwareSpec,
    InputSpec,
    ModelSpec,
    Phase,
    WorkloadError,
    WorkloadSpec,
)

__all__ = [
    "BOTTLENECK_LIBRARY",
    "BottleneckInfo",
    "BottleneckType",
    "get_bottleneck",
    "LEVEL_CAP",
    "ConfidenceLevel",
    "DiagnosisCandidate",
    "EvidenceItem",
    "MissingEvidence",
    "PROVENANCE_WEIGHT",
    "Metric",
    "Provenance",
    "weakest_provenance",
    "OPERATOR_SCHEMA_VERSION",
    "OPPORTUNITY_SCHEMA_VERSION",
    "OperatorRecord",
    "OperatorSchemaError",
    "OperatorTable",
    "ParseMeta",
    "ParseWarning",
    "SourceRef",
    "TaskType",
    "Opportunity",
    "OpportunitySchemaError",
    "OpportunityStatus",
    "PriorityScore",
    "ValidationPlan",
    "EnvironmentSpec",
    "ExecutionMode",
    "ExecutionSpec",
    "GraphMode",
    "HardwareSpec",
    "InputSpec",
    "ModelSpec",
    "Phase",
    "WorkloadError",
    "WorkloadSpec",
]
