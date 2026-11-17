"""Per-client context layer: ingestion pipeline + retrieval.

Business facts come from here, never from the model. Ingestion turns raw sources
(website copy, FAQ, company config, sample replies) into a structured
`ClientContext`. Retrieval selects the subset relevant to one inbound message and
decides whether an answer would be *grounded* in an approved FAQ.
"""

from sms_engine.context.ingestion import ContextIngestor, RawClientSources
from sms_engine.context.retrieval import ContextRetriever

__all__ = ["ContextIngestor", "RawClientSources", "ContextRetriever"]
