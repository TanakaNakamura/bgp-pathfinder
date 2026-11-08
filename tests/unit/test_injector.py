"""BGP adapters: command syntax, live and dry-run injectors, ExaBGP message parsing."""

import io
import json
import logging

import pytest

from optimizer.bgp import (
    Ack,
    DryRunInjector,
    ExaBgpCommands,
    ExaBgpInjector,
    ExaBgpMessageParser,
    NeighborState,
    Shutdown,
    Unparsed,
)

PREFIX = "198.51.100.0/24"
ANNOUNCE = "announce route 198.51.100.0/24 next-hop 10.2.0.2 local-preference 300 community [no-export no-advertise]"


def test_announce_command_matches_exabgp_syntax():
    assert ExaBgpCommands().announce(PREFIX, "10.2.0.2") == ANNOUNCE


def test_withdraw_command():
    assert ExaBgpCommands().withdraw(PREFIX) == "withdraw route 198.51.100.0/24"


def test_neighbor_scope_and_no_communities():
    cmds = ExaBgpCommands(local_preference=250, communities=(), neighbor="10.9.0.1")
    assert cmds.announce(PREFIX, "10.1.0.2") == (
        "neighbor 10.9.0.1 announce route 198.51.100.0/24 next-hop 10.1.0.2 local-preference 250"
    )
    assert cmds.withdraw(PREFIX) == "neighbor 10.9.0.1 withdraw route 198.51.100.0/24"


def test_live_injector_writes_lines_and_matches_acks(caplog):
    out = io.StringIO()
    inj = ExaBgpInjector(out, ExaBgpCommands())
    assert not inj.dry_run
    inj.announce(PREFIX, "10.2.0.2")
    inj.withdraw(PREFIX)
    assert out.getvalue().splitlines() == [ANNOUNCE, "withdraw route 198.51.100.0/24"]
    with caplog.at_level(logging.ERROR, logger="injector"):
        inj.on_ack(True)
        inj.on_ack(False)
    assert inj.errors == 1
    assert caplog.records[-1].command == "withdraw route 198.51.100.0/24"


@pytest.mark.parametrize("make", [lambda: ExaBgpInjector(io.StringIO(), ExaBgpCommands()),
                                  lambda: DryRunInjector(ExaBgpCommands())])
def test_injectors_are_interchangeable(make):
    """Liskov: both return the same command for the same call."""
    inj = make()
    assert inj.announce(PREFIX, "10.2.0.2") == ANNOUNCE
    assert inj.withdraw(PREFIX) == "withdraw route 198.51.100.0/24"


def test_dry_run_injector_sends_nothing():
    inj = DryRunInjector(ExaBgpCommands())
    assert inj.dry_run
    assert inj.announce(PREFIX, "10.2.0.2") == ANNOUNCE  # nothing to write to: it has no output at all


@pytest.mark.parametrize(
    "line, expected",
    [
        ("done", Ack(True)),
        ("error", Ack(False)),
        ("shutdown", Shutdown()),
        (json.dumps({"type": "notification", "notification": "shutdown"}), Shutdown()),
        (
            json.dumps({"type": "state", "neighbor": {"address": {"peer": "10.9.0.1"}, "state": "down",
                                                      "reason": "peer reset"}}),
            NeighborState("10.9.0.1", "down", "peer reset"),
        ),
        (json.dumps({"type": "update"}), None),
        ("", None),
        ("garbage", Unparsed("garbage")),
        ("[1, 2]", Unparsed("[1, 2]")),
    ],
)
def test_exabgp_message_parser(line, expected):
    assert ExaBgpMessageParser().parse(line) == expected
