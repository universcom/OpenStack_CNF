"""cnf.osc.v1.vm — openstack cnf vm * commands."""
from __future__ import annotations

from osc_lib.command import command


class ListVM(command.Lister):
    """List VMs visible to CNF.\n\n  openstack cnf vm list [--cluster <id>]"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("--cluster", metavar="<cluster-id>", default=None)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        params = {}
        if parsed_args.cluster:
            params["cluster_id"] = parsed_args.cluster
        vms = client.get("/vms", params=params)
        cols = ("id", "name", "status", "host", "ips")
        return cols, [
            (v["id"], v["name"], v["status"], v["host"], ", ".join(v.get("ips", [])))
            for v in vms
        ]


class VMStatus(command.ShowOne):
    """Show VM details.\n\n  openstack cnf vm status <vm-id>"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("vm_id", metavar="<vm-id>")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        v = client.get(f"/vms/{parsed_args.vm_id}")
        return (
            ("id", "name", "status", "host", "flavor_id", "ips", "volumes"),
            (
                v["id"], v["name"], v["status"], v["host"], v["flavor_id"],
                ", ".join(v.get("ips", [])),
                ", ".join(v.get("volumes", [])),
            ),
        )


class MigrateVM(command.ShowOne):
    """Cold migrate a VM to another cluster.\n\n  openstack cnf vm migrate <vm-id> --to <cluster-id>"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("vm_id", metavar="<vm-id>")
        parser.add_argument("--to", required=True, dest="dest_cluster", metavar="<cluster-id>")
        parser.add_argument("--dest-host", default=None, metavar="<host>")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        body = {"dest_cluster_id": parsed_args.dest_cluster}
        if parsed_args.dest_host:
            body["dest_host"] = parsed_args.dest_host
        result = client.post(f"/vms/{parsed_args.vm_id}/migrate", json=body)
        return (
            ("migration_id", "status"),
            (result["migration_id"], result["status"]),
        )


class LiveMigrateVM(command.ShowOne):
    """Live migrate a VM to another cluster with minimal downtime.\n\n  openstack cnf vm live-migrate <vm-id> --to <cluster-id>"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("vm_id", metavar="<vm-id>")
        parser.add_argument("--to", required=True, dest="dest_cluster", metavar="<cluster-id>")
        parser.add_argument("--dest-host", default=None, metavar="<host>")
        parser.add_argument("--dest-next-hop", default=None, metavar="<ip>",
                            help="BGP next-hop IP on destination cluster")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        body = {"dest_cluster_id": parsed_args.dest_cluster}
        if parsed_args.dest_host:
            body["dest_host"] = parsed_args.dest_host
        if parsed_args.dest_next_hop:
            body["dest_next_hop"] = parsed_args.dest_next_hop
        result = client.post(f"/vms/{parsed_args.vm_id}/live-migrate", json=body)
        return (
            ("migration_id", "status"),
            (result["migration_id"], result["status"]),
        )
