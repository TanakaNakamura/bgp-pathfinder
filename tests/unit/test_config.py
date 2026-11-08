"""Config parsing and validation."""

import pytest

from optimizer.config import ConfigError, load, parse
from tests.unit.helpers import LAB_CONFIG


def base_raw():
    return {
        "providers": [
            {"name": "isp-a", "probe_source": "10.99.1.1", "next_hop": "10.1.0.2"},
            {"name": "isp-b", "probe_source": "10.99.2.1", "next_hop": "10.2.0.2"},
        ],
        "base_provider": "isp-a",
        "prefixes": [{"prefix": "198.51.100.0/24", "targets": ["198.51.100.10"]}],
    }


def test_lab_config_loads():
    cfg = load(LAB_CONFIG)
    assert cfg.policy.base_provider == "isp-a"
    assert cfg.provider("isp-b").next_hop == "10.2.0.2"
    assert cfg.prefixes[0].targets == ("198.51.100.10",)
    assert cfg.probe.icmp_count == 5 and cfg.probe.tcp_port == 80
    assert cfg.bgp.communities == ("no-export", "no-advertise")
    assert cfg.metrics is not None and cfg.metrics.port == 9101


def test_defaults_are_safe():
    cfg = parse(base_raw())
    assert cfg.dry_run is True  # never announce unless asked to
    assert cfg.metrics is None  # no listening socket unless asked to


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.update(base_provider="isp-x"), "base_provider"),
        (lambda r: r["providers"].pop(), "two providers"),
        (lambda r: r["providers"][1].update(name="isp-a"), "unique"),
        (lambda r: r["providers"][0].update(probe_source="10.99.1"), "invalid IPv4"),
        (lambda r: r["prefixes"][0].update(targets=["192.0.2.1"]), "outside"),
        (lambda r: r["prefixes"][0].update(prefix="198.51.100.1/24"), "prefix"),
        (lambda r: r.update(decision={"confirm_round": 3}), "unknown keys"),
        (lambda r: r.update(decision={"return_ratio": 0}), "return_ratio"),
        (lambda r: r.update(probe={"icmp": {"cnt": 3}}), "unknown keys"),
        (lambda r: r.update(metrics={"port": 70000}), "metrics.port"),
        (lambda r: r.update(metrics={"listen": "any"}), "metrics.listen"),
    ],
)
def test_invalid_configs(mutate, message):
    raw = base_raw()
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse(raw)
