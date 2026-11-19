#!/usr/bin/env python3
"""Drive an ONTAP console until the node has a node-mgmt IP and admin password.

na_ontap_cluster talks to a node over HTTPS. A factory node is sitting at
LOADER or in the cluster setup wizard and has no management IP, so the
module cannot log in yet. This script does only that prerequisite:

  LOADER          -> boot_ontap
  setup wizard    -> node-mgmt port, IP, netmask, gateway, then exit
  CLI             -> set the admin password if it is not already set

Cluster create and the node-2 join are left to na_ontap_cluster.
"""

import argparse
import os
import re
import sys

try:
    import pexpect
except ImportError:
    sys.stderr.write("pexpect is required: pip install -r requirements.txt\n")
    sys.exit(2)


# First matching pattern wins, so keep specific prompts ahead of generic ones.
PATTERNS = [
    ("hostkey", r"(?i)are you sure you want to continue connecting"),
    ("denied", r"(?i)permission denied"),
    ("login_bad", r"(?i)login incorrect"),
    ("password_current", r"(?im)^enter your current password:"),
    ("password_new", r"(?im)^enter a new password:"),
    ("password_retype", r"(?im)^enter it again:"),
    ("password", r"(?i)password:\s*$"),
    ("loader", r"(?im)^LOADER[-A-Z0-9]*>"),
    ("login", r"(?im)^login:"),
    ("autosupport", r"(?i)type yes to confirm and continue"),
    ("mgmt_port", r"(?i)node management interface port"),
    ("mgmt_ip", r"(?i)node management interface IP address"),
    ("mgmt_netmask", r"(?i)node management interface netmask"),
    ("mgmt_gateway", r"(?i)node management interface default gateway"),
    # create/join is the real prompt. The "press Enter" line is printed just
    # above it; matching the question consumes both so we only answer once.
    ("create_join", r"(?i)create a new cluster or join an existing cluster"),
    ("press_enter", r"(?i)press Enter to complete cluster setup"),
    ("single_node", r"(?i)single node cluster"),
    ("use_config", r"(?i)use this configuration"),
    ("wizard_rest", r"(?i)cluster administrator|enter the cluster name|additional license key|cluster management interface|DNS domain names|name server IP addresses|controller located|private cluster network"),
    ("cli", r"(?m)::\*?>\s*$"),
]

NEED_SHOW = "need_show"
NEED_PASSWORD = "need_password"


class ConsoleError(Exception):
    pass


def _ip_present(text, ip):
    return re.search(r"(?<![0-9])" + re.escape(ip) + r"(?![0-9])", text) is not None


class ConsoleDriver:
    """State machine over console prompts. `child` needs expect, sendline, before, and match."""

    def __init__(self, child, cfg, log=None):
        self.child = child
        self.cfg = cfg
        self.log = log or (lambda message: None)
        self.seen_ontap = False
        self.ssh_password_sent = False
        self.login_index = 0
        self.state = NEED_SHOW
        self.show_sent = False
        self.setup_attempts = 0
        self.password_command_sent = False
        self.waiting_for_boot = False
        self.redraws = 0
        self.exit_attempts = 0
        self.done = False
        self.login_candidates = [cfg["node_login_password"]]
        if cfg["admin_password"] not in self.login_candidates:
            self.login_candidates.append(cfg["admin_password"])
        self.working_password = cfg["node_login_password"]

    def run(self, overall_timeout):
        import time

        deadline = time.time() + overall_timeout
        compiled = [pattern for _name, pattern in PATTERNS]
        while not self.done:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ConsoleError("timed out waiting for the ONTAP console")
            step_timeout = min(remaining, 900 if self.waiting_for_boot else 180)
            self.waiting_for_boot = False
            try:
                index = self.child.expect(compiled, timeout=step_timeout)
            except pexpect.TIMEOUT:
                tail = getattr(self.child, "before", "") or ""
                # A prompt printed before we attached produces no new output.
                if not tail.strip() and self.redraws < 3:
                    self.redraws += 1
                    self._send("", "poking the console to redraw the prompt")
                    continue
                raise ConsoleError(
                    "console prompt not recognized. Last output:\n" + tail[-1500:]
                )
            except pexpect.EOF:
                tail = getattr(self.child, "before", "") or ""
                raise ConsoleError("console closed early. Last output:\n" + tail[-1500:])
            self._handle(PATTERNS[index][0])

    def _send(self, text, note):
        self.log(note)
        self.child.sendline("" if text is None else text)

    def _mark_ontap(self):
        self.seen_ontap = True

    def _handle(self, name):
        before = self.child.before or ""

        if name == "hostkey":
            self._send("yes", "accepting SSH host key")
            return
        if name == "denied":
            raise ConsoleError("console login was denied")
        if name == "login_bad":
            self.login_index += 1
            self.log("login rejected, trying the next password")
            return
        if name == "password_current":
            self._send(self.working_password, "sending current admin password")
            return
        if name == "password_new":
            self._send(self.cfg["admin_password"], "sending new admin password")
            return
        if name == "password_retype":
            self._send(self.cfg["admin_password"], "confirming admin password")
            return
        if name == "password":
            self._login_password()
            return
        if name == "loader":
            self._mark_ontap()
            self._send("boot_ontap", "LOADER prompt, sending boot_ontap")
            self.waiting_for_boot = True
            return
        if name == "login":
            self._mark_ontap()
            if self.login_index >= len(self.login_candidates):
                raise ConsoleError("could not log in as admin")
            self.working_password = self.login_candidates[self.login_index]
            self._send(self.cfg["username"], "logging in as admin")
            return
        if name == "autosupport":
            self._mark_ontap()
            self._send("yes", "acknowledging AutoSupport")
            return
        if name == "mgmt_port":
            self._mark_ontap()
            self._send(self.cfg["node_mgmt_port"], "setting node-mgmt port")
            return
        if name == "mgmt_ip":
            self._send(self.cfg["node_mgmt_ip"], "setting node-mgmt IP")
            return
        if name == "mgmt_netmask":
            self._send(self.cfg["node_mgmt_netmask"], "setting node-mgmt netmask")
            return
        if name == "mgmt_gateway":
            self._send(self.cfg["node_mgmt_gateway"], "setting node-mgmt gateway")
            return
        if name in ("press_enter", "create_join", "single_node", "use_config", "wizard_rest"):
            # Node-mgmt is already saved. Create runs over REST afterwards.
            self.exit_attempts += 1
            if self.exit_attempts > 3:
                raise ConsoleError(
                    "the setup wizard did not accept exit after node-mgmt was set"
                )
            self._mark_ontap()
            self.state = NEED_SHOW
            self.show_sent = False
            self._send("exit", "leaving the setup wizard after node-mgmt")
            return
        if name == "cli":
            self._mark_ontap()
            self._cli(before)
            return
        raise ConsoleError("unhandled console prompt %s" % name)

    def _login_password(self):
        if (
            not self.seen_ontap
            and self.cfg.get("ssh_password")
            and not self.ssh_password_sent
        ):
            self.ssh_password_sent = True
            self._send(self.cfg["ssh_password"], "sending console SSH password")
            return
        if self.login_index >= len(self.login_candidates):
            raise ConsoleError("could not log in as admin")
        self.working_password = self.login_candidates[self.login_index]
        self._send(self.working_password, "sending ONTAP login password")

    def _cli(self, before):
        if self.state == NEED_PASSWORD:
            if not self.password_command_sent:
                self.password_command_sent = True
                self._send(
                    "security login password -username %s" % self.cfg["username"],
                    "setting the admin password",
                )
                return
            if re.search(r"(?i)\berror:", before):
                raise ConsoleError("admin password change failed:\n" + before[-1500:])
            self.done = True
            self.log("node management is ready")
            return

        if not self.show_sent:
            self.show_sent = True
            self.state = NEED_SHOW
            self._send(
                "network interface show -role node-mgmt -fields address",
                "checking node-mgmt address",
            )
            return

        self.show_sent = False
        if _ip_present(before, self.cfg["node_mgmt_ip"]):
            self.log("node-mgmt IP %s is present" % self.cfg["node_mgmt_ip"])
            if self.working_password == self.cfg["admin_password"]:
                self.done = True
                self.log("node management is ready")
                return
            self.state = NEED_PASSWORD
            self._cli(before)
            return

        self.setup_attempts += 1
        if self.setup_attempts > 2:
            raise ConsoleError(
                "node-mgmt IP %s was not configured. Last output:\n%s"
                % (self.cfg["node_mgmt_ip"], before[-1500:])
            )
        self._send("cluster setup", "starting the cluster setup wizard")


def _env(name):
    return os.environ.get(name, "")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True, help="command that opens the console")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--node-mgmt-port", required=True)
    parser.add_argument("--node-mgmt-ip", required=True)
    parser.add_argument("--node-mgmt-netmask", required=True)
    parser.add_argument("--node-mgmt-gateway", required=True)
    parser.add_argument(
        "--timeout",
        type=int,
        default=1500,
        help="seconds to wait for boot plus the wizard",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = {
        "username": args.username,
        "admin_password": _env("ONTAP_ADMIN_PASSWORD"),
        "node_login_password": _env("ONTAP_LOGIN_PASSWORD"),
        "ssh_password": _env("ONTAP_SSH_PASSWORD"),
        "node_mgmt_port": args.node_mgmt_port,
        "node_mgmt_ip": args.node_mgmt_ip,
        "node_mgmt_netmask": args.node_mgmt_netmask,
        "node_mgmt_gateway": args.node_mgmt_gateway,
    }
    if not cfg["admin_password"]:
        sys.stderr.write("ONTAP_ADMIN_PASSWORD is not set\n")
        return 2

    def log(message):
        sys.stderr.write("ACTION: %s\n" % message)
        sys.stderr.flush()

    child = pexpect.spawn(
        args.command,
        encoding="utf-8",
        codec_errors="replace",
        timeout=180,
    )
    # Serial consoles drop characters if we type at full speed.
    child.delaybeforesend = 0.3
    child.logfile_read = sys.stderr
    try:
        ConsoleDriver(child, cfg, log=log).run(args.timeout)
    except (ConsoleError, pexpect.ExceptionPexpect) as error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 1
    finally:
        if child.isalive():
            child.close(force=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
