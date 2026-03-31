"""Shared pytest configuration and markers."""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests requiring live governance container + executor")
