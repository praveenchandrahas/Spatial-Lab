import pytest

from spatial_lab.config import DEFAULTS
from spatial_lab.engine import Engine
from spatial_lab.layout import load_layout


@pytest.fixture
def layout():
    return load_layout(None)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SPATIAL_LAB_CONFIG", str(tmp_path / "config.yml"))
    monkeypatch.setenv("SPATIAL_LAB_DATA", str(tmp_path / "data"))
    c = dict(DEFAULTS)
    c["ceiling_db"] = -40.0
    return c


@pytest.fixture
def engine(layout, cfg):
    return Engine(layout, cfg)
