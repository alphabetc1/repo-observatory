"""Optional analysis contract. No provider or network call is enabled by default."""
from dataclasses import asdict, dataclass
import importlib
import json
from typing import Protocol


@dataclass(frozen=True)
class AnalysisRequest:
    entry_id: str
    title: str
    body: str
    source_updated_at: str
    language: str


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    rationale: str
    suggested_priority: str
    provider: str
    model: str
    version: str


class Analyzer(Protocol):
    def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...


def load_analyzer(spec):
    if not spec:
        return None
    module, separator, name = spec.partition(':')
    if not separator or not module or not name:
        raise ValueError('analyzer must be a trusted local module:factory')
    return getattr(importlib.import_module(module), name)()


def analyze_entry(entry, analyzer, language='en'):
    if analyzer is None:
        return None
    request = AnalysisRequest(entry['id'], entry['title'], entry['body'], entry['updated_at'], language)
    result = analyzer.analyze(request)
    if not isinstance(result, AnalysisResult):
        raise ValueError('Analyzer must return AnalysisResult')
    data = asdict(result)
    if any(not isinstance(v, str) or not v.strip() for v in data.values()):
        raise ValueError('Analysis fields must be nonempty strings')
    if result.suggested_priority not in ('P0', 'P1', 'P2', 'P3'):
        raise ValueError('Invalid suggested priority')
    if len(json.dumps(data)) > 32000:
        raise ValueError('Analysis exceeds size limit')
    return {**data, 'source_updated_at': request.source_updated_at, 'language': language}
