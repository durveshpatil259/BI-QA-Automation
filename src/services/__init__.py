"""Services layer: application/business logic.

Orchestrates the domain and storage layers to fulfil use cases (project
management, metadata extraction, datasource access, comparison, validation, LLM
reasoning, test-case generation, reporting). The strict rule of this product
lives here: Python performs ALL deterministic work and assembles a single
AnalysisContext BEFORE any LLM is invoked.

Modules are added incrementally, one per build step.
"""
