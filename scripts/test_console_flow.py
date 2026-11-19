#!/usr/bin/env python3
"""Replay recorded console screens through ConsoleDriver. No lab hardware required."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pexpect  # noqa: E402

from ontap_console import PATTERNS, ConsoleDriver, ConsoleError  # noqa: E402


class FakeChild:
    def __init__(self, screens):
        self.screens = list(screens)
        self.sent = []
        self.before = ""
        self.match = None
        self.timeouts = []

    def expect(self, patterns, timeout=None):
        self.timeouts.append(timeout)
        if not self.screens:
            raise pexpect.EOF("no more screens")
        screen = self.screens.pop(0)
        for index, pattern in enumerate(patterns):
            match = re.search(pattern, screen)
            if match:
                self.before = screen[: match.start()]
                self.match = match
                return index
        raise AssertionError("no pattern matched %r" % screen)

    def sendline(self, text):
        self.sent.append(text)


def _cfg(**overrides):
    cfg = {
        "username": "admin",
        "admin_password": "Netapp123!",
        "node_login_password": "",
        "ssh_password": "",
        "node_mgmt_port": "e0M",
        "node_mgmt_ip": "192.0.2.11",
        "node_mgmt_netmask": "255.255.255.0",
        "node_mgmt_gateway": "192.0.2.1",
    }
    cfg.update(overrides)
    return cfg


def _run(screens, timeout=30, **overrides):
    child = FakeChild(screens)
    notes = []
    ConsoleDriver(child, _cfg(**overrides), log=notes.append).run(timeout)
    return child.sent, notes, child.timeouts


def test_loader_then_wizard():
    ip = "192.0.2.11"
    sent, notes, timeouts = _run(
        [
            "LOADER-A> ",
            "Booting ONTAP\nError: corrected ECC\nlogin: ",
            "Password: ",
            "Welcome to the cluster setup wizard.\nType yes to confirm and continue {yes}: ",
            "Enter the node management interface port [e0M]: ",
            "Enter the node management interface IP address: ",
            "Enter the node management interface netmask: ",
            "Enter the node management interface default gateway: ",
            "A node management interface has been created.\n"
            "Otherwise, press Enter to complete cluster setup using the command line interface:\n"
            "Do you want to create a new cluster or join an existing cluster? {create, join}: ",
            "login: ",
            "Password: ",
            "::> ",
            "vserver lif address\nnode node_mgmt %s\n::> " % ip,
            "Enter your current password: ",
            "Enter a new password: ",
            "Enter it again: ",
            "::> ",
        ],
        timeout=2000,
    )
    assert sent == [
        "boot_ontap",
        "admin",
        "",
        "yes",
        "e0M",
        ip,
        "255.255.255.0",
        "192.0.2.1",
        "exit",
        "admin",
        "",
        "network interface show -role node-mgmt -fields address",
        "security login password -username admin",
        "",
        "Netapp123!",
        "Netapp123!",
    ], sent
    assert "node management is ready" in notes
    # Expect after boot_ontap has to cover a full ONTAP boot.
    assert timeouts[1] == 900, timeouts


def test_already_configured_uses_existing_password():
    ip = "192.0.2.12"
    sent, notes, _timeouts = _run(
        [
            "login: ",
            "Password: ",
            "Login incorrect\n",
            "login: ",
            "Password: ",
            "labcluster::> ",
            "node node_mgmt %s\nlabcluster::> " % ip,
        ],
        node_mgmt_ip=ip,
        node_login_password="",
        admin_password="Netapp123!",
    )
    assert sent == [
        "admin",
        "",
        "admin",
        "Netapp123!",
        "network interface show -role node-mgmt -fields address",
    ], sent
    assert "node management is ready" in notes


def test_ssh_password_then_loader():
    sent, _notes, _timeouts = _run(
        [
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
            "console@lab's password: ",
            "LOADER> ",
            "login: ",
            "Password: ",
            "Type yes to confirm and continue {yes}: ",
            "Enter the node management interface port [e0M]: ",
            "Enter the node management interface IP address: ",
            "Enter the node management interface netmask: ",
            "Enter the node management interface default gateway: ",
            "Do you want to create a new cluster or join an existing cluster? {create, join}: ",
            "::> ",
            "address 192.0.2.11\n::> ",
            "Enter your current password: ",
            "Enter a new password: ",
            "Enter it again: ",
            "::> ",
        ],
        ssh_password="ssh-secret",
    )
    assert sent[0] == "yes"
    assert sent[1] == "ssh-secret"
    assert sent[2] == "boot_ontap"
    assert "ssh-secret" not in sent[3:]


def test_missing_ip_fails_instead_of_looping():
    child = FakeChild(
        [
            "::> ",
            "There are no entries matching your query.\n::> ",
            "Type yes to confirm and continue {yes}: ",
            "Enter the node management interface port [e0M]: ",
            "Enter the node management interface IP address: ",
            "Enter the node management interface netmask: ",
            "Enter the node management interface default gateway: ",
            "Do you want to create a new cluster or join an existing cluster? {create, join}: ",
            "::> ",
            "There are no entries matching your query.\n::> ",
            "Type yes to confirm and continue {yes}: ",
            "Enter the node management interface port [e0M]: ",
            "Enter the node management interface IP address: ",
            "Enter the node management interface netmask: ",
            "Enter the node management interface default gateway: ",
            "Do you want to create a new cluster or join an existing cluster? {create, join}: ",
            "::> ",
            "There are no entries matching your query.\n::> ",
        ]
    )
    try:
        ConsoleDriver(child, _cfg(), log=lambda _message: None).run(30)
    except ConsoleError as error:
        assert "was not configured" in str(error)
    else:
        raise AssertionError("expected a failure when node-mgmt never appears")


class IdleThenLoader:
    """First read has no output, which is a console already sitting at a prompt."""

    def __init__(self):
        self.sent = []
        self.before = ""
        self.match = None
        self.idle = True
        self.inner = FakeChild(["LOADER-A> "])

    def expect(self, patterns, timeout=None):
        if self.idle:
            self.idle = False
            self.before = ""
            raise pexpect.TIMEOUT("no output")
        return self.inner.expect(patterns, timeout=timeout)

    def sendline(self, text):
        self.sent.append(text)
        self.inner.sent.append(text)


def test_blank_console_is_poked_for_a_prompt():
    child = IdleThenLoader()
    try:
        ConsoleDriver(child, _cfg(), log=lambda _message: None).run(30)
    except ConsoleError:
        pass
    assert child.sent[0] == ""
    assert "boot_ontap" in child.sent


def test_patterns_compile():
    for name, pattern in PATTERNS:
        re.compile(pattern)


if __name__ == "__main__":
    test_patterns_compile()
    test_loader_then_wizard()
    test_already_configured_uses_existing_password()
    test_ssh_password_then_loader()
    test_blank_console_is_poked_for_a_prompt()
    test_missing_ip_fails_instead_of_looping()
    print("ok")
