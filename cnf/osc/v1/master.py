"""cnf.osc.v1.master — openstack cnf master * commands."""
from osc_lib.command import command


class ShowMaster(command.ShowOne):
    """Show which CNF node is currently master.\n\n  openstack cnf master show"""

    def get_parser(self, prog_name):
        return super().get_parser(prog_name)

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        m = client.get("/master")
        return (
            ("master_cluster_id", "this_cluster_id", "this_cluster_role"),
            (m["master_cluster_id"], m["this_cluster_id"], m["this_cluster_role"]),
        )


class ElectMaster(command.ShowOne):
    """Transfer master role to another cluster.\n\n  openstack cnf master elect <cluster-id>"""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument("cluster_id", metavar="<cluster-id>")
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.cnf
        result = client.post("/master/elect", json={"target_cluster_id": parsed_args.cluster_id})
        return (("success", "target"), (result["success"], result["target"]))
