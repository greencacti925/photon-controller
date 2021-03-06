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

""" Contains the implementation code for ESX VM operations."""
import logging
from operator import itemgetter
from host.hypervisor.vm_utils import parse_vmdk
from host.hypervisor.image_scanner import waste_time

import os
import socket
import struct
import threading

from pyVmomi import vim

from common.exclusive_set import DuplicatedValue
from common.exclusive_set import ExclusiveSet
from common.file_util import rm_rf
from common.file_util import mkdir_p
from common.kind import Flavor
from common.kind import Unit
from gen.agent.ttypes import PowerState
from gen.host.ttypes import ConnectedStatus
from gen.host.ttypes import VmNetworkInfo
from gen.host.ttypes import Ipv4Address
from gen.resource.ttypes import MksTicket
from host.hypervisor.resources import Disk
from host.hypervisor.resources import Resource
from host.hypervisor.resources import State
from host.hypervisor.resources import Vm
from host.hypervisor.vm_manager import OperationNotAllowedException
from host.hypervisor.vm_manager import VmManager
from host.hypervisor.vm_manager import IsoNotAttachedException
from host.hypervisor.vm_manager import VmAlreadyExistException
from host.hypervisor.vm_manager import VmNotFoundException
from host.hypervisor.vm_manager import VmPowerStateException
from host.hypervisor.esx.vm_config import datastore_to_os_path
from host.hypervisor.esx.vm_config import DeviceNotFoundException
from host.hypervisor.esx.vm_config import EsxVmConfig
from host.hypervisor.esx.vm_config import get_image_base_disk
from host.hypervisor.esx.vm_config import get_root_disk
from host.hypervisor.esx.vm_config import is_persistent_disk
from host.hypervisor.esx.vm_config import os_datastore_path
from host.hypervisor.esx.vm_config import vmdk_id
from host.hypervisor.esx.folder import VM_FOLDER_NAME
from host.hypervisor.datastore_manager import DatastoreNotFoundException

from common.log import log_duration


class NetUtil(object):
    """ Network utility classes for dealing with vmomi dataobjects
        We don't have Ipv4Address python packages on esx.
    """

    @staticmethod
    def is_ipv4_address(ip_address):
        """Utility method to check if an ip address is ipv4
        :param ip_addres: string ip address
        :rtype: bool, return True if the ip_address is a v4 address
        """
        try:
            socket.inet_aton(ip_address)
        except socket.error:
            return False
        return ip_address.count('.') == 3

    @staticmethod
    def prefix_len_to_mask(prefix_len):
        """Utility method to convert prefix length to netmask
        IpV4address pkg is not available on esx.
        :param prefix_len: int prefix len
        :rtype: string, string representation of the netmask
        """
        if (prefix_len < 0 or prefix_len > 32):
            raise ValueError("Invalid prefix length")
        mask = (1L << 32) - (1L << 32 >> prefix_len)

        return socket.inet_ntoa(struct.pack('>L', mask))


class EsxVmManager(VmManager):

    """ESX VM Manager specific implementation.

    This will be used by host/vm_manager.py if the agent has selected to use
    the ESX hypervisor on boot. This class contains all methods for VM power
    operations.

    Attributes:
        vim_client: The VimClient instance.
        vm_config: The EsxVmConfig instance.
        _logger: The global _logger to log messages to.

    """

    GUESTINFO_PREFIX = "guestinfo.esxcloud."
    VMINFO_PREFIX = "photon_controller.vminfo."
    EXTRA_CONFIG_VNC_ENABLED = "RemoteDisplay.vnc.enabled"
    EXTRA_CONFIG_VNC_PORT = "RemoteDisplay.vnc.port"
    METADATA_EXTRA_CONFIG_KEYS = (
        'bios.bootOrder', 'monitor.suspend_on_triplefault'
        # More TBA ...
    )

    def __init__(self, vim_client, ds_manager):
        self.vim_client = vim_client
        self.vm_config = EsxVmConfig(vim_client)
        self._logger = logging.getLogger(__name__)
        self._ds_manager = ds_manager
        self._lock = threading.Lock()
        self._datastore_cache = {}

    @staticmethod
    def _vim_power_state_to_resource_state(power_state):
        return {
            vim.VirtualMachine.PowerState.poweredOn: State.STARTED,
            vim.VirtualMachine.PowerState.poweredOff: State.STOPPED,
            vim.VirtualMachine.PowerState.suspended: State.SUSPENDED
        }[power_state]

    @staticmethod
    def _power_state_to_resource_state(power_state):
        return {
            PowerState.poweredOn: State.STARTED,
            PowerState.poweredOff: State.STOPPED,
            PowerState.suspended: State.SUSPENDED
        }[power_state]

    def _power_vm(self, vm_id, op):
        vm = self.vim_client.get_vm(vm_id)
        self._invoke_vm(vm, op)

    def _vm_op_to_requested_state(self, op):
        """ Return the string of a requested state from a VM op.

            For example if the operation is PowerOn the requested state is
            poweredOn.
        """
        if op == "PowerOn":
            return "poweredOn"
        elif op == "PowerOff":
            return "poweredOff"
        elif op == "Suspend":
            return "suspended"
        else:
            return "unknown"

    def _invoke_vm(self, vm, op, *args):
        try:
            self._logger.debug("Invoking '%s' for VM '%s'" % (op, vm.name))
            task = getattr(vm, op)(*args)
            self.vim_client.wait_for_task(task)
        except vim.fault.InvalidPowerState, e:
            if e.existingState == self._vm_op_to_requested_state(op):
                self._logger.info("VM %s already in %s state, %s successful." %
                                  (vm.name, e.existingState, op))
                pass
            else:
                raise VmPowerStateException(e.msg)

    def power_on_vm(self, vm_id):
        self._power_vm(vm_id, "PowerOn")

    def power_off_vm(self, vm_id):
        self._power_vm(vm_id, "PowerOff")

    def reset_vm(self, vm_id):
        self._power_vm(vm_id, "Reset")

    def suspend_vm(self, vm_id):
        self._power_vm(vm_id, "Suspend")

    def resume_vm(self, vm_id):
        self._power_vm(vm_id, "PowerOn")

    def _get_extra_config_map(self, metadata):
        # this can be simplified if the metadata dictionary follows some
        # convention in describing extra config properties
        if metadata is None:
            return {}
        return dict((k, v) for (k, v) in metadata.items() if k in
                    self.METADATA_EXTRA_CONFIG_KEYS)

    @log_duration
    def create_vm_spec(self, vm_id, datastore, flavor, metadata=None, env={},
                       **kwargs):
        """Create a new Virtual Machine create spec.

        :param vm_id: Name of the VM
        :type vm_id: str
        :param datastore: Name of the VM's datastore
        :type datastore: str
        :param flavor: VM flavor
        :type flavor: Flavor
        :param metadata: VM creation metadata
        :param kwargs: not used
        """

        # TODO(vspivak): long term introduce separate config (from cost) for
        # the hypervisor sizing meta
        cpus = int(flavor.cost["vm.cpu"].convert(Unit.COUNT))
        memory = int(flavor.cost["vm.memory"].convert(Unit.MB))
        spec = self.vm_config.create_spec(vm_id, datastore, memory,
                                          cpus, metadata, env)

        extra_config_map = self._get_extra_config_map(spec._metadata)
        # our one vm-identifying extra config
        extra_config_map[self.GUESTINFO_PREFIX + "vm.id"] = vm_id
        self.vm_config.set_extra_config(spec, extra_config_map)

        self.vm_config.set_diskuuid_enabled(spec, True)
        return spec

    @log_duration
    def update_vm_spec(self):
        """ Return an empty update spec for a VM.
        """
        return self.vm_config.update_spec()

    @log_duration
    def create_vm(self, vm_id, create_spec):
        """Create a new Virtual Maching given a VM create spec.

        :param vm_id: The Vm id
        :type vm_id: string
        :param create_spec: The VM spec builder
        :type ConfigSpec
        :raise: VmAlreadyExistException
        """
        folder = self.vim_client.vm_folder
        resource_pool = self.vim_client.root_resource_pool

        # sanity check since VIM does not prevent this
        try:
            if self.vim_client.get_vm_in_cache(vm_id):
                raise VmAlreadyExistException("VM already exists")
        except VmNotFoundException:
            pass

        # The scenario of the vm creation at ESX where intermediate directory
        # has to be created has not been well exercised and is known to be
        # racy and not informative on failures. So be defensive and proactively
        # create the intermediate directory ("/vmfs/volumes/<dsid>/vms/xy").
        vm_parent_dir = datastore_to_os_path(create_spec.files.vmPathName)
        if os.path.exists(vm_parent_dir):
            self._logger.debug("Parent directory %s exists" % vm_parent_dir)
        else:
            mkdir_p(vm_parent_dir)

        task = folder.CreateVm(create_spec, resource_pool, None)
        self.vim_client.wait_for_task(task)
        self.vim_client.wait_for_vm_create(vm_id)

    @log_duration
    def update_vm(self, vm_id, spec):
        """ Update the VM using the given spec.
        :type spec: vim.vm.ConfigSpec
        """
        vm = self.vim_client.get_vm(vm_id)
        self._reconfig_vm(vm, spec)

    def _ensure_directory_cleanup(self, vm_ds_path):
        # Upon successful destroy of VM, log any stray files still left in the
        # VM directory and delete the directory.
        vm_dir = os.path.dirname(datastore_to_os_path(vm_ds_path))
        if os.path.isdir(vm_dir):
            files = os.listdir(vm_dir)
            for f in files:
                if f.endswith(".vmdk"):
                    self._logger.info("Stray disk "
                                      "(possible data leak): %s" % f)
                else:
                    self._logger.info("Stray file: %s" % f)
            self._logger.warning("Force delete vm directory %s" % vm_dir)
            rm_rf(vm_dir)

    @log_duration
    def delete_vm(self, vm_id, force=False):
        """Delete a Virtual Machine

        :param vm_id: Name of the VM
        :type vm_id: str
        :param force: Not to check persistent disk, forcefully delete vm.
        :type force: boolean
        :raise VmPowerStateException when vm is not powered off
        """
        vm = self.vim_client.get_vm(vm_id)
        if vm.runtime.powerState != 'poweredOff':
            raise VmPowerStateException("Can only delete vm in state %s" %
                                        vm.runtime.powerState)

        vm_ds_path = self.get_vm_path(vm.config)

        if not force:
            self._verify_disks(vm)

        self._logger.info("Destroy VM at %s" % vm_ds_path)

        self._invoke_vm(vm, "Destroy")

        self._ensure_directory_cleanup(vm_ds_path)

        self.vim_client.wait_for_vm_delete(vm_id)

    @log_duration
    def has_vm(self, vm_id):
        try:
            self.vim_client.get_vm_in_cache(vm_id)
            return True
        except VmNotFoundException:
            return False

    @log_duration
    def add_disk(self, cspec, datastore, disk_id, info, disk_is_image=False):
        """Add an existing disk to a VM
        :param cspec: config spec
        :type cspec: ConfigSpec
        :param vm_id: VM id
        :type vm_id: str
        :param datastore: Name of the VM's datastore
        :type datastore: str
        :param disk_id: Disk id
        :type disk_id: str
        """
        if not info:
            # New VM just generate a base config.
            info = vim.vm.ConfigInfo(hardware=vim.vm.VirtualHardware())

        self.vm_config.add_scsi_disk(info, cspec, datastore, disk_id,
                                     disk_is_image=disk_is_image)
        return cspec

    @log_duration
    def remove_all_disks(self, cspec, info):
        """Removes all disks from the vm's config
        :param cspec: config spec
        :type cspec: ConfigSpec
        :param info: VM's config info
        :type info: ConfigInfo
        """
        self.vm_config.remove_all_disks(cspec, info)
        return cspec

    def remove_disk(self, spec, datastore, disk_id, info):
        """Remove an existing disk from a VM
        :param spec: config spec
        :type spec: ConfigSpec
        :param vm_id: Vm id
        :type vm_id: str
        :param datastore: Name of the VM's datastore
        :type datastore: str
        :param disk_id: Disk id
        :type disk_id: str
        """
        matcher = self.vm_config.disk_matcher(datastore, disk_id)
        devices = self.vm_config.get_devices_from_config(info)
        device = self.vm_config.get_device(devices, vim.vm.device.VirtualDisk,
                                           matcher=matcher)
        self.vm_config.remove_device(spec, device)
        return spec

    @log_duration
    def create_empty_disk(self, cfg_spec, datastore, disk_id, size_mb):
        """Add a create empty scsi disk spec to the config spec. The method
        will try to find an existing scsi controller to add the disk to. If no
        such scsi controller is found, it will add a new controller.

        :param cfg_spec: The VMs reconfigure spec
        :type cfg_spec: The VirtualMachineConfigSpec
        :param datastore: Name of the VM's datastore
        :type datastore: str
        :param disk_id: vmdk id
        :type disk_id: str
        :param size_mb: size of the disk in MB
        :type size_mb: int
        """
        self.vm_config.create_empty_disk(cfg_spec, datastore, disk_id,
                                         size_mb)
        return cfg_spec

    @log_duration
    def create_child_disk(self, cfg_spec, datastore, disk_id, parent_id):
        """Add a create child scsi disk spec to the config spec. The method
        will try to find an existing scsi controller to add the disk to. If no
        such scsi controller is found, it will add a new controller.

        :param cfg_spec: The VMs reconfigure spec
        :type cfg_spec: The VirtualMachineConfigSpec
        :param datastore: Name of the VM's datastore
        :type datastore: str
        :param disk_id: vmdk id
        :type disk_id: str
        :param parent_id: parent disk id
        :type parent_id: str
        """
        self.vm_config.create_child_disk(cfg_spec, datastore, disk_id,
                                         parent_id)
        return cfg_spec

    @log_duration
    def add_nic(self, spec, network_name=None):
        """Add a network adapter to a VM

        :param spec: The VM config spec to update with the added nic
        :type spec: vim.vm.ConfigSpec
        :param network_name: Network name
        :type network_id: str
        """
        spec = self.vm_config.add_nic(spec, network_name)

    @log_duration
    def customize_vm(self, spec):
        if 'annotation' in spec._metadata:
            self.vm_config.set_annotation(spec, spec._metadata['annotation'])

        self.vm_config.customize_serial_ports(spec)

    def _get_datastore_uuid(self, name):
        try:
            return self._ds_manager.normalize(name)
        except:
            # The exception usually happens when the agent is not
            # provisioned with the right configurations, especially in
            # integration test.
            self._logger.exception("Failed to get uuid for %s" % name)
            return None

    @log_duration
    def get_resources(self):
        resources = []
        vms = self.vim_client.get_vms_in_cache()
        for vm in vms:
            vm_resource = self._get_resource_from_vmcache(vm)
            if vm_resource.datastore:
                resources.append(Resource(vm_resource, vm_resource.disks))
        return resources

    def get_power_state(self, vm_id):
        vm = self.vim_client.get_vm_in_cache(vm_id)
        return self._power_state_to_resource_state(vm.power_state)

    @log_duration
    def get_resource(self, vm_id):
        vmcache = self.vim_client.get_vm_in_cache(vm_id)
        return self._get_resource_from_vmcache(vmcache)

    def _get_resource_from_vmcache(self, vmcache):
        """Translate to vm resource from vm cache
        """
        vm_resource = Vm(vmcache.name)
        vm_resource.flavor = Flavor("default")  # TODO
        vm_resource.disks = []

        for disk in vmcache.disks:
            disk_id = vmdk_id(disk)
            datastore_name = self._get_datastore_name_from_ds_path(disk)
            datastore_uuid = self._get_datastore_uuid(datastore_name)
            if datastore_uuid:
                disk_resource = Disk(disk_id, Flavor("default"), False,
                                     False, -1, None, datastore_uuid)
                vm_resource.disks.append(disk_resource)

        vm_resource.state = self._power_state_to_resource_state(
            vmcache.power_state)

        datastore_name = self._get_datastore_name_from_ds_path(vmcache.path)
        vm_resource.datastore = self._get_datastore_uuid(datastore_name)

        return vm_resource

    def get_resource_ids(self):
        ids = []
        vm_folder = self.vim_client.vm_folder
        for vm in vm_folder.GetChildEntity():
            ids.append(vm.name)
        return ids

    def get_used_memory_mb(self):
        vms = self.vim_client.get_vms_in_cache()
        if not vms:
            return 0

        memory = 0
        for vm in vms:
            # Vms in cache might include half updated record, e.g. with
            # None memory_mb, for a short time windows. Those Vms in cache
            # could be excluded from total used memory.
            if vm.memory_mb:
                memory += vm.memory_mb

        # This indicates that no values were retrieved from the cache.
        if memory == 0:
            raise VmNotFoundException("No valid VMs were found")

        return memory

    def get_configured_cpu_count(self):
        """
        Returns the total number of vCPUs across all VMs
        :return: number of vCPUs - int
        """
        vms = self.vim_client.get_vms_in_cache()
        if not vms:
            return 0

        cpu_count = 0
        for vm in vms:
            if vm.num_cpu:
                cpu_count += vm.num_cpu

        # This indicates that no values were retrieved from the cache.
        if cpu_count == 0:
            raise VmNotFoundException("No valid VMs were found")

        return cpu_count

    def _reconfig_vm(self, vm, spec):
        self._invoke_vm(vm, "ReconfigVM_Task", spec)

    def _get_datastore_name_from_ds_path(self, vm_path):
        try:
            return vm_path[vm_path.index("[") + 1:vm_path.index("]")]
        except:
            self._logger.warning("vm_path %s is malformated" % vm_path)
            raise

    def _verify_disks(self, vm):
        persistent_disks = [
            disk for disk in vm.layout.disk
            if is_persistent_disk(disk.diskFile)
        ]

        if persistent_disks:
            raise OperationNotAllowedException("persistent disks attached")

    @log_duration
    def get_vm_network(self, vm_id):
        """ Get the vm's network information
        We only report ip info if vmware tools is running within the guest.
        If tools are not running we can only report back the mac address
        assigned by the vmx, the connected status of the device and the network
        attached to the device.
        The information for mac, networkname and connected status is available
        through two places, the ethernetCards backing info and through the
        guestInfo. Both of these codepaths are not using VimVigor and seem to
        be implemented in a similar manner in hostd, so they should agree with
        each other. Just read this from the guestInfo as well.

        :param vm_id: Name of the VM
        :rtype: VmNetworkInfo
        """
        network_info = []

        # Throws when VM is not found.
        vm = self.vim_client.get_vm(vm_id)

        if (vm.guest is None or not vm.guest.net):
            # No guest info so return the info from the config file
            return self.get_network_config(vm_id)

        guest_nic_info_list = vm.guest.net

        # vmomi list attrs are never None could be an empty list
        for guest_nic_info in guest_nic_info_list:
            if (guest_nic_info.macAddress is None):
                # No mac address no real guest info. Not possible to have mac
                # address not reporte but ip stack info available.
                continue
            info = VmNetworkInfo(mac_address=guest_nic_info.macAddress)

            # Fill in the connected status.
            if guest_nic_info.connected:
                info.is_connected = ConnectedStatus.CONNECTED
            else:
                info.is_connected = ConnectedStatus.DISCONNECTED

            # Fill in the network binding info
            if guest_nic_info.network is not None:
                info.network = guest_nic_info.network

            # See if the ip information is available.
            if guest_nic_info.ipConfig is not None:
                ip_addresses = guest_nic_info.ipConfig.ipAddress
                # This is an array due to ipv6 support
                for ip_address in ip_addresses:
                    if (NetUtil.is_ipv4_address(ip_address.ipAddress)):
                        ip = Ipv4Address(
                            ip_address=ip_address.ipAddress,
                            netmask=NetUtil.prefix_len_to_mask(
                                ip_address.prefixLength))
                        info.ip_address = ip
                        break
            network_info.append(info)

        return network_info

    def attach_cdrom(self, spec, iso_file, vm_id):
        """ Attach an iso file to the VM after adding a CD-ROM device.

        :param spec: The VM config spec to update with the cdrom add
        :type spec: vim.vm.ConfigSpec
        :param iso_file: the file system path to the cdrom
        :type iso_file: str
        :param vm_id: The id of VM to attach iso from
        :type vm_id: str
        :rtype: bool. True if success, False if failure
        """
        vm = self.vim_client.get_vm(vm_id)

        if vm.config is None:
            raise Exception("Invalid VM config")

        # callee will modify spec
        return self.vm_config.add_iso_cdrom(spec, iso_file, vm.config)

    def disconnect_cdrom(self, spec, vm_id):
        """ Disconnect cdrom device from VM

        :param spec: The VM config spec to update with the cdrom change
        :type spec: vim.vm.ConfigSpec
        :param vm_id: The id of VM to detach iso from
        :type vm_id: str
        """
        vm = self.vim_client.get_vm(vm_id)
        if vm.config is None:
            raise Exception("Invalid VM config")

        try:
            iso_path = self.vm_config.disconnect_iso_cdrom(spec, vm.config)
        except DeviceNotFoundException, e:
            raise IsoNotAttachedException(e)
        except TypeError, e:
            raise IsoNotAttachedException(e)

        return iso_path

    def detach_cdrom(self, spec, vm_id):
        """ Remove cdrom device from VM

        :param spec: The VM config spec to update with the cdrom change
        :type spec: vim.vm.ConfigSpec
        :param vm_id: The id of VM to detach iso from
        :type vm_id: str
        """
        vm = self.vim_client.get_vm(vm_id)
        self.vm_config.remove_iso_cdrom(spec, vm.config)

    def remove_iso(self, iso_ds_path):
        try:
            os.remove(datastore_to_os_path(iso_ds_path))
        except:
            # The iso may not exist, so just catch and move on.
            pass

    def _get_network_config_int(self, config):
        """ Internal method that returns the device id, the network name and
        the mac address of the device.
        """
        # Throws when VM is not found.
        network_info = []

        if (config is None):
            self._logger.info("VM, has no hardware specification")
            return network_info

        if (config.hardware.device):
            idx = 0
            for device in config.hardware.device:
                if (isinstance(device, vim.vm.device.VirtualEthernetCard) and
                    isinstance(device.backing,
                               vim.vm.device.VirtualEthernetCard.
                               NetworkBackingInfo)):
                    # idx is used for mac address generation
                    network_info.append((idx,
                                         device.macAddress,
                                         device.backing.deviceName,
                                         device.key))
                    idx += 1
        return sorted(network_info, key=itemgetter(2))

    @log_duration
    def get_network_config(self, vm_id):
        """ Get the network backing of a VM by reading its configuration.

        This is different from the get_vm_network above which gets the network
        information from tools.
        Only the mac address and the corresponding network name is going to be
        populated in this model.
        :type vm_id: VM str
        :rtype VMNetworkInfo list.
        """

        network_info = []
        vm = self.vim_client.get_vm(vm_id)
        networks = self._get_network_config_int(vm.config)

        for idx, mac, network, _ in networks:
            # We don't set MAC address when VM gets created, so MAC address
            # won't be set until the VM gets powered on.
            info = VmNetworkInfo(mac_address=mac,
                                 network=network)
            network_info.append(info)
        return network_info

    def _find_ip(self, conn_spec, network):
        """ Finds the ip and netmast associated with the network name.
        :type NetworkConnectionSpec: The network connection spec to extract
                                      the ip info for.
        :type network: The network to extract the connection info for.
        :rtype: Tuple containing ip, netmask, updated network conn spec
        """
        found = False
        ip, mask = None, None
        for spec in conn_spec.nic_spec:
            if (spec.network_name == network):
                found = True
                if spec.ip_address:
                    ip = spec.ip_address.ip_address
                    mask = spec.ip_address.netmask
                conn_spec.nic_spec.remove(spec)
                break
        # We should have failed earlier if we didn't have the nic
        assert(found)
        return ip, mask, conn_spec

    @log_duration
    def set_guestinfo_ip(self, spec, info, net_spec):
        """ Set the ip address information for the VM in the config file.
        Reads the network config from the vmx and sets the IP address for the
        corresponding devices in guest info and the default GW properties in
        guest info.
        A script from within the guest will read these attributes and set it
        within the guest.
        :type spec: vim.vm.ConfigSpec, the virtual machine config spec
        :type info: vim.vm.ConfigInfo, the virtual machine config info
        :type: net_spec: the NetworkConnectionSpec object to apply
        """
        if net_spec is None:
            return
        prefix = self.GUESTINFO_PREFIX
        guest_info = {}
        if net_spec.default_gateway:
            guest_info[prefix + "default_gateway"] = \
                net_spec.default_gateway
        # Read the networks from the created VM.
        networks = self._get_network_config_int(info)
        for index, (_, _, network, _) in enumerate(networks):
            ip, netmask, net_spec = self._find_ip(net_spec, network)
            if ip and netmask:
                guest_info[prefix + str(index) + ".ip"] = ip
                guest_info[prefix + str(index) + ".netmask"] = netmask

        if len(guest_info.keys()) > 0:
            self.vm_config.set_extra_config(spec, guest_info)
            return True

        return False

    def set_vminfo(self, spec, vminfo):
        prefixed_vminfo = {}
        for k, v in vminfo.iteritems():
            prefixed_vminfo[self.VMINFO_PREFIX + k] = v
        self.vm_config.set_extra_config(spec, prefixed_vminfo)

    def get_vminfo(self, vm_id):
        extras = self.get_vm_config(vm_id).extraConfig
        vminfo = {}
        for config in extras:
            if config.key.startswith(self.VMINFO_PREFIX):
                key = config.key[(len(self.VMINFO_PREFIX)):]
                vminfo[key] = config.value
        return vminfo

    def get_vm_config(self, vm_id):
        """ Get the config info of a VM. """
        vm = self.vim_client.get_vm(vm_id)
        return vm.config

    def get_vm_path(self, config):
        """ Get the datastore path to the VM's config file. """
        return config.files.vmPathName

    def get_vm_datastore(self, config):
        """ Get the datastore id to the VM's config file.

        The VM can have file components residing on other datastores as well,
        but this call is implemented by design to only return the datastore
        in which the config file resides.
        """
        vmx = config.files.vmPathName
        datastore_name = self._get_datastore_name_from_ds_path(vmx)
        datastore_id = self._ds_manager.normalize(datastore_name)
        return datastore_id

    @log_duration
    def get_linked_clone_path(self, vm_id):
        """Get the absolute path of a VM linked clone disk

        :param vm_id: VM ID as a string.
        :return: absolute path to the linked clone disk, or None if the VM
                 doesn't exist in the cache or was created with full clone.
        """
        vm = self.vim_client.get_vm_in_cache(vm_id)
        if not vm or not vm.disks:
            self._logger.debug("Image disk not found for %s: %s" % (vm_id, vm))
            return None
        return get_root_disk(vm.disks)

    @log_duration
    def get_linked_clone_image_path(self, vm_id):
        """Get image path for a VM created with linked clone.

        VMs created with linked clone has a base image disk under
        /vmfs/volumes/$datastore/images. This method fetches a VM from the
        cache and find that disk.
        """
        vm = self.vim_client.get_vm_in_cache(vm_id)
        if not vm or not vm.disks:
            self._logger.debug("Image disk not found for %s: %s" % (vm_id, vm))
            return None
        return get_image_base_disk(vm.disks)

    @log_duration
    def set_vnc_port(self, spec, port):
        """
        :param spec: vim.vm.ConfigSpec, the virtual machine config spec
        :param port: int, the vnc port assigned to the vm
        """
        if spec.extraConfig is None:
            spec.extraConfig = []

        spec.extraConfig.append(vim.OptionValue(
            key=self.EXTRA_CONFIG_VNC_ENABLED,
            value="True"))
        spec.extraConfig.append(vim.OptionValue(
            key=self.EXTRA_CONFIG_VNC_PORT,
            value=port))

        return spec

    @log_duration
    def get_vnc_port(self, vm_id):
        """Get vnc port from a vm
        :param vm_id: the id of the vm
        :return: port number assigned to vm or None
        """
        vm = self.vim_client.get_vm(vm_id)
        if not vm.config.extraConfig:
            return None

        options = [o for o in vm.config.extraConfig
                   if o.key == self.EXTRA_CONFIG_VNC_PORT]
        if not options:
            return None

        return int(options[0].value)

    @log_duration
    def get_occupied_vnc_ports(self):
        vms = self.vim_client.get_vms()

        ports = ExclusiveSet()
        for vm in vms:
            if vm.config and vm.config.extraConfig:
                enabled = False
                port = None
                for option in vm.config.extraConfig:
                    if option.key == self.EXTRA_CONFIG_VNC_ENABLED:
                        enabled = option.value.upper() == 'TRUE'
                    elif option.key == self.EXTRA_CONFIG_VNC_PORT:
                        port = int(option.value)
                if enabled:
                    try:
                        ports.add(port)
                    except DuplicatedValue:
                        self._logger.warning("port %d already occupied" % port)
        return ports

    def get_mks_ticket(self, vm_id):
        vm = self.vim_client.get_vm(vm_id)
        if vm.runtime.powerState != 'poweredOn':
            raise OperationNotAllowedException('Not allowed on vm that is '
                                               'not powered on.')
        mks = vm.AcquireMksTicket()
        return MksTicket(cfg_file=mks.cfgFile,
                         host=mks.host,
                         port=mks.port,
                         ssl_thumbprint=mks.sslThumbprint,
                         ticket=mks.ticket)

    def get_vm_images(self, image_scanner):
        vms_dir_path = os_datastore_path(image_scanner.datastore_id,
                                         VM_FOLDER_NAME)
        # Log messages with prefix: "IMAGE SCANNER" are for debugging
        # and will be removed after basic testing
        self._logger.info("IMAGE SCANNER: vms_dir: %s" % vms_dir_path)
        if not os.path.isdir(vms_dir_path):
            self._logger.info("get_vm_images: vms_dir: %s, doesn't exist"
                              % vms_dir_path)
            raise DatastoreNotFoundException(
                "Image scanner, cannot find vms "
                "directory for datastore: %s"
                % image_scanner.datastore_id)

        return self._collect_active_images(image_scanner, vms_dir_path)

    def _collect_active_images(self, image_scanner, root):
        """
        :param root: top directory
        :return: dictionary of used images, key is image id
        """
        # Log messages with prefix: "IMAGE SCANNER" are for debugging
        # and will be removed after basic testing
        self._logger.info("IMAGE SCANNER: calling collect_active_images()")
        # Compute scan rest interval
        rest_interval_sec = image_scanner.get_vm_scan_rest_interval()
        active_images = dict()
        for curdir, dirs, files in os.walk(root):

            # On a directory change check if it still needs to run
            if image_scanner.is_stopped():
                return active_images

            # If this contains only other directories skip it
            if len(files) == 0:
                continue

            # Look for the vmdk file
            for vm_file in files:
                self._logger.info("IMAGE SCANNER: current file %s" % vm_file)
                # Skip non vmdk files
                if not vm_file.endswith(".vmdk"):
                    continue
                # Skip vmdk delta files
                if vm_file.endswith("delta.vmdk"):
                    continue
                # Skip vmdk flat file
                if vm_file.endswith("flat.vmdk"):
                    continue
                vmdk_pathname = os.path.join(curdir, vm_file)
                self._logger.info("IMAGE SCANNER: found vmdk: %s"
                                  % vmdk_pathname)
                try:
                    vmdk_dictionary = parse_vmdk(vmdk_pathname)
                    # If there is no file_name_hint, skip it
                    if image_scanner.FILE_NAME_HINT not in vmdk_dictionary:
                        # This should be a common occurrence
                        # the log level should debug
                        self._logger.info("IMAGE_SCANNER: Vm scan, "
                                          "skipping file: %s "
                                          "missing parent hint"
                                          % vmdk_pathname)
                        continue
                    file_name_hint = \
                        vmdk_dictionary[image_scanner.FILE_NAME_HINT]
                    image_id = image_scanner.image_manager.\
                        get_image_id_from_path(file_name_hint)
                    if image_id not in active_images:
                        self._logger.info(
                            "IMAGE SCANNER: adding image_id: %s" % image_id)
                        active_images[image_id] = file_name_hint
                except Exception as ex:
                    self._logger.warn("Vm scan, skipping file: %s : %s"
                                      % (vmdk_pathname, ex))
            waste_time(rest_interval_sec)
        return active_images
