import pytest

from src.services.update_check import (
    UpdateChecker, is_newer, _parse_version, _strip_v,
)


def test_strip_v():
    assert _strip_v("v0.3.28") == "0.3.28"
    assert _strip_v("0.3.28") == "0.3.28"


def test_parse_version():
    assert _parse_version("0.3.28") == (0, 3, 28)
    assert _parse_version("1.0.0-beta") == (1, 0, 0)
    assert _parse_version("garbage") == (0, 0, 0)


def test_is_newer():
    assert is_newer("0.3.28", "0.3.0")
    assert is_newer("1.0.0", "0.99.99")
    assert not is_newer("0.3.0", "0.3.0")
    assert not is_newer("0.3.0", "0.3.28")


def test_snapshot_shape_before_check():
    c = UpdateChecker(current_version="0.3.0")
    snap = c.snapshot()
    assert snap["version"] == "0.3.0"
    assert snap["latest_version"] is None
    assert snap["has_update"] is False
    assert snap["release_url"] is None
    assert snap["release_published_at"] is None
    assert snap["checked_at"] is None


def test_has_update_true_when_latest_newer():
    c = UpdateChecker(current_version="0.3.0")
    c.latest_version = "0.3.28"
    assert c.has_update is True


def test_has_update_false_when_same_or_older():
    c = UpdateChecker(current_version="0.3.28")
    c.latest_version = "0.3.28"
    assert c.has_update is False
    c.latest_version = "0.3.0"
    assert c.has_update is False
