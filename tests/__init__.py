"""Marks the suite as a package.

Without this, pytest's default `prepend` import mode puts `tests/` itself on
sys.path instead of the project root, and the `from tests.conftest import ...`
lines in every test module fail to resolve. It also means conftest is imported
once, as `tests.conftest`, rather than twice under two different names.
"""
