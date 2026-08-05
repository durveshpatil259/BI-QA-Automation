"""Domain layer: pure data models with no external dependencies.

These dataclasses describe *what* the system reasons about (projects,
datasources, dashboard metadata, validation findings, test cases, analysis
context/reports). They contain no I/O and no business logic beyond
(de)serialization, so they can be freely shared by storage, services and UI.
"""
