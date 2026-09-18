"""Data-source setup contracts and credential persistence."""

from __future__ import annotations

from types import SimpleNamespace

from rigger.core.config import read_env_value
from rigger.core.plugin import DataPlugin, DataProviderField, DataProviderSpec
from rigger.services import configure_data_provider, remove_market, save_market


class LicensedSource(DataPlugin):
    name = "licensed"
    provider_spec = DataProviderSpec(
        label="Licensed source",
        fields=(
            DataProviderField("endpoint", "Endpoint", required=True),
            DataProviderField("api_key", "API key", required=True, secret=True, env_var="FT_API_KEY"),
        ),
    )


class FakeRig:
    def __init__(self) -> None:
        self.plugins = {"licensed": LicensedSource()}
        self.cfg = SimpleNamespace(plugins={})
        self.reloaded = 0

    def reload_data_sources(self) -> None:
        self.reloaded += 1


def test_licensed_source_persists_key_only_in_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rig = FakeRig()
    save_market("lse", label="London", currency="GBP", yahoo_suffix=".L")
    configure_data_provider(
        rig,
        "licensed",
        {"endpoint": "https://api.example.test/v1", "api_key": "secret-value"},
        markets=["lse"],
    )
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "https://api.example.test/v1" in text
    assert "secret-value" not in text
    assert 'markets = [\n    "lse",\n]' in text
    assert read_env_value("FT_API_KEY") == "secret-value"
    assert rig.reloaded == 1
    try:
        remove_market("lse")
    except ValueError as exc:
        assert "licensed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("referenced market was removed")


def test_licensed_source_rejects_missing_required_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    try:
        configure_data_provider(
            FakeRig(), "licensed", {"endpoint": "https://api.example.test", "api_key": ""}
        )
    except ValueError as exc:
        assert "API key is required" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing required key was accepted")
