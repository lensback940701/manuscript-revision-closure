"""Standalone runtime for Manuscript Revision Closure."""

__version__ = "0.6.1"

from . import assessor as _assessor
from .runtime_repair import install_runtime_repair

install_runtime_repair(_assessor)

AnalysisResult = _assessor.AnalysisResult
RunOptions = _assessor.RunOptions
analyze_manuscript = _assessor.analyze_manuscript

__all__ = ["AnalysisResult", "RunOptions", "analyze_manuscript"]
