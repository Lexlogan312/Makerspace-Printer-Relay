"""relay.toml settings, and the Printer record."""

import dataclasses

import pytest

from relay.config import ConfigError, load_config
from tests.fixtures import PRINTER


def test_rejects_the_publishable_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    path = tmp_path / "relay.toml"
    path.write_text('[supabase]\nurl = "https://x.supabase.co"\nkey = "sb_publishable_abc"\n')
    with pytest.raises(ConfigError, match="publishable"):
        load_config(path)


def test_reads_printer_network_and_env_vars_override_the_file(tmp_path, monkeypatch):
    path = tmp_path / "relay.toml"
    path.write_text('[supabase]\nurl = "https://x.supabase.co"\nkey = "sb_secret_file"\n'
                    '[relay]\nprinter_network = "10.42.0.0/24"\n')
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_env")
    config = load_config(path)
    assert config.supabase_key == "sb_secret_env"
    assert str(config.printer_network) == "10.42.0.0/24"


def test_reserved_printers_stay_in_use_but_out_of_service_ones_do_not():
    use = {s: dataclasses.replace(PRINTER, maintenance_status=s).in_use
           for s in ("operational", "reserved", "maintenance", "offline_permanent")}
    assert use == {"operational": True, "reserved": True, "maintenance": False, "offline_permanent": False}
