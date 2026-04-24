"""cnf.osc.plugin — Registers the CNF extension with python-openstackclient."""
from __future__ import annotations

import logging

from osc_lib import utils

DEFAULT_API_VERSION = "1"
API_VERSION_OPTION = "os_cnf_api_version"
API_NAME = "cnf"
API_VERSIONS = {"1": "cnf.osc.client.CnfClient"}


def make_client(instance):
    """Return a CNF API client for use in osc commands."""
    from cnf.osc.client import CnfClient

    endpoint = instance.get_endpoint_for_service_type(
        "cnf",
        region_name=instance._region_name,
        interface=instance.interface,
    )
    return CnfClient(endpoint=endpoint, session=instance.session)


def build_option_parser(parser):
    parser.add_argument(
        "--os-cnf-api-version",
        metavar="<cnf-api-version>",
        default=utils.env("OS_CNF_API_VERSION", default=DEFAULT_API_VERSION),
        help="CNF API version (default: 1)",
    )
    return parser
