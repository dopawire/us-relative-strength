"""Shared test guards."""
import pytest

import refresh_universe


@pytest.fixture(autouse=True)
def _isolated_universe_files(tmp_path, monkeypatch):
    """Never let a test read or write the real master / change logs / Yahoo
    profile cache."""
    for name in ("MASTER", "CHANGES", "DELISTED", "PROFILE_CACHE", "YAHOO_MAP"):
        monkeypatch.setattr(refresh_universe, name, str(tmp_path / f"{name.lower()}.csv"))
