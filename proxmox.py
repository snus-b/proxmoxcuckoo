# Template copied from virtualbox.py modified to work with Proxmox 

import logging
import os
import subprocess
import time

from cuckoo.common.abstracts import Machinery
from cuckoo.common.config import config
from cuckoo.common.exceptions import (
    CuckooCriticalError, CuckooMachineError, CuckooMachineSnapshotError,
    CuckooMissingMachineError
)
from cuckoo.misc import Popen
from proxmoxer import ProxmoxAPI

log = logging.getLogger(__name__)

class Proxmox(Machinery):
    """Virtualization layer for Proxmox."""

    # VM states.
    SAVED    = "paused"
    FINISH   = "finish-migrate"
    RUNNING  = "running"
    POWEROFF = "stopped"
    ERROR    = "machete"

    def _initialize_check(self):
        """Runs all checks when a machine manager is initialized.
        @raise CuckooMachineError: if Proxmox is not found.
        """

        if self.options.proxmox.username == None:
            raise CuckooCriticalError(
                "Proxmox requires a username. Example 'root@pam'"
            )

        if self.options.proxmox.password == None:
            raise CuckooCriticalError(
                "Proxmox requires a password."
            )

        if self.options.proxmox.host == None:
            raise CuckooCriticalError(
                "Proxmox requires a valid hostname or IP."
            )

        if self.options.proxmox.nodename == None:
            raise CuckooCriticalError(
                "Proxmox requires a valid name of the node to access."
            )

        self.username = self.options.proxmox.username
        self.password = self.options.proxmox.password
        self.host     = self.options.proxmox.host
        self.nodename = self.options.proxmox.nodename#.split(',') #TODO add comma deliminated nodes

        if not self.username[-4:] == '@pam':
            self.username += '@pam'

        self._connect_proxmox()

        super(Proxmox, self)._initialize_check()

        # Restore each virtual machine to its snapshot. This will crash early
        # for users that don't have proper snapshots in-place, which is good.
        # TODO: This seems a good idea but need to find how to implement without every registered
        #       vm starting. Restore even from a paused machine come back running!!!
#        for machine in self.machines():
#            if machine.label not in self.vm_label_id:
#                continue
#
#            # note restore even from a paused machine starts a running machine
#            self.restore(machine.label, machine)
#            self.stop( machine.label, machine )

    def _connect_proxmox( self ):
        self.proxmox = ProxmoxAPI( self.host, user=self.username, password=self.password, verify_ssl=False)
        self.nodes = self.proxmox.nodes()
        if len(self.nodes.get()) == 0:
            raise CuckooCriticalError( "Proxmox found no Nodes.")
            
        self._verifyNodes()
        self.vm_label_id = self._getVMID()

    def _getVMStatus( self, vmid ):
        ret = None
        try:
            ret = self.nodes.get('%s/qemu/%s/status/current' % (self.nodename, vmid) )
        except:
            log.debug( "Connection failed attempting to reconnect")
            self._connect_proxmox()
            try:
                ret = self.nodes.get('%s/qemu/%s/status/current' % (self.nodename, vmid) )
            except:
                raise CuckooCriticalError('Proxmox connection failed')
        return ret    
        
    def _verifyNodes( self ):
        bFound = False
        for node in self.nodes.get():
            if node['node'] in self.nodename:
                if node['status'] == 'online':
                    bFound = True
                    continue

                raise CuckooCriticalError( "The node %r was not online." % self.nodename)
        if not bFound:
            raise CuckooCriticalError( "Proxmox failed to find the node %r." % self.nodename)

    def _getVMID( self ):
        ret = {}
        for vm in self.nodes.get('%s/qemu' % self.nodename ):
            ret[vm['name']] = { 'vmid': vm['vmid'], 'status': vm['status'] }

        return ret

    def restore(self, label, machine):
        """Restore a VM to its snapshot."""

        vmid = self.vm_label_id[ label ]['vmid']

        if machine.snapshot:
            log.debug(
                "Restoring virtual machine %s to %s",
                label, machine.snapshot
            )
            snapshot = machine.snapshot
        else:
            log.debug(
                "Restoring virtual machine %s to its current snapshot",
                label
            )

            snapshots = self.nodes.get('%s/qemu/%s/snapshot' % (self.nodename, vmid ))
            if len(snapshots) == 1:
                raise CuckooMachineSnapshotError(
                    "Proxmox failed trying to restore the snapshot of "
                    "machine '%s' (this most likely means there is no snapshot, "
                    "please refer to our documentation for more information on "
                    "how to setup a snapshot for your VM): %s" % (label, e)
                )

            snapshot = snapshots[-1]['parent']
                
        try:
            print 'n:%r id:%r snap:%r' % ( self.nodename, vmid, snapshot) 
            self.nodes.post('%s/qemu/%s/snapshot/%s/rollback' % ( self.nodename,
                                                                 vmid, 
                                                                 snapshot) )
            print "------ finished the revert";
        except OSError as e:
            raise CuckooMachineSnapshotError(
                "Proxmox failed trying to restore the snapshot of "
                "machine '%s' (this most likely means there is no snapshot, "
                "please refer to our documentation for more information on "
                "how to setup a snapshot for your VM): %s" % (label, e)
            )

    def start(self, label, task):
        """Start a virtual machine.
        @param label: virtual machine name.
        @param task: task object.
        @raise CuckooMachineError: if unable to start.
        """
        vmid = self.vm_label_id[ label ]['vmid']
        log.debug("Starting vm %s id %s", label, vmid)


        if self._status(label) == self.RUNNING:
            raise CuckooMachineError(
                "Trying to start an already started VM: %s" % label
            )

        machine = self.db.view_machine_by_label(label)
        self.restore(label, machine)

        # Proxmox can revert to  saved paused state but the state is in the qemu status not the vm status.
        self._wait_status(label, self.SAVED, self.FINISH, self.RUNNING)

        if not self._status(label) == self.RUNNING:
            try:
                self.nodes.post('%s/qemu/%s/status/start' % ( self.nodename, vmid  ) )
            except OSError as e:
                raise CuckooMachineError(
                    "Proxmox failed to start the machine: %s" % e
                )
            self._wait_status(label, self.RUNNING)

        if "nictrace" in machine.options:
            self.dump_pcap(label, task)

    def stop(self, label):
        """Stops a virtual machine.
        @param label: virtual machine name.
        @raise CuckooMachineError: if unable to stop.
        """
        vmid = self.vm_label_id[ label ]['vmid']
        log.debug("Stopping vm %s id %s" % ( label, vmid ) )

        status = self._status(label)

        if status == self.SAVED:
            return

        if status == self.POWEROFF:
            raise CuckooMachineError(
                "Trying to stop an already stopped VM: %s" % label
            )

        try:
            self.nodes.post('%s/qemu/%s/status/stop' % ( self.nodename, vmid  ) )
        except OSError as e:
            raise CuckooMachineError(
                "Proxmox failed powering off the machine: %s" % e
            )

        self._wait_status(label, self.POWEROFF, self.SAVED, self.FINISH)

    def _status(self, label):
        """Gets current status of a vm.
        @param label: virtual machine name.
        @return: status string.
        """

        status = self._getVMStatus( self.vm_label_id[ label ]['vmid'] )['qmpstatus']

        if status is False:
            status = self.ERROR

        # Report back status.
        if status:
            self.set_status(label, status)
            return status

        raise CuckooMachineError(
            "Unable to get status for %s" % label
        )

    def dump_pcap(self, label, task):
        #TODO Add this if it can be done
        pass

    def dump_memory(self, label, path):
        """Takes a memory dump.
        @param path: path to where to store the memory dump.
        """
        #TODO: can be done as part of live snapshots.  https://pve.proxmox.com/wiki/Live_Snapshots
        # The memory will be saved with the snapshot. Possibly this could be parsed out and saved to disc
        pass
