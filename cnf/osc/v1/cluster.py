"""cnf.osc.v1.cluster — openstack cnf cluster * commands."""
from __future__ import annotations

from osc_lib.command import command
from osc_lib import utils


class ListCluster(command.Lister):
    """List all CNF-registered clusters.\n\n  openstack cnf cluster list"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        clusters = client.get("/clusters")
        cols = ("id", "name", "role", "status", "grpc_addr", "bgp_as")
        return cols, [
            (c["id"], c["name"], c["role"], c["status"], c["grpc_addr"], c.get("bgp_as"))
            for c in clusters
        ]


class ShowCluster(command.ShowOne):
    """Show details of a CNF cluster.\n\n  openstack cnf cluster show <cluster>"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("cluster", metavar="<cluster-id>")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        c = client.get(f"/clusters/{parsed_args.cluster}")
        return (
            ("id", "name", "auth_url", "region", "role", "status", "grpc_addr", "bgp_as"),
            (c["id"], c["name"], c["auth_url"], c["region"],
             c["role"], c["status"], c["grpc_addr"], c.get("bgp_as")),
        )


class ClusterMetrics(command.ShowOne):
    """Show latest resource metrics for a cluster.\n\n  openstack cnf cluster metrics <cluster>"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("cluster", metavar="<cluster-id>")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        m = client.get(f"/clusters/{parsed_args.cluster}/metrics")
        return (
            ("cluster_id", "cpu_used_pct", "ram_used_pct", "disk_used_pct", "vm_count", "collected_at"),
            (m["cluster_id"], m["cpu_used_pct"], m["ram_used_pct"],
             m["disk_used_pct"], m["vm_count"], m["collected_at"]),
        )


class RegisterCluster(command.ShowOne):
    """Register a new cluster with CNF.\n\n  openstack cnf cluster register <name> --auth-url URL --grpc-addr ADDR"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("name", metavar="<name>")
        parser.add_argument("--auth-url", required=True, metavar="<url>")
        parser.add_argument("--grpc-addr", required=True, metavar="<host:port>")
        parser.add_argument("--region", default="RegionOne", metavar="<region>")
        parser.add_argument("--bgp-as", type=int, metavar="<asn>")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        body = {
            "name":      parsed_args.name,
            "auth_url":  parsed_args.auth_url,
            "grpc_addr": parsed_args.grpc_addr,
            "region":    parsed_args.region,
        }
        if parsed_args.bgp_as:
            body["bgp_as"] = parsed_args.bgp_as
        c = client.post("/clusters", json=body)
        return (
            ("id", "name", "role", "status", "grpc_addr"),
            (c["id"], c["name"], c["role"], c["status"], c["grpc_addr"]),
        )
