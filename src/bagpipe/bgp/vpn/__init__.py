# vim: tabstop=4 shiftwidth=4 softtabstop=4
# encoding: utf-8

# Copyright 2014 Orange
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from threading import Lock

import re
import logging

from bagpipe.bgp.vpn.ipvpn import VRF
from bagpipe.bgp.vpn.evpn import EVI

import bagpipe.bgp.common.exceptions as exc

from bagpipe.bgp.common.looking_glass import LookingGlass, LGMap
from bagpipe.bgp.common import utils
from bagpipe.bgp.common.run_command import runCommand

from bagpipe.bgp.vpn.label_allocator import LabelAllocator

from exabgp.message.update.attribute.communities import RouteTarget


log = logging.getLogger(__name__)

class VPNManager(object, LookingGlass):
    """
    Creates, and keeps track of, VPN instances (VRFs and EVIs) and passes plug/unplug calls to the right VPN instance. 
    """
    
    type2class = { "ipvpn": VRF,
                   "evpn": EVI
                  }
    
    def __init__(self, bgpManager, dataplaneDrivers):
        '''
        dataplaneDrivers is a dict from vpn type to each dataplane driver, e.g. { "ipvpn": driverA, "evpn": driverB }
        '''
        
        log.debug("VPNManager init")
        
        self.bgpManager = bgpManager
        
        self.dataplaneDrivers = dataplaneDrivers

        # Init VPN instance identifiers
        self.instanceId = 1
        
        # VPN instance workers dict
        self.vpnWorkers = {}
        
        logging.debug("Creating label allocator")
        self.labelAllocator = LabelAllocator()
        
        # dict containing info how an ipvpn is plugged
        # from an evpn  (keys: ipvpn instances)  
        self._evpn_ipvpn_ifs = {}
        
        self.lock = Lock()
    
    def _convertRouteTargets(self, orig_list):
        assert(isinstance(orig_list,list))
        list_ = []
        for rt in orig_list:
            if rt == '': continue
            try:
                asn, nn = rt.split(':')
                list_.append(RouteTarget(int(asn), None, int(nn)))
            except Exception:
                raise Exception("Malformed route target: '%s'" % rt)
        return list_
    
    def _formatIpAddressPrefix(self, ipAddress):
        if re.match('([12]?\d?\d\.){3}[12]?\d?\d\/[123]?\d', ipAddress):
            address = ipAddress
        elif re.match('([12]?\d?\d\.){3}[12]?\d?\d', ipAddress):
            address = ipAddress + "/32"
        else:
            raise exc.MalformedIPAddress
            
        return address
    
    @utils.synchronized
    def getInstanceId(self):
        iid = self.instanceId
        self.instanceId += 1
        return iid
    
    def _attach_evpn2ipvpn(self,localPort,ipvpnInstance):
        """ Assuming localPort indicates no real interface but only
        an EVPN, this method will create a pair of twin interfaces, one 
        to plug in the EVPN, the other to plug in the IPVPN.
        
        The localPort dict will be modified so that the 'linuxif' indicates
        the name of the interface to plug in the IPVPN.
        
        The EVPN instance will be notified so that it forwards traffic
        destinated to the gateway on the interface toward the IPVPN.
        """
        assert('evpn' in localPort)

        if not 'id' in localPort['evpn']:
            raise Exception("Missing parameter 'id' :an external EVPN instance id must be specified for an EVPN attachment")
        
        try:
            evpnInstance = self.vpnWorkers[localPort['evpn']['id']]
        except:
            raise Exception("The specified evpn instance does not exist (%s)"
                             % localPort['evpn'])
        
        if (evpnInstance.type != "evpn"):
            raise Exception("The specified evpn instance to plug is not an "
                            "evpn instance (is %s instead)"% evpnInstance.type)
        
        if ipvpnInstance in self._evpn_ipvpn_ifs:
            (evpn_if,ipvpn_if
             ,evpnInstance,managed) = self. _evpn_ipvpn_ifs[ipvpnInstance]

            if not (localPort['evpn']['id'] == evpnInstance.instanceId):
                raise Exception('Trying to plug into an IPVPN a new E-VPN while'
                                ' one is already plugged in')
            else:
                # do nothing
                log.warning('Trying to plug an E-VPN into an IPVPN, but it was'
                            'already done')
                return
        
        #  detect if this evpn is already plugged into an IPVPN
        if evpnInstance.hasGatewayPort():
            raise Exception("Trying to plug E-VPN into an IPVPN, but this EVPN "
                            "already is plugged into an IPVPN")
        
        if ('linuxif' in localPort and localPort['linuxif']):
            raise Exception("Cannot specify an attachment with both a linuxif "
                            "and an evpn")
        
        if 'ovs_port_name' in localPort['evpn']:
            try:
                assert(localPort['ovs']['plugged'])
                assert(localPort['ovs']['port_name'] or localPort['ovs']['port_number'])
            except:
                raise Exception("Using ovs_port_name in an EVPN/IPVPN attachment"
                                " requires specifying the corresponding OVS" 
                                " port, which must also be pre-plugged")
            
            evpn_if = localPort['evpn']['ovs_port_name']
            
            # we assume in this case that the E-VPN interface is already
            # plugged into the E-VPN bridge
            managed=False
        else:
            evpn_if="evpn%d-ipvpn%d" %(evpnInstance.instanceId,ipvpnInstance.instanceId)
            ipvpn_if="ipvpn%d-evpn%d" %(ipvpnInstance.instanceId,evpnInstance.instanceId)
        
            #FIXME: do it only if not existing already...
            log.info("Creating veth pair %s %s "% (evpn_if,ipvpn_if))
            
            # delete the interfaces if they exist already
            runCommand(log, "ip link delete %s" % evpn_if, acceptableReturnCodes=[0,1])
            runCommand(log, "ip link delete %s" % ipvpn_if, acceptableReturnCodes=[0,1])
            
            runCommand(log, "ip link add %s type veth peer name %s" % 
                       (evpn_if,ipvpn_if))
    
            runCommand(log, "ip link set %s up" % evpn_if)
            runCommand(log, "ip link set %s up" % ipvpn_if)
            managed=True

        localPort['linuxif']=ipvpn_if
        
        evpnInstance.setGatewayPort(evpn_if,ipvpnInstance)
        
        self._evpn_ipvpn_ifs[ipvpnInstance]=(evpn_if,ipvpn_if,evpnInstance,managed)

    def _pre_detach_evpn2ipvpn(self,localPort,ipvpn):
        """ Symmetric to _attach_evpn2ipvpn
        """
        assert('evpn' in localPort)
        
        (evpn_if,ipvpn_if,evpnInstance,_) = self._evpn_ipvpn_ifs[ipvpn]
        
        if not 'id' in localPort['evpn']:
            raise Exception("Missing parameter 'id' :an external EVPN instance id must be specified for an EVPN attachment")
        
        if not (localPort['evpn']['id'] == evpnInstance.externalInstanceId):
            raise Exception('Mismatch between evpn specified at detach (%s) and evpn that was specified at attach (%s)'%
                            (localPort['evpn']['id'], evpnInstance.externalInstanceId))
        
        #TODO: check that this evpn instance is still up and running 
        evpnInstance.gatewayPortDown(evpn_if)
        
        localPort['linuxif']=ipvpn_if
    
    def _post_detach_evpn2ipvpn(self,localPort,ipvpn):
        (evpn_if,ipvpn_if,_,managed) = self._evpn_ipvpn_ifs[ipvpn]
        
        # cleanup veth pair
        if managed:
            runCommand(log, "ip link delete %s" % evpn_if)
            # the following is not needed since the two ifs are twins  
            #runCommand(log, "ip link delete %s" % ipvpn_if, acceptableReturnCodes=[0,1])
            
        del self._evpn_ipvpn_ifs[ipvpn]

    def plugVifToVPN(self, externalInstanceId, instanceType, importRTs, exportRTs, macAddress, ipAddress, gatewayIP, localPort, linuxbr):
        
        # Verify and format IP address with prefix if necessary
        try:
            ipAddressPrefix = self._formatIpAddressPrefix(ipAddress)
        except exc.MalformedIPAddress:
            raise
        
        # Convert route target string to RouteTarget dictionary
        importRTs = self._convertRouteTargets(importRTs)
        exportRTs = self._convertRouteTargets(exportRTs)

        # retrieve network mask
        mask = int(ipAddressPrefix.split('/')[1])

        # Retrieve VPN worker or create new one if does not exist
        try:
            vpnInstance = self.vpnWorkers[externalInstanceId]
            if (vpnInstance.type != instanceType):
                raise Exception("Trying to plug port on an existing instance of a different type (existing: %s, asked: %s)"% (vpnInstance.type,instanceType))
        except KeyError:
            instanceId = self.getInstanceId()
            log.info("Create and start new VPN instance %d for external network instance identifier %s" % (instanceId, externalInstanceId))
            try:
                vpnInstanceFactory = VPNManager.type2class[instanceType]
            except KeyError:
                log.error("Unsupported instanceType for VPNInstance: %s" % instanceType)
                raise Exception("Unsupported instance type: %s" % instanceType)
         
            try:
                dataplaneDriver = self.dataplaneDrivers[instanceType]
            except KeyError:
                log.error("No dataplane driver configured for VPN type %s" % instanceType)
                raise Exception("No dataplane driver configured for VPN type %s" % instanceType)
            
            
            if instanceType == "evpn" and linuxbr:
                kwargs = {'linuxbr': linuxbr}
            else:
                kwargs = {}

            vpnInstance = vpnInstanceFactory(
                                        self.bgpManager, self.labelAllocator, dataplaneDriver,
                                        externalInstanceId, instanceId, importRTs, exportRTs, gatewayIP, mask,
                                        **kwargs)
            
            # Update VPN instance workers list
            self.vpnWorkers[externalInstanceId] = vpnInstance
        
            vpnInstance.start()
        
        # Check if new route target import/export must be updated
        if not ((set(vpnInstance.importRTs) == set(importRTs)) and
                 (set(vpnInstance.exportRTs) == set(exportRTs))):
            vpnInstance.updateRouteTargets(importRTs, exportRTs)

        if instanceType == "ipvpn" and 'evpn' in localPort:
            # special processing for the case where what we plug into
            # the ipvpn is not an existing interface but an interface
            # to create, connected to an existing evpn instance
            self._attach_evpn2ipvpn(localPort,vpnInstance)

        # Plug VIF to VPN instance
        vpnInstance.vifPlugged(macAddress, ipAddressPrefix, localPort)
        
    def unplugVifFromVPN(self, externalInstanceId, macAddress, ipAddress, localPort):
        
        # Verify and format IP address with prefix if necessary
        try:
            ipAddressPrefix = self._formatIpAddressPrefix(ipAddress)
        except exc.MalformedIPAddress:
            raise
        
        # Retrieve VPN instance worker or raise exception if does not exist
        try:
            vpnInstance = self.vpnWorkers[externalInstanceId]
        except KeyError:
            log.error("Try to unplug VIF from non existing VPN instance worker %s" % externalInstanceId)
            raise exc.VPNNotFound(externalInstanceId)
        
        if vpnInstance.type == "ipvpn" and 'evpn' in localPort:
            self._pre_detach_evpn2ipvpn(localPort,vpnInstance)

        # Unplug VIF from VPN instance
        vpnInstance.vifUnplugged(macAddress, ipAddressPrefix, localPort)

        if vpnInstance.type == "ipvpn" and 'evpn' in localPort:
            self._post_detach_evpn2ipvpn(localPort,vpnInstance)
                
        if vpnInstance.isEmpty():
            vpnInstance.cleanup()
            del self.vpnWorkers[externalInstanceId]
            

    def stop(self):
        for worker in self.vpnWorkers.itervalues():
            worker.stop()
        for worker in self.vpnWorkers.itervalues():
            worker.join()

    ### Looking Glass hooks ####
    
    def getLGMap(self):
        class DataplaneLGHook(LookingGlass):
            def __init__(self, vpnManager):
                self.vpnManager = vpnManager
            def getLGMap(self):
                return {
                "drivers": (LGMap.COLLECTION, (self.vpnManager.getLGDataplanesList, self.vpnManager.getLGDataplaneFromPathItem)),
                "ids":     (LGMap.DELEGATE, self.vpnManager.labelAllocator)
                }
        dataplaneHook = DataplaneLGHook(self)
        return { 
               "instances": (LGMap.COLLECTION, (self.getLGVPNList, self.getLGVPNFromPathItem)),
               "dataplane": (LGMap.DELEGATE, dataplaneHook)
               }
    
    def getLGVPNList(self):
        return [{"id": i} for i in self.vpnWorkers.iterkeys()]
        
    def getLGVPNFromPathItem(self, pathItem):
        return self.vpnWorkers[pathItem]
    
    def getVPNWorkersCount(self):
        return len(self.vpnWorkers)

    ######## LookingGLass ########
    
    def getLGDataplanesList(self):
        return [{"id": i} for i in self.dataplaneDrivers.iterkeys()]

    def getLGDataplaneFromPathItem(self, pathItem):
        return self.dataplaneDrivers[pathItem]
    