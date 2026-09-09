"""Individual Scanner adapters. Each module here implements the Scanner
protocol from gitscribe.validation.analysis.scanner for exactly one
external tool - see validation_layer.md section 7 for each tool's
canonical responsibility. Register new adapters in
gitscribe.validation.analysis.registry.build_default_registry(), not by
importing them elsewhere.
"""
