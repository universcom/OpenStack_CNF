"""cnf.osc.v1.policy — openstack cnf policy * commands."""
from __future__ import annotations

import json
from osc_lib.command import command


class ListPolicy(command.Lister):
    """List CNF scheduler policies.\n\n  openstack cnf policy list"""

    def get_parser(self, prog_name):
        return super().get_parser(prog_name)

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        # Policies endpoint to be added to REST API
        policies = client.get("/policies") if hasattr(client, "_policies") else []
        cols = ("id", "name", "enabled", "priority", "rule")
        return cols, [
            (p["id"], p["name"], p["enabled"], p["priority"], json.dumps(p["rule"]))
            for p in policies
        ]


class SetPolicy(command.ShowOne):
    """Create or update a scheduler policy.\n\n  openstack cnf policy set <name> --rule '{...}'"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("name", metavar="<name>")
        parser.add_argument("--rule", required=True, metavar="<json>",
                            help='JSON rule, e.g. \'{"trigger":"cpu_pct>80","action":"live_migrate","target":"least_loaded"}\'')
        parser.add_argument("--priority", type=int, default=100)
        parser.add_argument("--disable", action="store_true", default=False)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        body = {
            "name":     parsed_args.name,
            "rule":     json.loads(parsed_args.rule),
            "priority": parsed_args.priority,
            "enabled":  not parsed_args.disable,
        }
        result = client.post("/policies", json=body)
        return (
            ("id", "name", "enabled", "priority"),
            (result["id"], result["name"], result["enabled"], result["priority"]),
        )
