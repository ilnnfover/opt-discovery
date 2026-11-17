"""Optimization Opportunity Discovery (MVP).

Pipeline: profiler export -> normalized OperatorTable -> rule-based bottleneck
diagnosis -> Opportunity list.  Every number carries provenance; missing data is
reported as missing instead of being filled with guesses.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
