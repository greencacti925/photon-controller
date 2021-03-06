# Copyright 2015 VMware, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, without
# warranties or conditions of any kind, EITHER EXPRESS OR IMPLIED.  See the
# License for then specific language governing permissions and limitations
# under the License.

import unittest

from mock import MagicMock
from hamcrest import *  # noqa
from pyVmomi import vim

from gen.resource.ttypes import Network, NetworkType
from host.hypervisor.esx.network_manager import EsxNetworkManager

MGMT_NETWORK_NAME = "Management Network"


def _net_config(type, name=MGMT_NETWORK_NAME):
    net_config = vim.host.VirtualNicManager.NetConfig(nicType=type)
    net_config.candidateVnic = [vim.host.VirtualNic(portgroup=name)]
    return net_config


class TestEsxNetworkManager(unittest.TestCase):

    def test_get_networks(self):
        """ Test normal get_network workflow:
        - call vim_client correctly.
        - collect network types and translate them to thrift representation
        correctly.
        """
        vim_client = MagicMock()
        vim_client.get_network_configs.return_value = [
            _net_config("faultToleranceLogging"),
            _net_config("management"),
            _net_config("vSphereReplication"),
            _net_config("vSphereReplicationNFC"),
            _net_config("vmotion"),
            _net_config("vsan"),
            _net_config("gandalfTheGray"),
        ]
        vim_client.get_networks.return_value = ["VM Network", "VM Network 2"]
        network_manager = EsxNetworkManager(vim_client, [])
        networks = network_manager.get_networks()

        assert_that(networks, has_length(3))
        # Verify 2 VM networks
        assert_that(networks, has_item(Network("VM Network",
                                               [NetworkType.VM])))
        assert_that(networks, has_item(Network("VM Network 2",
                                               [NetworkType.VM])))
        # Verify management network
        mgmt_types = [
            NetworkType.FT_LOGGING,
            NetworkType.MANAGEMENT,
            NetworkType.VSPHERE_REPLICATION,
            NetworkType.VSPHERE_REPLICATION_NFC,
            NetworkType.VMOTION,
            NetworkType.VSAN,
            NetworkType.OTHER,
        ].sort()
        network = self._find(MGMT_NETWORK_NAME, networks)
        assert_that(network.types.sort(), is_(mgmt_types))

    def test_get_vm_netwokrs(self):
        vim_client = MagicMock()
        vim_client.get_networks.return_value = ["VM Network", "VM Network 2"]

        # Verify identical list works.
        network_manager = EsxNetworkManager(vim_client, ["VM Network",
                                                         "VM Network 2"])
        networks = network_manager.get_vm_networks()
        self.assertEqual(networks, ["VM Network", "VM Network 2"])

        # Verify strict subset works
        network_manager = EsxNetworkManager(vim_client, ["VM Network"])
        networks = network_manager.get_vm_networks()
        self.assertEqual(networks, ["VM Network"])

        # Verify we filter out invalid networks.
        network_manager = EsxNetworkManager(vim_client, ["FOOBAR",
                                                         "VM Network"])
        networks = network_manager.get_vm_networks()
        self.assertEqual(networks, ["VM Network"])

        # If no network is specified, return the actual network list.
        network_manager = EsxNetworkManager(vim_client, None)
        networks = network_manager.get_vm_networks()
        self.assertEqual(networks, ["VM Network", "VM Network 2"])

    def _find(self, network_name, networks):
        network = [network for network in networks
                   if network.id == network_name]
        assert_that(network, has_length(1))
        return network[0]
