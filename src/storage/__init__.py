"""Storage layer: project-folder persistence.

Everything is stored on the local filesystem inside per-project folders (no
cloud, no database). This layer knows the on-disk layout and how to read/write
the JSON files and asset folders; it exposes repository-style APIs to the
services and UI layers.
"""
