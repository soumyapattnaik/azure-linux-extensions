#!/usr/bin/env python
#
# Azure Linux Agent
#
# Copyright 2015 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.6+ and Openssl 1.0+
#
# Implements parts of RFC 2131, 1541, 1497 and
# http://msdn.microsoft.com/en-us/library/cc227282%28PROT.10%29.aspx
# http://msdn.microsoft.com/en-us/library/cc227259%28PROT.13%29.aspx
#

import crypt
import random
import base64

try:
    import httplib as httplibs
except ImportError:
    import http.client as httplibs
import os
import os.path
import platform
import pwd
import re
import shutil
import socket
try:
    import SocketServer as SocketServers
except ImportError:
    import socketserver as SocketServers
import string
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import xml.dom.minidom
import inspect
import zipfile
import json
import datetime
import xml.sax.saxutils
from distutils.version import LooseVersion

if not hasattr(subprocess, 'check_output'):
    def check_output(*popenargs, **kwargs):
        r"""Backport from subprocess module from python 2.7"""
        if 'stdout' in kwargs:
            raise ValueError('stdout argument not allowed, it will be overridden.')
        process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
        output, unused_err = process.communicate()
        retcode = process.poll()
        if retcode:
            cmd = kwargs.get("args")
            if cmd is None:
                cmd = popenargs[0]
            raise subprocess.CalledProcessError(retcode, cmd, output=output)
        return output


    # Exception classes used by this module.
    class CalledProcessError(Exception):
        def __init__(self, returncode, cmd, output=None):
            self.returncode = returncode
            self.cmd = cmd
            self.output = output

        def __str__(self):
            return "Command '%s' returned non-zero exit status %d" % (self.cmd, self.returncode)


    subprocess.check_output = check_output
    subprocess.CalledProcessError = CalledProcessError

GuestAgentName = "WALinuxAgent"
GuestAgentLongName = "Azure Linux Agent"
GuestAgentVersion = "WALinuxAgent-2.0.16"
ProtocolVersion = "2012-11-30"  # WARNING this value is used to confirm the correct fabric protocol.

Config = None
WaAgent = None
DiskActivated = False
Openssl = "openssl"
Children = []
ExtensionChildren = []
VMM_STARTUP_SCRIPT_NAME = 'install'
VMM_CONFIG_FILE_NAME = 'linuxosconfiguration.xml'
global RulesFiles
RulesFiles = ["/lib/udev/rules.d/75-persistent-net-generator.rules",
              "/etc/udev/rules.d/70-persistent-net.rules"]
VarLibDhcpDirectories = ["/var/lib/dhclient", "/var/lib/dhcpcd", "/var/lib/dhcp"]
EtcDhcpClientConfFiles = ["/etc/dhcp/dhclient.conf", "/etc/dhcp3/dhclient.conf"]
global LibDir
LibDir = "/var/lib/waagent"
global provisioned
provisioned = False
global provisionError
provisionError = None
HandlerStatusToAggStatus = {"installed": "Installing", "enabled": "Ready", "unintalled": "NotReady",
                            "disabled": "NotReady"}

WaagentConf = """\
#
# Azure Linux Agent Configuration
#

Role.StateConsumer=None                 # Specified program is invoked with the argument "Ready" when we report ready status
                                        # to the endpoint server.
Role.ConfigurationConsumer=None         # Specified program is invoked with XML file argument specifying role configuration.
Role.TopologyConsumer=None              # Specified program is invoked with XML file argument specifying role topology.

Provisioning.Enabled=y                  #
Provisioning.DeleteRootPassword=y       # Password authentication for root account will be unavailable.
Provisioning.RegenerateSshHostKeyPair=y # Generate fresh host key pair.
Provisioning.SshHostKeyPairType=rsa     # Supported values are "rsa", "dsa" and "ecdsa".
Provisioning.MonitorHostName=y          # Monitor host name changes and publish changes via DHCP requests.

ResourceDisk.Format=y                   # Format if unformatted. If 'n', resource disk will not be mounted.
ResourceDisk.Filesystem=ext4            # Typically ext3 or ext4. FreeBSD images should use 'ufs2' here.
ResourceDisk.MountPoint=/mnt/resource   #
ResourceDisk.EnableSwap=n               # Create and use swapfile on resource disk.
ResourceDisk.SwapSizeMB=0               # Size of the swapfile.

LBProbeResponder=y                      # Respond to load balancer probes if requested by Azure.

Logs.Verbose=n                          # Enable verbose logs

OS.RootDeviceScsiTimeout=300            # Root device timeout in seconds.
OS.OpensslPath=None                     # If "None", the system default version is used.
"""
README_FILENAME = "DATALOSS_WARNING_README.txt"
README_FILECONTENT = """\
WARNING: THIS IS A TEMPORARY DISK. 

Any data stored on this drive is SUBJECT TO LOSS and THERE IS NO WAY TO RECOVER IT.

Please do not use this disk for storing any personal or application data.

For additional details to please refer to the MSDN documentation at : http://msdn.microsoft.com/en-us/library/windowsazure/jj672979.aspx
"""


############################################################
# BEGIN DISTRO CLASS DEFS
############################################################
############################################################
#	AbstractDistro
############################################################
class AbstractDistro(object):
    """
    AbstractDistro defines a skeleton neccesary for a concrete Distro class.

    Generic methods and attributes are kept here, distribution specific attributes
    and behavior are to be placed in the concrete child named distroDistro, where
    distro is the string returned by calling python platform.linux_distribution()[0].
    So for CentOS the derived class is called 'centosDistro'.
    """

    def __init__(self):
        """
        Generic Attributes go here.  These are based on 'majority rules'.
        This __init__() may be called or overriden by the child.
        """
        self.agent_service_name = os.path.basename(sys.argv[0])
        self.selinux = None
        self.service_cmd = '/usr/sbin/service'
        self.ssh_service_restart_option = 'restart'
        self.ssh_service_name = 'ssh'
        self.ssh_config_file = '/etc/ssh/sshd_config'
        self.hostname_file_path = '/etc/hostname'
        self.dhcp_client_name = 'dhclient'
        self.requiredDeps = ['route', 'shutdown', 'ssh-keygen', 'useradd', 'usermod',
                             'openssl', 'sfdisk', 'fdisk', 'mkfs',
                             'sed', 'grep', 'sudo', 'parted']
        self.init_script_file = '/etc/init.d/waagent'
        self.agent_package_name = 'WALinuxAgent'
        self.fileBlackList = ["/root/.bash_history", "/var/log/waagent.log", '/etc/resolv.conf']
        self.agent_files_to_uninstall = ["/etc/waagent.conf", "/etc/logrotate.d/waagent"]
        self.grubKernelBootOptionsFile = '/etc/default/grub'
        self.grubKernelBootOptionsLine = 'GRUB_CMDLINE_LINUX_DEFAULT='
        self.getpidcmd = 'pidof'
        self.mount_dvd_cmd = 'mount'
        self.sudoers_dir_base = '/etc'
        self.waagent_conf_file = WaagentConf
        self.shadow_file_mode = 0o600
        self.shadow_file_path = "/etc/shadow"
        self.dhcp_enabled = False

    def isSelinuxSystem(self):
        """
        Checks and sets self.selinux = True if SELinux is available on system.
        """
        if self.selinux == None:
            if Run("which getenforce", chk_err=False):
                self.selinux = False
            else:
                self.selinux = True
        return self.selinux

    def isSelinuxRunning(self):
        """
        Calls shell command 'getenforce' and returns True if 'Enforcing'.
        """
        if self.isSelinuxSystem():
            return RunGetOutput("getenforce")[1].startswith("Enforcing")
        else:
            return False

    def setSelinuxEnforce(self, state):
        """
        Calls shell command 'setenforce' with 'state' and returns resulting exit code.
        """
        if self.isSelinuxSystem():
            if state:
                s = '1'
            else:
                s = '0'
            return Run("setenforce " + s)

    def setSelinuxContext(self, path, cn):
        """
        Calls shell 'chcon' with 'path' and 'cn' context.
        Returns exit result.
        """
        if self.isSelinuxSystem():
            if not os.path.exists(path):
                Error("Path does not exist: {0}".format(path))
                return 1
            return Run('chcon ' + cn + ' ' + path)

    def setHostname(self, name):
        """
        Shell call to hostname.
        Returns resulting exit code.
        """
        return Run('hostname ' + name)

    def publishHostname(self, name):
        """
        Set the contents of the hostname file to 'name'.
        Return 1 on failure.
        """
        try:
            r = SetFileContents(self.hostname_file_path, name)
            for f in EtcDhcpClientConfFiles:
                if os.path.exists(f) and FindStringInFile(f,
                                                          r'^[^#]*?send\s*host-name.*?(<hostname>|gethostname[(,)])') == None:
                    r = ReplaceFileContentsAtomic('/etc/dhcp/dhclient.conf', "send host-name \"" + name + "\";\n"
                                                  + "\n".join(filter(lambda a: not a.startswith("send host-name"),
                                                                     GetFileContents('/etc/dhcp/dhclient.conf').split(
                                                                         '\n'))))
        except:
            return 1
        return r

    def installAgentServiceScriptFiles(self):
        """
        Create the waagent support files for service installation.
        Called by registerAgentService()
        Abstract Virtual Function.  Over-ridden in concrete Distro classes.
        """
        pass

    def registerAgentService(self):
        """
        Calls installAgentService to create service files.
        Shell exec service registration commands. (e.g. chkconfig --add waagent)
        Abstract Virtual Function.  Over-ridden in concrete Distro classes.
        """
        pass

    def uninstallAgentService(self):
        """
        Call service subsystem to remove waagent script.
        Abstract Virtual Function.  Over-ridden in concrete Distro classes.
        """
        pass

    def unregisterAgentService(self):
        """
        Calls self.stopAgentService and call self.uninstallAgentService()
        """
        self.stopAgentService()
        self.uninstallAgentService()

    def startAgentService(self):
        """
        Service call to start the Agent service
        """
        return Run(self.service_cmd + ' ' + self.agent_service_name + ' start')

    def stopAgentService(self):
        """
        Service call to stop the Agent service
        """
        return Run(self.service_cmd + ' ' + self.agent_service_name + ' stop', False)

    def restartSshService(self):
        """
        Service call to re(start) the SSH service
        """
        sshRestartCmd = self.service_cmd + " " + self.ssh_service_name + " " + self.ssh_service_restart_option
        retcode = Run(sshRestartCmd)
        if retcode > 0:
            Error("Failed to restart SSH service with return code:" + str(retcode))
        return retcode

    def checkPackageInstalled(self, p):
        """
        Query package database for prescence of an installed package.
        Abstract Virtual Function.  Over-ridden in concrete Distro classes.
        """
        pass

    def checkPackageUpdateable(self, p):
        """
        Online check if updated package of walinuxagent is available.
        Abstract Virtual Function.  Over-ridden in concrete Distro classes.
        """
        pass

    def deleteRootPassword(self):
        """
        Generic root password removal.
        """
        filepath = "/etc/shadow"
        ReplaceFileContentsAtomic(filepath, "root:*LOCK*:14600::::::\n"
                                  + "\n".join(
            filter(lambda a: not a.startswith("root:"), GetFileContents(filepath).split('\n'))))
        os.chmod(filepath, self.shadow_file_mode)
        if self.isSelinuxSystem():
            self.setSelinuxContext(filepath, 'system_u:object_r:shadow_t:s0')
        Log("Root password deleted.")
        return 0

    def changePass(self, user, password):
        Log("Change user password")
        crypt_id = Config.get("Provisioning.PasswordCryptId")
        if crypt_id is None:
            crypt_id = "6"

        salt_len = Config.get("Provisioning.PasswordCryptSaltLength")
        try:
            salt_len = int(salt_len)
            if salt_len < 0 or salt_len > 10:
                salt_len = 10
        except (ValueError, TypeError):
            salt_len = 10

        return self.chpasswd(user, password, crypt_id=crypt_id,
                             salt_len=salt_len)

    def chpasswd(self, username, password, crypt_id=6, salt_len=10):
        passwd_hash = self.gen_password_hash(password, crypt_id, salt_len)
        cmd = "usermod -p '{0}' {1}".format(passwd_hash, username)
        ret, output = RunGetOutput(cmd, log_cmd=False)
        if ret != 0:
            return "Failed to set password for {0}: {1}".format(username, output)

    def gen_password_hash(self, password, crypt_id, salt_len):
        collection = string.ascii_letters + string.digits
        salt = ''.join(random.choice(collection) for _ in range(salt_len))
        salt = "${0}${1}".format(crypt_id, salt)
        return crypt.crypt(password, salt)

    def load_ata_piix(self):
        return WaAgent.TryLoadAtapiix()

    def unload_ata_piix(self):
        """
        Generic function to remove ata_piix.ko.
        """
        return WaAgent.TryUnloadAtapiix()

    def deprovisionWarnUser(self):
        """
        Generic user warnings used at deprovision.
        """
        print("WARNING! Nameserver configuration in /etc/resolv.conf will be deleted.")

    def deprovisionDeleteFiles(self):
        """
        Files to delete when VM is deprovisioned
        """
        for a in VarLibDhcpDirectories:
            Run("rm -f " + a + "/*")

        # Clear LibDir, remove nameserver and root bash history

        for f in os.listdir(LibDir) + self.fileBlackList:
            try:
                os.remove(f)
            except:
                pass
        return 0

    def uninstallDeleteFiles(self):
        """
        Files to delete when agent is uninstalled.
        """
        for f in self.agent_files_to_uninstall:
            try:
                os.remove(f)
            except:
                pass
        return 0

    def checkDependencies(self):
        """
        Generic dependency check.
        Return 1 unless all dependencies are satisfied.
        """
        if self.checkPackageInstalled('NetworkManager'):
            Error(GuestAgentLongName + " is not compatible with network-manager.")
            return 1
        try:
            m = __import__('pyasn1')
        except ImportError:
            Error(GuestAgentLongName + " requires python-pyasn1 for your Linux distribution.")
            return 1
        for a in self.requiredDeps:
            if Run("which " + a + " > /dev/null 2>&1", chk_err=False):
                Error("Missing required dependency: " + a)
                return 1
        return 0

    def packagedInstall(self, buildroot):
        """
        Called from setup.py for use by RPM.
        Copies generated files waagent.conf, under the buildroot.
        """
        if not os.path.exists(buildroot + '/etc'):
            os.mkdir(buildroot + '/etc')
        SetFileContents(buildroot + '/etc/waagent.conf', MyDistro.waagent_conf_file)

        if not os.path.exists(buildroot + '/etc/logrotate.d'):
            os.mkdir(buildroot + '/etc/logrotate.d')
        SetFileContents(buildroot + '/etc/logrotate.d/waagent', WaagentLogrotate)

        self.init_script_file = buildroot + self.init_script_file
        # this allows us to call installAgentServiceScriptFiles()
        if not os.path.exists(os.path.dirname(self.init_script_file)):
            os.mkdir(os.path.dirname(self.init_script_file))
        self.installAgentServiceScriptFiles()

    def RestartInterface(self, iface, max_retry=3):
        for retry in range(1, max_retry + 1):
            ret = Run("ifdown " + iface + " && ifup " + iface)
            if ret == 0:
                return
            Log("Failed to restart interface: {0}, ret={1}".format(iface, ret))
            if retry < max_retry:
                Log("Retry restart interface in 5 seconds")
                time.sleep(5)

    def CreateAccount(self, user, password, expiration, thumbprint):
        return CreateAccount(user, password, expiration, thumbprint)

    def DeleteAccount(self, user):
        return DeleteAccount(user)


    def Install(self):
        return Install()

    def mediaHasFilesystem(self, dsk):
        if len(dsk) == 0:
            return False
        if Run("LC_ALL=C fdisk -l " + dsk + " | grep Disk"):
            return False
        return True

    def mountDVD(self, dvd, location):
        return RunGetOutput(self.mount_dvd_cmd + ' ' + dvd + ' ' + location)

    def GetHome(self):
        return GetHome()

    def getDhcpClientName(self):
        return self.dhcp_client_name

    def initScsiDiskTimeout(self):
        """
        Set the SCSI disk timeout when the agent starts running
        """
        self.setScsiDiskTimeout()

    def setScsiDiskTimeout(self):
        """
        Iterate all SCSI disks(include hot-add) and set their timeout if their value are different from the OS.RootDeviceScsiTimeout
        """
        try:
            scsiTimeout = Config.get("OS.RootDeviceScsiTimeout")
            for diskName in [disk for disk in os.listdir("/sys/block") if disk.startswith("sd")]:
                self.setBlockDeviceTimeout(diskName, scsiTimeout)
        except:
            pass

    def setBlockDeviceTimeout(self, device, timeout):
        """
        Set SCSI disk timeout by set /sys/block/sd*/device/timeout
        """
        if timeout != None and device:
            filePath = "/sys/block/" + device + "/device/timeout"
            if (GetFileContents(filePath).splitlines()[0].rstrip() != timeout):
                SetFileContents(filePath, timeout)
                Log("SetBlockDeviceTimeout: Update the device " + device + " with timeout " + timeout)

    def waitForSshHostKey(self, path):
        """
        Provide a dummy waiting, since by default, ssh host key is created by waagent and the key
        should already been created.
        """
        if (os.path.isfile(path)):
            return True
        else:
            Error("Can't find host key: {0}".format(path))
            return False

    def isDHCPEnabled(self):
        return self.dhcp_enabled

    def stopDHCP(self):
        """
        Stop the system DHCP client so that the agent can bind on its port. If
        the distro has set dhcp_enabled to True, it will need to provide an
        implementation of this method.
        """
        raise NotImplementedError('stopDHCP method missing')

    def startDHCP(self):
        """
        Start the system DHCP client. If the distro has set dhcp_enabled to
        True, it will need to provide an implementation of this method.
        """
        raise NotImplementedError('startDHCP method missing')

    def translateCustomData(self, data):
        """
        Translate the custom data from a Base64 encoding. Default to no-op.
        """
        decodeCustomData = Config.get("Provisioning.DecodeCustomData")
        if decodeCustomData != None and decodeCustomData.lower().startswith("y"):
            return base64.b64decode(data)
        return data

    def getConfigurationPath(self):
        return "/etc/waagent.conf"

    def getProcessorCores(self):
        return int(RunGetOutput("grep 'processor.*:' /proc/cpuinfo |wc -l")[1])

    def getTotalMemory(self):
        return int(RunGetOutput("grep MemTotal /proc/meminfo |awk '{print $2}'")[1]) / 1024

    def getInterfaceNameByMac(self, mac):
        ret, output = RunGetOutput("ifconfig -a")
        if ret != 0:
            raise Exception("Failed to get network interface info")
        output = output.replace('\n', '')
        match = re.search(r"(eth\d).*(HWaddr|ether) {0}".format(mac),
                          output, re.IGNORECASE)
        if match is None:
            raise Exception("Failed to get ifname with mac: {0}".format(mac))
        output = match.group(0)
        eths = re.findall(r"eth\d", output)
        if eths is None or len(eths) == 0:
            raise Exception("Failed to get ifname with mac: {0}".format(mac))
        return eths[-1]

    def configIpV4(self, ifName, addr, netmask=24):
        ret, output = RunGetOutput("ifconfig {0} up".format(ifName))
        if ret != 0:
            raise Exception("Failed to bring up {0}: {1}".format(ifName,
                                                                 output))
        ret, output = RunGetOutput("ifconfig {0} {1}/{2}".format(ifName, addr,
                                                                 netmask))
        if ret != 0:
            raise Exception("Failed to config ipv4 for {0}: {1}".format(ifName,
                                                                        output))

    def setDefaultGateway(self, gateway):
        Run("/sbin/route add default gw" + gateway, chk_err=False)

    def routeAdd(self, net, mask, gateway):
        Run("/sbin/route add -net " + net + " netmask " + mask + " gw " + gateway,
            chk_err=False)


############################################################
#	GentooDistro
############################################################
gentoo_init_file = """\
#!/sbin/runscript

command=/usr/sbin/waagent
pidfile=/var/run/waagent.pid
command_args=-daemon
command_background=true
name="Azure Linux Agent"

depend()
{
	need localmount
	use logger network
	after bootmisc modules
}

"""


class gentooDistro(AbstractDistro):
    """
    Gentoo distro concrete class
    """

    def __init__(self):  #
        super(gentooDistro, self).__init__()
        self.service_cmd = '/sbin/service'
        self.ssh_service_name = 'sshd'
        self.hostname_file_path = '/etc/conf.d/hostname'
        self.dhcp_client_name = 'dhcpcd'
        self.shadow_file_mode = 0o640
        self.init_file = gentoo_init_file

    def publishHostname(self, name):
        try:
            if (os.path.isfile(self.hostname_file_path)):
                r = ReplaceFileContentsAtomic(self.hostname_file_path, "hostname=\"" + name + "\"\n"
                                              + "\n".join(filter(lambda a: not a.startswith("hostname="),
                                                                 GetFileContents(self.hostname_file_path).split("\n"))))
        except:
            return 1
        return r

    def installAgentServiceScriptFiles(self):
        SetFileContents(self.init_script_file, self.init_file)
        os.chmod(self.init_script_file, 0o755)

    def registerAgentService(self):
        self.installAgentServiceScriptFiles()
        return Run('rc-update add ' + self.agent_service_name + ' default')

    def uninstallAgentService(self):
        return Run('rc-update del ' + self.agent_service_name + ' default')

    def unregisterAgentService(self):
        self.stopAgentService()
        return self.uninstallAgentService()

    def checkPackageInstalled(self, p):
        if Run('eix -I ^' + p + '$', chk_err=False):
            return 0
        else:
            return 1

    def checkPackageUpdateable(self, p):
        if Run('eix -u ^' + p + '$', chk_err=False):
            return 0
        else:
            return 1

    def RestartInterface(self, iface):
        Run("/etc/init.d/net." + iface + " restart")


############################################################
#	SuSEDistro
############################################################
suse_init_file = """\
#! /bin/sh
#
# Azure Linux Agent sysV init script
#
# Copyright 2013 Microsoft Corporation
# Copyright SUSE LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# /etc/init.d/waagent
#
#  and symbolic link
#
# /usr/sbin/rcwaagent
#
# System startup script for the waagent
#
### BEGIN INIT INFO
# Provides: AzureLinuxAgent
# Required-Start: $network sshd
# Required-Stop: $network sshd
# Default-Start: 3 5
# Default-Stop: 0 1 2 6
# Description: Start the AzureLinuxAgent
### END INIT INFO

PYTHON=/usr/bin/python
WAZD_BIN=/usr/sbin/waagent
WAZD_CONF=/etc/waagent.conf
WAZD_PIDFILE=/var/run/waagent.pid

test -x "$WAZD_BIN" || { echo "$WAZD_BIN not installed"; exit 5; }
test -e "$WAZD_CONF" || { echo "$WAZD_CONF not found"; exit 6; }

. /etc/rc.status

# First reset status of this service
rc_reset

# Return values acc. to LSB for all commands but status:
# 0 - success
# 1 - misc error
# 2 - invalid or excess args
# 3 - unimplemented feature (e.g. reload)
# 4 - insufficient privilege
# 5 - program not installed
# 6 - program not configured
#
# Note that starting an already running service, stopping
# or restarting a not-running service as well as the restart
# with force-reload (in case signalling is not supported) are
# considered a success.


case "$1" in
    start)
        echo -n "Starting AzureLinuxAgent"
        ## Start daemon with startproc(8). If this fails
        ## the echo return value is set appropriate.
        startproc -f ${PYTHON} ${WAZD_BIN} -daemon
        rc_status -v
        ;;
    stop)
        echo -n "Shutting down AzureLinuxAgent"
        ## Stop daemon with killproc(8) and if this fails
        ## set echo the echo return value.
        killproc -p ${WAZD_PIDFILE} ${PYTHON} ${WAZD_BIN}
        rc_status -v
        ;;
    try-restart)
        ## Stop the service and if this succeeds (i.e. the
        ## service was running before), start it again.
        $0 status >/dev/null && $0 restart
        rc_status
        ;;
    restart)
        ## Stop the service and regardless of whether it was
        ## running or not, start it again.
        $0 stop
        sleep 1
        $0 start
        rc_status
        ;;
    force-reload|reload)
        rc_status
        ;;
    status)
        echo -n "Checking for service AzureLinuxAgent "
        ## Check status with checkproc(8), if process is running
        ## checkproc will return with exit status 0.

        checkproc -p ${WAZD_PIDFILE} ${PYTHON} ${WAZD_BIN}
        rc_status -v
        ;;
    probe)
        ;;
    *)
        echo "Usage: $0 {start|stop|status|try-restart|restart|force-reload|reload}"
        exit 1
        ;;
esac
rc_exit
"""


class SuSEDistro(AbstractDistro):
    """
    SuSE Distro concrete class
    Put SuSE specific behavior here...
    """

    def __init__(self):
        super(SuSEDistro, self).__init__()
        self.service_cmd = '/sbin/service'
        self.ssh_service_name = 'sshd'
        self.kernel_boot_options_file = '/boot/grub/menu.lst'
        self.hostname_file_path = '/etc/HOSTNAME'
        self.requiredDeps += ["/sbin/insserv"]
        self.init_file = suse_init_file
        self.dhcp_client_name = 'dhcpcd'
        if ((DistInfo(fullname=1)[0] == 'SUSE Linux Enterprise Server' and DistInfo()[1] >= '12') or \
                    (DistInfo(fullname=1)[0] == 'openSUSE' and DistInfo()[1] >= '13.2')):
            self.dhcp_client_name = 'wickedd-dhcp4'
        self.grubKernelBootOptionsFile = '/boot/grub/menu.lst'
        self.grubKernelBootOptionsLine = 'kernel'
        self.getpidcmd = 'pidof '
        self.dhcp_enabled = True

    def checkPackageInstalled(self, p):
        if Run("rpm -q " + p, chk_err=False):
            return 0
        else:
            return 1

    def checkPackageUpdateable(self, p):
        if Run("zypper list-updates | grep " + p, chk_err=False):
            return 1
        else:
            return 0

    def installAgentServiceScriptFiles(self):
        try:
            SetFileContents(self.init_script_file, self.init_file)
            os.chmod(self.init_script_file, 0o744)
        except:
            pass

    def registerAgentService(self):
        self.installAgentServiceScriptFiles()
        return Run('insserv ' + self.agent_service_name)

    def uninstallAgentService(self):
        return Run('insserv -r ' + self.agent_service_name)

    def unregisterAgentService(self):
        self.stopAgentService()
        return self.uninstallAgentService()

    def startDHCP(self):
        Run("service " + self.dhcp_client_name + " start", chk_err=False)

    def stopDHCP(self):
        Run("service " + self.dhcp_client_name + " stop", chk_err=False)


############################################################
#	redhatDistro
############################################################

redhat_init_file = """\
#!/bin/bash
#
# Init file for AzureLinuxAgent.
#
# chkconfig: 2345 60 80
# description: AzureLinuxAgent
#

# source function library
. /etc/rc.d/init.d/functions

RETVAL=0
FriendlyName="AzureLinuxAgent"
WAZD_BIN=/usr/sbin/waagent

start()
{
    echo -n $"Starting $FriendlyName: "
    $WAZD_BIN -daemon &
}

stop()
{
    echo -n $"Stopping $FriendlyName: "
    killproc -p /var/run/waagent.pid $WAZD_BIN
    RETVAL=$?
    echo
    return $RETVAL
}

case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        start
        ;;
    reload)
        ;;
    report)
        ;;
    status)
        status $WAZD_BIN
        RETVAL=$?
        ;;
    *)
        echo $"Usage: $0 {start|stop|restart|status}"
        RETVAL=1
esac
exit $RETVAL
"""


class redhatDistro(AbstractDistro):
    """
    Redhat Distro concrete class
    Put Redhat specific behavior here...
    """

    def __init__(self):
        super(redhatDistro, self).__init__()
        self.service_cmd = '/sbin/service'
        self.ssh_service_restart_option = 'condrestart'
        self.ssh_service_name = 'sshd'
        self.hostname_file_path = None if DistInfo()[1] < '7.0' else '/etc/hostname'
        self.init_file = redhat_init_file
        self.grubKernelBootOptionsFile = '/boot/grub/menu.lst'
        self.grubKernelBootOptionsLine = 'kernel'

    def publishHostname(self, name):
        super(redhatDistro, self).publishHostname(name)
        if DistInfo()[1] < '7.0':
            filepath = "/etc/sysconfig/network"
            if os.path.isfile(filepath):
                ReplaceFileContentsAtomic(filepath, "HOSTNAME=" + name + "\n"
                                          + "\n".join(
                    filter(lambda a: not a.startswith("HOSTNAME"), GetFileContents(filepath).split('\n'))))

        ethernetInterface = MyDistro.GetInterfaceName()
        filepath = "/etc/sysconfig/network-scripts/ifcfg-" + ethernetInterface
        if os.path.isfile(filepath):
            ReplaceFileContentsAtomic(filepath, "DHCP_HOSTNAME=" + name + "\n"
                                      + "\n".join(
                filter(lambda a: not a.startswith("DHCP_HOSTNAME"), GetFileContents(filepath).split('\n'))))
        return 0

    def installAgentServiceScriptFiles(self):
        SetFileContents(self.init_script_file, self.init_file)
        os.chmod(self.init_script_file, 0o744)
        return 0

    def registerAgentService(self):
        self.installAgentServiceScriptFiles()
        return Run('chkconfig --add waagent')

    def uninstallAgentService(self):
        return Run('chkconfig --del ' + self.agent_service_name)

    def unregisterAgentService(self):
        self.stopAgentService()
        return self.uninstallAgentService()

    def checkPackageInstalled(self, p):
        if Run("yum list installed " + p, chk_err=False):
            return 0
        else:
            return 1

    def checkPackageUpdateable(self, p):
        if Run("yum check-update | grep " + p, chk_err=False):
            return 1
        else:
            return 0

    def checkDependencies(self):
        """
        Generic dependency check.
        Return 1 unless all dependencies are satisfied.
        """
        if DistInfo()[1] < '7.0' and self.checkPackageInstalled('NetworkManager'):
            Error(GuestAgentLongName + " is not compatible with network-manager.")
            return 1
        try:
            m = __import__('pyasn1')
        except ImportError:
            Error(GuestAgentLongName + " requires python-pyasn1 for your Linux distribution.")
            return 1
        for a in self.requiredDeps:
            if Run("which " + a + " > /dev/null 2>&1", chk_err=False):
                Error("Missing required dependency: " + a)
                return 1
        return 0


############################################################
#	centosDistro
############################################################

class centosDistro(redhatDistro):
    """
    CentOS Distro concrete class
    Put CentOS specific behavior here...
    """

    def __init__(self):
        super(centosDistro, self).__init__()


############################################################
#   eulerosDistro
############################################################

class eulerosDistro(redhatDistro):
    """
    EulerOS Distro concrete class
    Put EulerOS specific behavior here...
    """

    def __init__(self):
        super(eulerosDistro, self).__init__()


############################################################
#	oracleDistro
############################################################

class oracleDistro(redhatDistro):
    """
    Oracle Distro concrete class
    Put Oracle specific behavior here...
    """

    def __init__(self):
        super(oracleDistro, self).__init__()


############################################################
#	asianuxDistro
############################################################

class asianuxDistro(redhatDistro):
    """
    Asianux Distro concrete class
    Put Asianux specific behavior here...
    """

    def __init__(self):
        super(asianuxDistro, self).__init__()


############################################################
#   CoreOSDistro
############################################################

class CoreOSDistro(AbstractDistro):
    """
    CoreOS Distro concrete class
    Put CoreOS specific behavior here...
    """
    CORE_UID = 500

    def __init__(self):
        super(CoreOSDistro, self).__init__()
        self.requiredDeps += ["/usr/bin/systemctl"]
        self.agent_service_name = 'waagent'
        self.init_script_file = '/etc/systemd/system/waagent.service'
        self.fileBlackList.append("/etc/machine-id")
        self.dhcp_client_name = 'systemd-networkd'
        self.getpidcmd = 'pidof '
        self.shadow_file_mode = 0o640
        self.waagent_path = '/usr/share/oem/bin'
        self.python_path = '/usr/share/oem/python/bin'
        self.dhcp_enabled = True
        if 'PATH' in os.environ:
            os.environ['PATH'] = "{0}:{1}".format(os.environ['PATH'], self.python_path)
        else:
            os.environ['PATH'] = self.python_path

        if 'PYTHONPATH' in os.environ:
            os.environ['PYTHONPATH'] = "{0}:{1}".format(os.environ['PYTHONPATH'], self.waagent_path)
        else:
            os.environ['PYTHONPATH'] = self.waagent_path

    def checkPackageInstalled(self, p):
        """
        There is no package manager in CoreOS.  Return 1 since it must be preinstalled.
        """
        return 1

    def checkDependencies(self):
        for a in self.requiredDeps:
            if Run("which " + a + " > /dev/null 2>&1", chk_err=False):
                Error("Missing required dependency: " + a)
                return 1
        return 0

    def checkPackageUpdateable(self, p):
        """
        There is no package manager in CoreOS.  Return 0 since it can't be updated via package.
        """
        return 0

    def startAgentService(self):
        return Run('systemctl start ' + self.agent_service_name)

    def stopAgentService(self):
        return Run('systemctl stop ' + self.agent_service_name)

    def restartSshService(self):
        """
        SSH is socket activated on CoreOS. No need to restart it.
        """
        return 0

    def sshDeployPublicKey(self, fprint, path):
        """
        We support PKCS8.
        """
        if Run("ssh-keygen -i -m PKCS8 -f " + fprint + " >> " + path):
            return 1
        else:
            return 0

    def RestartInterface(self, iface):
        Run("systemctl restart systemd-networkd")

    def CreateAccount(self, user, password, expiration, thumbprint):
        """
        Create a user account, with 'user', 'password', 'expiration', ssh keys
        and sudo permissions.
        Returns None if successful, error string on failure.
        """
        userentry = None
        try:
            userentry = pwd.getpwnam(user)
        except:
            pass
        uidmin = None
        try:
            uidmin = int(GetLineStartingWith("UID_MIN", "/etc/login.defs").split()[1])
        except:
            pass
        if uidmin == None:
            uidmin = 100
        if userentry != None and userentry[2] < uidmin and userentry[2] != self.CORE_UID:
            Error("CreateAccount: " + user + " is a system user. Will not set password.")
            return "Failed to set password for system user: " + user + " (0x06)."
        if userentry == None:
            command = "useradd --create-home --password '*' " + user
            if expiration != None:
                command += " --expiredate " + expiration.split('.')[0]
            if Run(command):
                Error("Failed to create user account: " + user)
                return "Failed to create user account: " + user + " (0x07)."
        else:
            Log("CreateAccount: " + user + " already exists. Will update password.")
        if password != None:
            self.changePass(user, password)
        try:
            if password == None:
                SetFileContents("/etc/sudoers.d/waagent", user + " ALL = (ALL) NOPASSWD: ALL\n")
            else:
                SetFileContents("/etc/sudoers.d/waagent", user + " ALL = (ALL) ALL\n")
            os.chmod("/etc/sudoers.d/waagent", 0o440)
        except:
            Error("CreateAccount: Failed to configure sudo access for user.")
            return "Failed to configure sudo privileges (0x08)."
        home = MyDistro.GetHome()
        if thumbprint != None:
            dir = home + "/" + user + "/.ssh"
            CreateDir(dir, user, 0o700)
            pub = dir + "/id_rsa.pub"
            prv = dir + "/id_rsa"
            Run("ssh-keygen -y -f " + thumbprint + ".prv > " + pub)
            SetFileContents(prv, GetFileContents(thumbprint + ".prv"))
            for f in [pub, prv]:
                os.chmod(f, 0o600)
                ChangeOwner(f, user)
            SetFileContents(dir + "/authorized_keys", GetFileContents(pub))
            ChangeOwner(dir + "/authorized_keys", user)
        Log("Created user account: " + user)
        return None

    def startDHCP(self):
        Run("systemctl start " + self.dhcp_client_name, chk_err=False)

    def stopDHCP(self):
        Run("systemctl stop " + self.dhcp_client_name, chk_err=False)

    def translateCustomData(self, data):
        return base64.b64decode(data)

    def getConfigurationPath(self):
        return "/usr/share/oem/waagent.conf"


############################################################
#	debianDistro
############################################################
debian_init_file = """\
#!/bin/sh
### BEGIN INIT INFO
# Provides:          AzureLinuxAgent
# Required-Start:    $network $syslog
# Required-Stop:     $network $syslog
# Should-Start:      $network $syslog
# Should-Stop:       $network $syslog
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: AzureLinuxAgent
# Description:       AzureLinuxAgent
### END INIT INFO

. /lib/lsb/init-functions

OPTIONS="-daemon"
WAZD_BIN=/usr/sbin/waagent
WAZD_PID=/var/run/waagent.pid

case "$1" in
    start)
        log_begin_msg "Starting AzureLinuxAgent..."
        pid=$( pidofproc $WAZD_BIN )
        if [ -n "$pid" ] ; then
              log_begin_msg "Already running."
              log_end_msg 0
              exit 0
        fi
        start-stop-daemon --start --quiet --oknodo --background --exec $WAZD_BIN -- $OPTIONS
        log_end_msg $?
        ;;

    stop)
        log_begin_msg "Stopping AzureLinuxAgent..."
        start-stop-daemon --stop --quiet --oknodo --pidfile $WAZD_PID
        ret=$?
        rm -f $WAZD_PID
        log_end_msg $ret
        ;;
    force-reload)
        $0 restart
        ;;
    restart)
        $0 stop
        $0 start
        ;;
    status)
        status_of_proc $WAZD_BIN && exit 0 || exit $?
        ;;
    *)
        log_success_msg "Usage: /etc/init.d/waagent {start|stop|force-reload|restart|status}"
        exit 1
        ;;
esac

exit 0
"""


class debianDistro(AbstractDistro):
    """
    debian Distro concrete class
    Put debian specific behavior here...
    """

    def __init__(self):
        super(debianDistro, self).__init__()
        self.requiredDeps += ["/usr/sbin/update-rc.d"]
        self.init_file = debian_init_file
        self.agent_package_name = 'walinuxagent'
        self.dhcp_client_name = 'dhclient'
        self.getpidcmd = 'pidof '
        self.shadow_file_mode = 0o640

    def checkPackageInstalled(self, p):
        """
        Check that the package is installed.
        Return 1 if installed, 0 if not installed.
        This method of using dpkg-query
        allows wildcards to be present in the
        package name.
        """
        if not Run("dpkg-query -W -f='${Status}\n' '" + p + "' | grep ' installed' 2>&1", chk_err=False):
            return 1
        else:
            return 0

    def checkDependencies(self):
        """
        Debian dependency check.  python-pyasn1 is NOT needed.
        Return 1 unless all dependencies are satisfied.
        NOTE: using network*manager will catch either package name in Ubuntu or debian.
        """
        if self.checkPackageInstalled('network*manager'):
            Error(GuestAgentLongName + " is not compatible with network-manager.")
            return 1
        for a in self.requiredDeps:
            if Run("which " + a + " > /dev/null 2>&1", chk_err=False):
                Error("Missing required dependency: " + a)
                return 1
        return 0

    def checkPackageUpdateable(self, p):
        if Run("apt-get update ; apt-get upgrade -us | grep " + p, chk_err=False):
            return 1
        else:
            return 0

    def installAgentServiceScriptFiles(self):
        """
        If we are packaged - the service name is walinuxagent, do nothing.
        """
        if self.agent_service_name == 'walinuxagent':
            return 0
        try:
            SetFileContents(self.init_script_file, self.init_file)
            os.chmod(self.init_script_file, 0o744)
        except OSError as e:
            ErrorWithPrefix('installAgentServiceScriptFiles',
                            'Exception: ' + str(e) + ' occured creating ' + self.init_script_file)
            return 1
        return 0

    def registerAgentService(self):
        if self.installAgentServiceScriptFiles() == 0:
            return Run('update-rc.d waagent defaults')
        else:
            return 1

    def uninstallAgentService(self):
        return Run('update-rc.d -f ' + self.agent_service_name + ' remove')

    def unregisterAgentService(self):
        self.stopAgentService()
        return self.uninstallAgentService()

    def sshDeployPublicKey(self, fprint, path):
        """
        We support PKCS8.
        """
        if Run("ssh-keygen -i -m PKCS8 -f " + fprint + " >> " + path):
            return 1
        else:
            return 0


############################################################
#	KaliDistro - WIP
#       Functioning on Kali 1.1.0a so far
############################################################
class KaliDistro(debianDistro):
    """
    Kali Distro concrete class
    Put Kali specific behavior here...
    """

    def __init__(self):
        super(KaliDistro, self).__init__()


############################################################
#	UbuntuDistro
############################################################
ubuntu_upstart_file = """\
#walinuxagent - start Azure agent

description "walinuxagent"
author "Ben Howard <ben.howard@canonical.com>"

start on (filesystem and started rsyslog)

pre-start script

	WALINUXAGENT_ENABLED=1
    [ -r /etc/default/walinuxagent ] && . /etc/default/walinuxagent

    if [ "$WALINUXAGENT_ENABLED" != "1" ]; then
        exit 1
    fi

    if [ ! -x /usr/sbin/waagent ]; then
        exit 1
    fi

    #Load the udf module
    modprobe -b udf
end script

exec /usr/sbin/waagent -daemon
"""


class UbuntuDistro(debianDistro):
    """
    Ubuntu Distro concrete class
    Put Ubuntu specific behavior here...
    """

    def __init__(self):
        super(UbuntuDistro, self).__init__()
        self.init_script_file = '/etc/init/waagent.conf'
        self.init_file = ubuntu_upstart_file
        self.fileBlackList = ["/root/.bash_history", "/var/log/waagent.log"]
        self.dhcp_client_name = None
        self.getpidcmd = 'pidof '

    def registerAgentService(self):
        return self.installAgentServiceScriptFiles()

    def uninstallAgentService(self):
        """
        If we are packaged - the service name is walinuxagent, do nothing.
        """
        if self.agent_service_name == 'walinuxagent':
            return 0
        os.remove('/etc/init/' + self.agent_service_name + '.conf')

    def unregisterAgentService(self):
        """
        If we are packaged - the service name is walinuxagent, do nothing.
        """
        if self.agent_service_name == 'walinuxagent':
            return
        self.stopAgentService()
        return self.uninstallAgentService()

    def deprovisionWarnUser(self):
        """
        Ubuntu specific warning string from Deprovision.
        """
        print("WARNING! Nameserver configuration in /etc/resolvconf/resolv.conf.d/{tail,original} will be deleted.")

    def deprovisionDeleteFiles(self):
        """
        Ubuntu uses resolv.conf by default, so removing /etc/resolv.conf will
        break resolvconf. Therefore, we check to see if resolvconf is in use,
        and if so, we remove the resolvconf artifacts.
        """
        if os.path.realpath('/etc/resolv.conf') != '/run/resolvconf/resolv.conf':
            Log("resolvconf is not configured. Removing /etc/resolv.conf")
            self.fileBlackList.append('/etc/resolv.conf')
        else:
            Log("resolvconf is enabled; leaving /etc/resolv.conf intact")
            resolvConfD = '/etc/resolvconf/resolv.conf.d/'
            self.fileBlackList.extend([resolvConfD + 'tail', resolvConfD + 'original'])
        for f in os.listdir(LibDir) + self.fileBlackList:
            try:
                os.remove(f)
            except:
                pass
        return 0

    def getDhcpClientName(self):
        if self.dhcp_client_name != None:
            return self.dhcp_client_name
        if DistInfo()[1] == '12.04':
            self.dhcp_client_name = 'dhclient3'
        else:
            self.dhcp_client_name = 'dhclient'
        return self.dhcp_client_name

    def waitForSshHostKey(self, path):
        """
        Wait until the ssh host key is generated by cloud init.
        """
        for retry in range(0, 10):
            if (os.path.isfile(path)):
                return True
            time.sleep(1)
        Error("Can't find host key: {0}".format(path))
        return False


############################################################
#	LinuxMintDistro
############################################################

class LinuxMintDistro(UbuntuDistro):
    """
    LinuxMint Distro concrete class
    Put LinuxMint specific behavior here...
    """

    def __init__(self):
        super(LinuxMintDistro, self).__init__()

############################################################
#      DefaultDistro
############################################################

class DefaultDistro(UbuntuDistro):
    """
    Default Distro concrete class
    Put Default distro specific behavior here...
    """

    def __init__(self):
        super(DefaultDistro, self).__init__()

############################################################
#	fedoraDistro
############################################################
fedora_systemd_service = """\
[Unit]
Description=Azure Linux Agent
After=network.target
After=sshd.service
ConditionFileIsExecutable=/usr/sbin/waagent
ConditionPathExists=/etc/waagent.conf

[Service]
Type=simple
ExecStart=/usr/sbin/waagent -daemon

[Install]
WantedBy=multi-user.target
"""


class fedoraDistro(redhatDistro):
    """
    FedoraDistro concrete class
    Put Fedora specific behavior here...
    """

    def __init__(self):
        super(fedoraDistro, self).__init__()
        self.service_cmd = '/usr/bin/systemctl'
        self.hostname_file_path = '/etc/hostname'
        self.init_script_file = '/usr/lib/systemd/system/' + self.agent_service_name + '.service'
        self.init_file = fedora_systemd_service
        self.grubKernelBootOptionsFile = '/etc/default/grub'
        self.grubKernelBootOptionsLine = 'GRUB_CMDLINE_LINUX='

    def publishHostname(self, name):
        SetFileContents(self.hostname_file_path, name + '\n')
        ethernetInterface = MyDistro.GetInterfaceName()
        filepath = "/etc/sysconfig/network-scripts/ifcfg-" + ethernetInterface
        if os.path.isfile(filepath):
            ReplaceFileContentsAtomic(filepath, "DHCP_HOSTNAME=" + name + "\n"
                                      + "\n".join(
                filter(lambda a: not a.startswith("DHCP_HOSTNAME"), GetFileContents(filepath).split('\n'))))
        return 0

    def installAgentServiceScriptFiles(self):
        SetFileContents(self.init_script_file, self.init_file)
        os.chmod(self.init_script_file, 0o644)
        return Run(self.service_cmd + ' daemon-reload')

    def registerAgentService(self):
        self.installAgentServiceScriptFiles()
        return Run(self.service_cmd + ' enable ' + self.agent_service_name)

    def uninstallAgentService(self):
        """
        Call service subsystem to remove waagent script.
        """
        return Run(self.service_cmd + ' disable ' + self.agent_service_name)

    def unregisterAgentService(self):
        """
        Calls self.stopAgentService and call self.uninstallAgentService()
        """
        self.stopAgentService()
        self.uninstallAgentService()

    def startAgentService(self):
        """
        Service call to start the Agent service
        """
        return Run(self.service_cmd + ' start ' + self.agent_service_name)

    def stopAgentService(self):
        """
        Service call to stop the Agent service
        """
        return Run(self.service_cmd + ' stop ' + self.agent_service_name, False)

    def restartSshService(self):
        """
        Service call to re(start) the SSH service
        """
        sshRestartCmd = self.service_cmd + " " + self.ssh_service_restart_option + " " + self.ssh_service_name
        retcode = Run(sshRestartCmd)
        if retcode > 0:
            Error("Failed to restart SSH service with return code:" + str(retcode))
        return retcode



    def deleteRootPassword(self):
        return Run("/sbin/usermod root -p '!!'")

    def packagedInstall(self, buildroot):
        """
        Called from setup.py for use by RPM.
        Copies generated files waagent.conf, under the buildroot.
        """
        if not os.path.exists(buildroot + '/etc'):
            os.mkdir(buildroot + '/etc')
        SetFileContents(buildroot + '/etc/waagent.conf', MyDistro.waagent_conf_file)

        if not os.path.exists(buildroot + '/etc/logrotate.d'):
            os.mkdir(buildroot + '/etc/logrotate.d')
        SetFileContents(buildroot + '/etc/logrotate.d/WALinuxAgent', WaagentLogrotate)

        self.init_script_file = buildroot + self.init_script_file
        # this allows us to call installAgentServiceScriptFiles()
        if not os.path.exists(os.path.dirname(self.init_script_file)):
            os.mkdir(os.path.dirname(self.init_script_file))
        self.installAgentServiceScriptFiles()

    def CreateAccount(self, user, password, expiration, thumbprint):
        super(fedoraDistro, self).CreateAccount(user, password, expiration, thumbprint)
        Run('/sbin/usermod ' + user + ' -G wheel')

    def DeleteAccount(self, user):
        Run('/sbin/usermod ' + user + ' -G ""')
        super(fedoraDistro, self).DeleteAccount(user)


############################################################
#	FreeBSD
############################################################
FreeBSDWaagentConf = """\
#
# Azure Linux Agent Configuration
#

Role.StateConsumer=None                 # Specified program is invoked with the argument "Ready" when we report ready status
                                        # to the endpoint server.
Role.ConfigurationConsumer=None         # Specified program is invoked with XML file argument specifying role configuration.
Role.TopologyConsumer=None              # Specified program is invoked with XML file argument specifying role topology.

Provisioning.Enabled=y                  #
Provisioning.DeleteRootPassword=y       # Password authentication for root account will be unavailable.
Provisioning.RegenerateSshHostKeyPair=y # Generate fresh host key pair.
Provisioning.SshHostKeyPairType=rsa     # Supported values are "rsa", "dsa" and "ecdsa".
Provisioning.MonitorHostName=y          # Monitor host name changes and publish changes via DHCP requests.

ResourceDisk.Format=y                   # Format if unformatted. If 'n', resource disk will not be mounted.
ResourceDisk.Filesystem=ufs2            #
ResourceDisk.MountPoint=/mnt/resource   #
ResourceDisk.EnableSwap=n               # Create and use swapfile on resource disk.
ResourceDisk.SwapSizeMB=0               # Size of the swapfile.

LBProbeResponder=y                      # Respond to load balancer probes if requested by Azure.

Logs.Verbose=n                          # Enable verbose logs

OS.RootDeviceScsiTimeout=300            # Root device timeout in seconds.
OS.OpensslPath=None                     # If "None", the system default version is used.
"""

bsd_init_file = """\
#! /bin/sh

# PROVIDE: waagent
# REQUIRE: DAEMON cleanvar sshd
# BEFORE: LOGIN
# KEYWORD: nojail

. /etc/rc.subr
export PATH=$PATH:/usr/local/bin
name="waagent"
rcvar="waagent_enable"
command="/usr/sbin/${name}"
command_interpreter="/usr/local/bin/python"
waagent_flags=" daemon &"

pidfile="/var/run/waagent.pid"

load_rc_config $name
run_rc_command "$1"

"""
bsd_activate_resource_disk_txt = """\
#!/usr/bin/env python

import os
import sys
import imp

# waagent has no '.py' therefore create waagent module import manually.
__name__='setupmain' #prevent waagent.__main__ from executing
waagent=imp.load_source('waagent','/tmp/waagent') 
waagent.LoggerInit('/var/log/waagent.log','/dev/console')
from waagent import RunGetOutput,Run
Config=waagent.ConfigurationProvider(None)
format = Config.get("ResourceDisk.Format")
if format == None or format.lower().startswith("n"):
    sys.exit(0)
device_base = 'da1'
device = "/dev/" + device_base
for entry in RunGetOutput("mount")[1].split():
    if entry.startswith(device + "s1"):
        waagent.Log("ActivateResourceDisk: " + device + "s1 is already mounted.")
        sys.exit(0)
mountpoint = Config.get("ResourceDisk.MountPoint")
if mountpoint == None:
    mountpoint = "/mnt/resource"
waagent.CreateDir(mountpoint, "root", 0755)
fs = Config.get("ResourceDisk.Filesystem")
if waagent.FreeBSDDistro().mediaHasFilesystem(device) == False :
    Run("newfs " + device + "s1")
if Run("mount " + device + "s1 " + mountpoint):
    waagent.Error("ActivateResourceDisk: Failed to mount resource disk (" + device + "s1).")
    sys.exit(0)
waagent.Log("Resource disk (" + device + "s1) is mounted at " + mountpoint + " with fstype " + fs)
waagent.SetFileContents(os.path.join(mountpoint,waagent.README_FILENAME), waagent.README_FILECONTENT)
swap = Config.get("ResourceDisk.EnableSwap")
if swap == None or swap.lower().startswith("n"):
    sys.exit(0)
sizeKB = int(Config.get("ResourceDisk.SwapSizeMB")) * 1024
if os.path.isfile(mountpoint + "/swapfile") and os.path.getsize(mountpoint + "/swapfile") != (sizeKB * 1024):
    os.remove(mountpoint + "/swapfile")
if not os.path.isfile(mountpoint + "/swapfile"):
    Run("umask 0077 && dd if=/dev/zero of=" + mountpoint + "/swapfile bs=1024 count=" + str(sizeKB))
if Run("mdconfig -a -t vnode -f " + mountpoint + "/swapfile -u 0"):
    waagent.Error("ActivateResourceDisk: Configuring swap - Failed to create md0")
if not Run("swapon /dev/md0"):
    waagent.Log("Enabled " + str(sizeKB) + " KB of swap at " + mountpoint + "/swapfile")
else:
    waagent.Error("ActivateResourceDisk: Failed to activate swap at " + mountpoint + "/swapfile")
"""


class FreeBSDDistro(AbstractDistro):
    """
    """

    def __init__(self):
        """
        Generic Attributes go here.  These are based on 'majority rules'.
        This __init__() may be called or overriden by the child.
        """
        super(FreeBSDDistro, self).__init__()
        self.agent_service_name = os.path.basename(sys.argv[0])
        self.selinux = False
        self.ssh_service_name = 'sshd'
        self.ssh_config_file = '/etc/ssh/sshd_config'
        self.hostname_file_path = '/etc/hostname'
        self.dhcp_client_name = 'dhclient'
        self.requiredDeps = ['route', 'shutdown', 'ssh-keygen', 'pw'
            , 'openssl', 'fdisk', 'sed', 'grep', 'sudo']
        self.init_script_file = '/etc/rc.d/waagent'
        self.init_file = bsd_init_file
        self.agent_package_name = 'WALinuxAgent'
        self.fileBlackList = ["/root/.bash_history", "/var/log/waagent.log", '/etc/resolv.conf']
        self.agent_files_to_uninstall = ["/etc/waagent.conf"]
        self.grubKernelBootOptionsFile = '/boot/loader.conf'
        self.grubKernelBootOptionsLine = ''
        self.getpidcmd = 'pgrep -n'
        self.mount_dvd_cmd = 'dd bs=2048 count=33 skip=295 if='  # custom data max len is 64k
        self.sudoers_dir_base = '/usr/local/etc'
        self.waagent_conf_file = FreeBSDWaagentConf

    def installAgentServiceScriptFiles(self):
        SetFileContents(self.init_script_file, self.init_file)
        os.chmod(self.init_script_file, 0o777)
        AppendFileContents("/etc/rc.conf", "waagent_enable='YES'\n")
        return 0

    def registerAgentService(self):
        self.installAgentServiceScriptFiles()
        return Run("services_mkdb " + self.init_script_file)

    def sshDeployPublicKey(self, fprint, path):
        """
        We support PKCS8.
        """
        if Run("ssh-keygen -i -m PKCS8 -f " + fprint + " >> " + path):
            return 1
        else:
            return 0

    def deleteRootPassword(self):
        """
        BSD root password removal.
        """
        filepath = "/etc/master.passwd"
        ReplaceStringInFile(filepath, r'root:.*?:', 'root::')
        # ReplaceFileContentsAtomic(filepath,"root:*LOCK*:14600::::::\n"
        #                          + "\n".join(filter(lambda a: not a.startswith("root:"),GetFileContents(filepath).split('\n'))))
        os.chmod(filepath, self.shadow_file_mode)
        if self.isSelinuxSystem():
            self.setSelinuxContext(filepath, 'system_u:object_r:shadow_t:s0')
        RunGetOutput("pwd_mkdb -u root /etc/master.passwd")
        Log("Root password deleted.")
        return 0

    def changePass(self, user, password):
        return RunSendStdin("pw usermod " + user + " -h 0 ", password, log_cmd=False)

    def load_ata_piix(self):
        return 0

    def unload_ata_piix(self):
        return 0

    def checkDependencies(self):
        """
        FreeBSD dependency check.
        Return 1 unless all dependencies are satisfied.
        """
        for a in self.requiredDeps:
            if Run("which " + a + " > /dev/null 2>&1", chk_err=False):
                Error("Missing required dependency: " + a)
                return 1
        return 0

    def packagedInstall(self, buildroot):
        pass

    def GetInterfaceName(self):
        """
        Return the ip of the 
        active ethernet interface.
        """
        iface, inet, mac = self.GetFreeBSDEthernetInfo()
        return iface

    def RestartInterface(self, iface):
        Run("service netif restart")

    def GetIpv4Address(self):
        """
        Return the ip of the 
        active ethernet interface.
        """
        iface, inet, mac = self.GetFreeBSDEthernetInfo()
        return inet

    def GetMacAddress(self):
        """
        Return the ip of the 
        active ethernet interface.
        """
        iface, inet, mac = self.GetFreeBSDEthernetInfo()
        l = mac.split(':')
        r = []
        for i in l:
            r.append(string.atoi(i, 16))
        return r

    def GetFreeBSDEthernetInfo(self):
        """
        There is no SIOCGIFCONF
        on freeBSD - just parse ifconfig.
        Returns strings: iface, inet4_addr, and mac
        or 'None,None,None' if unable to parse.
        We will sleep and retry as the network must be up.
        """
        code, output = RunGetOutput("ifconfig", chk_err=False)
        Log(output)
        retries = 10
        cmd = 'ifconfig | grep -A2 -B2 ether | grep -B3 inet | grep -A4 UP '
        code = 1

        while code > 0:
            if code > 0 and retries == 0:
                Error("GetFreeBSDEthernetInfo - Failed to detect ethernet interface")
                return None, None, None
            code, output = RunGetOutput(cmd, chk_err=False)
            retries -= 1
            if code > 0 and retries > 0:
                Log("GetFreeBSDEthernetInfo - Error: retry ethernet detection " + str(retries))
                if retries == 9:
                    c, o = RunGetOutput("ifconfig | grep -A1 -B2 ether", chk_err=False)
                    if c == 0:
                        t = o.replace('\n', ' ')
                        t = t.split()
                        i = t[0][:-1]
                        Log(RunGetOutput('id')[1])
                        Run('dhclient ' + i)
                time.sleep(10)

        j = output.replace('\n', ' ')
        j = j.split()
        iface = j[0][:-1]

        for i in range(len(j)):
            if j[i] == 'inet':
                inet = j[i + 1]
            elif j[i] == 'ether':
                mac = j[i + 1]

        return iface, inet, mac

    def CreateAccount(self, user, password, expiration, thumbprint):
        """
        Create a user account, with 'user', 'password', 'expiration', ssh keys
        and sudo permissions.
        Returns None if successful, error string on failure.
        """
        userentry = None
        try:
            userentry = pwd.getpwnam(user)
        except:
            pass
        uidmin = None
        try:
            if os.path.isfile("/etc/login.defs"):
                uidmin = int(GetLineStartingWith("UID_MIN", "/etc/login.defs").split()[1])
        except:
            pass
        if uidmin == None:
            uidmin = 100
        if userentry != None and userentry[2] < uidmin:
            Error("CreateAccount: " + user + " is a system user. Will not set password.")
            return "Failed to set password for system user: " + user + " (0x06)."
        if userentry == None:
            command = "pw useradd " + user + " -m"
            if expiration != None:
                command += " -e " + expiration.split('.')[0]
            if Run(command):
                Error("Failed to create user account: " + user)
                return "Failed to create user account: " + user + " (0x07)."
            else:
                Log("CreateAccount: " + user + " already exists. Will update password.")

        if password != None:
            self.changePass(user, password)
        try:
            # for older distros create sudoers.d
            if not os.path.isdir(MyDistro.sudoers_dir_base + '/sudoers.d/'):
                # create the /etc/sudoers.d/ directory
                os.mkdir(MyDistro.sudoers_dir_base + '/sudoers.d')
                # add the include of sudoers.d to the /etc/sudoers
                SetFileContents(MyDistro.sudoers_dir_base + '/sudoers', GetFileContents(
                    MyDistro.sudoers_dir_base + '/sudoers') + '\n#includedir ' + MyDistro.sudoers_dir_base + '/sudoers.d\n')
            if password == None:
                SetFileContents(MyDistro.sudoers_dir_base + "/sudoers.d/waagent", user + " ALL = (ALL) NOPASSWD: ALL\n")
            else:
                SetFileContents(MyDistro.sudoers_dir_base + "/sudoers.d/waagent", user + " ALL = (ALL) ALL\n")
            os.chmod(MyDistro.sudoers_dir_base + "/sudoers.d/waagent", 0o440)
        except:
            Error("CreateAccount: Failed to configure sudo access for user.")
            return "Failed to configure sudo privileges (0x08)."
        home = MyDistro.GetHome()
        if thumbprint != None:
            dir = home + "/" + user + "/.ssh"
            CreateDir(dir, user, 0o700)
            pub = dir + "/id_rsa.pub"
            prv = dir + "/id_rsa"
            Run("ssh-keygen -y -f " + thumbprint + ".prv > " + pub)
            SetFileContents(prv, GetFileContents(thumbprint + ".prv"))
            for f in [pub, prv]:
                os.chmod(f, 0o600)
                ChangeOwner(f, user)
            SetFileContents(dir + "/authorized_keys", GetFileContents(pub))
            ChangeOwner(dir + "/authorized_keys", user)
        Log("Created user account: " + user)
        return None

    def DeleteAccount(self, user):
        """
        Delete the 'user'.
        Clear utmp first, to avoid error.
        Removes the /etc/sudoers.d/waagent file.
        """
        userentry = None
        try:
            userentry = pwd.getpwnam(user)
        except:
            pass
        if userentry == None:
            Error("DeleteAccount: " + user + " not found.")
            return
        uidmin = None
        try:
            if os.path.isfile("/etc/login.defs"):
                uidmin = int(GetLineStartingWith("UID_MIN", "/etc/login.defs").split()[1])
        except:
            pass
        if uidmin == None:
            uidmin = 100
        if userentry[2] < uidmin:
            Error("DeleteAccount: " + user + " is a system user. Will not delete account.")
            return
        Run("> /var/run/utmp")  # Delete utmp to prevent error if we are the 'user' deleted
        pid = subprocess.Popen(['rmuser', '-y', user], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.PIPE).pid
        try:
            os.remove(MyDistro.sudoers_dir_base + "/sudoers.d/waagent")
        except:
            pass
        return

    def ActivateResourceDiskNoThread(self):
        """
        Format, mount, and if specified in the configuration
        set resource disk as swap.
        """
        global DiskActivated
        Run('cp /usr/sbin/waagent /tmp/')
        SetFileContents('/tmp/bsd_activate_resource_disk.py', bsd_activate_resource_disk_txt)
        Run('chmod +x /tmp/bsd_activate_resource_disk.py')
        pid = subprocess.Popen(["/tmp/bsd_activate_resource_disk.py", ""]).pid
        Log("Spawning bsd_activate_resource_disk.py")
        DiskActivated = True
        return

    def Install(self):
        """
        Install the agent service.
        Check dependencies.
        Create /etc/waagent.conf and move old version to
        /etc/waagent.conf.old
        Copy RulesFiles to /var/lib/waagent
        Create /etc/logrotate.d/waagent
        Set /etc/ssh/sshd_config ClientAliveInterval to 180
        Call ApplyVNUMAWorkaround()
        """
        if MyDistro.checkDependencies():
            return 1
        os.chmod(sys.argv[0], 0o755)
        SwitchCwd()
        for a in RulesFiles:
            if os.path.isfile(a):
                if os.path.isfile(GetLastPathElement(a)):
                    os.remove(GetLastPathElement(a))
                shutil.move(a, ".")
                Warn("Moved " + a + " -> " + LibDir + "/" + GetLastPathElement(a))
        MyDistro.registerAgentService()
        if os.path.isfile("/etc/waagent.conf"):
            try:
                os.remove("/etc/waagent.conf.old")
            except:
                pass
            try:
                os.rename("/etc/waagent.conf", "/etc/waagent.conf.old")
                Warn("Existing /etc/waagent.conf has been renamed to /etc/waagent.conf.old")
            except:
                pass
        SetFileContents("/etc/waagent.conf", self.waagent_conf_file)
        if os.path.exists('/usr/local/etc/logrotate.d/'):
            SetFileContents("/usr/local/etc/logrotate.d/waagent", WaagentLogrotate)
        filepath = "/etc/ssh/sshd_config"
        ReplaceFileContentsAtomic(filepath, "\n".join(filter(lambda a: not
        a.startswith("ClientAliveInterval"),
                                                             GetFileContents(filepath).split(
                                                                 '\n'))) + "\nClientAliveInterval 180\n")
        Log("Configured SSH client probing to keep connections alive.")
        # ApplyVNUMAWorkaround()
        return 0

    def mediaHasFilesystem(self, dsk):
        if Run('LC_ALL=C fdisk -p ' + dsk + ' | grep "invalid fdisk partition table found" ', False):
            return False
        return True

    def mountDVD(self, dvd, location):
        # At this point we cannot read a joliet option udf DVD in freebsd10 - so we 'dd' it into our location
        retcode, out = RunGetOutput(self.mount_dvd_cmd + dvd + ' of=' + location + '/ovf-env.xml')
        if retcode != 0:
            return retcode, out

        ovfxml = (GetFileContents(location + "/ovf-env.xml", asbin=False))
        if ord(ovfxml[0]) > 128 and ord(ovfxml[1]) > 128 and ord(ovfxml[2]) > 128:
            ovfxml = ovfxml[
                     3:]  # BOM is not stripped. First three bytes are > 128 and not unicode chars so we ignore them.
        ovfxml = ovfxml.strip(chr(0x00))
        ovfxml = "".join(filter(lambda x: ord(x) < 128, ovfxml))
        ovfxml = re.sub(r'</Environment>.*\Z', '', ovfxml, 0, re.DOTALL)
        ovfxml += '</Environment>'
        SetFileContents(location + "/ovf-env.xml", ovfxml)
        return retcode, out

    def GetHome(self):
        return '/home'

    def initScsiDiskTimeout(self):
        """
        Set the SCSI disk timeout by updating the kernal config
        """
        timeout = Config.get("OS.RootDeviceScsiTimeout")
        if timeout:
            Run("sysctl kern.cam.da.default_timeout=" + timeout)

    def setScsiDiskTimeout(self):
        return

    def setBlockDeviceTimeout(self, device, timeout):
        return

    def getProcessorCores(self):
        return int(RunGetOutput("sysctl hw.ncpu | awk '{print $2}'")[1])

    def getTotalMemory(self):
        return int(RunGetOutput("sysctl hw.realmem | awk '{print $2}'")[1]) / 1024

    def setDefaultGateway(self, gateway):
        Run("/sbin/route add default " + gateway, chk_err=False)

    def routeAdd(self, net, mask, gateway):
        Run("/sbin/route add -net " + net + " " + mask + " " + gateway, chk_err=False)


class NSBSDDistro(FreeBSDDistro):
    """
    Stormhield NS-BSD OS
    """

    def __init__(self):
        super(NSBSDDistro, self).__init__()


############################################################
# END DISTRO CLASS DEFS
############################################################

# This lets us index into a string or an array of integers transparently.
def Ord(a):
    """
    Allows indexing into a string or an array of integers transparently.
    Generic utility function.
    """
    if type(a) == type("a"):
        a = ord(a)
    return a


def IsLinux():
    """
    Returns True if platform is Linux.
    Generic utility function.
    """
    return (platform.uname()[0] == "Linux")


def GetLastPathElement(path):
    """
    Similar to basename.
    Generic utility function.
    """
    return path.rsplit('/', 1)[1]


def GetFileContents(filepath, asbin=False):
    """
    Read and return contents of 'filepath'.
    """
    mode = 'r'
    if asbin:
        mode += 'b'
    c = None
    try:
        with open(filepath, mode) as F:
            c = F.read()
    except IOError as e:
        ErrorWithPrefix('GetFileContents', 'Reading from file ' + filepath + ' Exception is ' + str(e))
        return None
    return c


def SetFileContents(filepath, contents):
    """
    Write 'contents' to 'filepath'.
    """
    if type(contents) == str:
        contents = contents.encode('latin-1', 'ignore')
    try:
        with open(filepath, "wb+") as F:
            F.write(contents)
    except IOError as e:
        ErrorWithPrefix('SetFileContents', 'Writing to file ' + filepath + ' Exception is ' + str(e))
        return None
    return 0


def AppendFileContents(filepath, contents):
    """
    Append 'contents' to 'filepath'.
    """
    if type(contents) == str:
        contents = contents.encode('latin-1')
    try:
        with open(filepath, "a+") as F:
            F.write(contents)
    except IOError as e:
        ErrorWithPrefix('AppendFileContents', 'Appending to file ' + filepath + ' Exception is ' + str(e))
        return None
    return 0


def ReplaceFileContentsAtomic(filepath, contents):
    """
    Write 'contents' to 'filepath' by creating a temp file, and replacing original.
    """
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(filepath))
    if type(contents) == str:
        contents = contents.encode('latin-1')
    try:
        os.write(handle, contents)
    except IOError as e:
        ErrorWithPrefix('ReplaceFileContentsAtomic', 'Writing to file ' + filepath + ' Exception is ' + str(e))
        return None
    finally:
        os.close(handle)
    try:
        os.rename(temp, filepath)
        return None
    except IOError as e:
        ErrorWithPrefix('ReplaceFileContentsAtomic', 'Renaming ' + temp + ' to ' + filepath + ' Exception is ' + str(e))
    try:
        os.remove(filepath)
    except IOError as e:
        ErrorWithPrefix('ReplaceFileContentsAtomic', 'Removing ' + filepath + ' Exception is ' + str(e))
    try:
        os.rename(temp, filepath)
    except IOError as e:
        ErrorWithPrefix('ReplaceFileContentsAtomic', 'Removing ' + filepath + ' Exception is ' + str(e))
        return 1
    return 0


def GetLineStartingWith(prefix, filepath):
    """
    Return line from 'filepath' if the line startswith 'prefix'
    """
    for line in GetFileContents(filepath).split('\n'):
        if line.startswith(prefix):
            return line
    return None


def Run(cmd, chk_err=True):
    """
    Calls RunGetOutput on 'cmd', returning only the return code.
    If chk_err=True then errors will be reported in the log.
    If chk_err=False then errors will be suppressed from the log.
    """
    retcode, out = RunGetOutput(cmd, chk_err)
    return retcode


def RunGetOutput(cmd, chk_err=True, log_cmd=True):
    """
    Wrapper for subprocess.check_output.
    Execute 'cmd'.  Returns return code and STDOUT, trapping expected exceptions.
    Reports exceptions to Error if chk_err parameter is True
    """
    if log_cmd:
        LogIfVerbose(cmd)
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
    except subprocess.CalledProcessError as e:
        if chk_err and log_cmd:
            Error('CalledProcessError.  Error Code is ' + str(e.returncode))
            Error('CalledProcessError.  Command string was ' + e.cmd)
            Error('CalledProcessError.  Command result was ' + (e.output[:-1]).decode('latin-1'))
        return e.returncode, e.output.decode('latin-1')
    return 0, output.decode('latin-1')


def RunSendStdin(cmd, input, chk_err=True, log_cmd=True):
    """
    Wrapper for subprocess.Popen.
    Execute 'cmd', sending 'input' to STDIN of 'cmd'.
    Returns return code and STDOUT, trapping expected exceptions.
    Reports exceptions to Error if chk_err parameter is True
    """
    if log_cmd:
        LogIfVerbose(cmd + input)
    try:
        me = subprocess.Popen([cmd], shell=True, stdin=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdout=subprocess.PIPE)
        output = me.communicate(input)
    except OSError as e:
        if chk_err and log_cmd:
            Error('CalledProcessError.  Error Code is ' + str(me.returncode))
            Error('CalledProcessError.  Command string was ' + cmd)
            Error('CalledProcessError.  Command result was ' + output[0].decode('latin-1'))
            return 1, output[0].decode('latin-1')
    if me.returncode is not 0 and chk_err is True and log_cmd:
        Error('CalledProcessError.  Error Code is ' + str(me.returncode))
        Error('CalledProcessError.  Command string was ' + cmd)
        Error('CalledProcessError.  Command result was ' + output[0].decode('latin-1'))
    return me.returncode, output[0].decode('latin-1')


def GetNodeTextData(a):
    """
    Filter non-text nodes from DOM tree
    """
    for b in a.childNodes:
        if b.nodeType == b.TEXT_NODE:
            return b.data


def GetHome():
    """
    Attempt to guess the $HOME location.
    Return the path string.
    """
    home = None
    try:
        home = GetLineStartingWith("HOME", "/etc/default/useradd").split('=')[1].strip()
    except:
        pass
    if (home == None) or (home.startswith("/") == False):
        home = "/home"
    return home


def ChangeOwner(filepath, user):
    """
    Lookup user.  Attempt chown 'filepath' to 'user'.
    """
    p = None
    try:
        p = pwd.getpwnam(user)
    except:
        pass
    if p != None:
        if not os.path.exists(filepath):
            Error("Path does not exist: {0}".format(filepath))
        else:
            os.chown(filepath, p[2], p[3])


def CreateDir(dirpath, user, mode):
    """
    Attempt os.makedirs, catch all exceptions.
    Call ChangeOwner afterwards.
    """
    try:
        os.makedirs(dirpath, mode)
    except:
        pass
    ChangeOwner(dirpath, user)


def CreateAccount(user, password, expiration, thumbprint):
    """
    Create a user account, with 'user', 'password', 'expiration', ssh keys
    and sudo permissions.
    Returns None if successful, error string on failure.
    """
    userentry = None
    try:
        userentry = pwd.getpwnam(user)
    except:
        pass
    uidmin = None
    try:
        uidmin = int(GetLineStartingWith("UID_MIN", "/etc/login.defs").split()[1])
    except:
        pass
    if uidmin == None:
        uidmin = 100
    if userentry != None and userentry[2] < uidmin:
        Error("CreateAccount: " + user + " is a system user. Will not set password.")
        return "Failed to set password for system user: " + user + " (0x06)."
    if userentry == None:
        command = "useradd -m " + user
        if expiration != None:
            command += " -e " + expiration.split('.')[0]
        if Run(command):
            Error("Failed to create user account: " + user)
            return "Failed to create user account: " + user + " (0x07)."
    else:
        Log("CreateAccount: " + user + " already exists. Will update password.")
    if password != None:
        MyDistro.changePass(user, password)
    try:
        # for older distros create sudoers.d
        if not os.path.isdir('/etc/sudoers.d/'):
            # create the /etc/sudoers.d/ directory
            os.mkdir('/etc/sudoers.d/')
            # add the include of sudoers.d to the /etc/sudoers
            SetFileContents('/etc/sudoers', GetFileContents('/etc/sudoers') + '\n#includedir /etc/sudoers.d\n')
        if password == None:
            SetFileContents("/etc/sudoers.d/waagent", user + " ALL = (ALL) NOPASSWD: ALL\n")
        else:
            SetFileContents("/etc/sudoers.d/waagent", user + " ALL = (ALL) ALL\n")
        os.chmod("/etc/sudoers.d/waagent", 0o440)
    except:
        Error("CreateAccount: Failed to configure sudo access for user.")
        return "Failed to configure sudo privileges (0x08)."
    home = MyDistro.GetHome()
    if thumbprint != None:
        dir = home + "/" + user + "/.ssh"
        CreateDir(dir, user, 0o700)
        pub = dir + "/id_rsa.pub"
        prv = dir + "/id_rsa"
        Run("ssh-keygen -y -f " + thumbprint + ".prv > " + pub)
        SetFileContents(prv, GetFileContents(thumbprint + ".prv"))
        for f in [pub, prv]:
            os.chmod(f, 0o600)
            ChangeOwner(f, user)
        SetFileContents(dir + "/authorized_keys", GetFileContents(pub))
        ChangeOwner(dir + "/authorized_keys", user)
    Log("Created user account: " + user)
    return None


def DeleteAccount(user):
    """
    Delete the 'user'.
    Clear utmp first, to avoid error.
    Removes the /etc/sudoers.d/waagent file.
    """
    userentry = None
    try:
        userentry = pwd.getpwnam(user)
    except:
        pass
    if userentry == None:
        Error("DeleteAccount: " + user + " not found.")
        return
    uidmin = None
    try:
        uidmin = int(GetLineStartingWith("UID_MIN", "/etc/login.defs").split()[1])
    except:
        pass
    if uidmin == None:
        uidmin = 100
    if userentry[2] < uidmin:
        Error("DeleteAccount: " + user + " is a system user. Will not delete account.")
        return
    Run("> /var/run/utmp")  # Delete utmp to prevent error if we are the 'user' deleted
    Run("userdel -f -r " + user)
    try:
        os.remove("/etc/sudoers.d/waagent")
    except:
        pass
    return


def IsInRangeInclusive(a, low, high):
    """
    Return True if 'a' in 'low' <= a >= 'high'
    """
    return (a >= low and a <= high)


def IsPrintable(ch):
    """
    Return True if character is displayable.
    """
    return IsInRangeInclusive(ch, Ord('A'), Ord('Z')) or IsInRangeInclusive(ch, Ord('a'),
                                                                            Ord('z')) or IsInRangeInclusive(ch,
                                                                                                            Ord('0'),
                                                                                                            Ord('9'))


def HexDump(buffer, size):
    """
    Return Hex formated dump of a 'buffer' of 'size'.
    """
    if size < 0:
        size = len(buffer)
    result = ""
    for i in range(0, size):
        if (i % 16) == 0:
            result += "%06X: " % i
        byte = buffer[i]
        if type(byte) == str:
            byte = ord(byte.decode('latin1'))
        result += "%02X " % byte
        if (i & 15) == 7:
            result += " "
        if ((i + 1) % 16) == 0 or (i + 1) == size:
            j = i
            while ((j + 1) % 16) != 0:
                result += "   "
                if (j & 7) == 7:
                    result += " "
                j += 1
            result += " "
            for j in range(i - (i % 16), i + 1):
                byte = buffer[j]
                if type(byte) == str:
                    byte = ord(byte.decode('latin1'))
                k = '.'
                if IsPrintable(byte):
                    k = chr(byte)
                result += k
            if (i + 1) != size:
                result += "\n"
    return result


def SimpleLog(file_path, message):
    if not file_path or len(message) < 1:
        return
    t = time.localtime()
    t = "%04u/%02u/%02u %02u:%02u:%02u " % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    lines = re.sub(re.compile(r'^(.)', re.MULTILINE), t + r'\1', message)
    with open(file_path, "a") as F:
        lines = filter(lambda x: x in string.printable, lines)
        F.write(lines.encode('ascii', 'ignore') + "\n")


class Logger(object):
    """
    The Agent's logging assumptions are:
    For Log, and LogWithPrefix all messages are logged to the
    self.file_path and to the self.con_path.  Setting either path
    parameter to None skips that log.  If Verbose is enabled, messages
    calling the LogIfVerbose method will be logged to file_path yet
    not to con_path.  Error and Warn messages are normal log messages
    with the 'ERROR:' or 'WARNING:' prefix added.
    """

    def __init__(self, filepath, conpath, verbose=False):
        """
        Construct an instance of Logger.
        """
        self.file_path = filepath
        self.con_path = conpath
        self.verbose = verbose

    def ThrottleLog(self, counter):
        """
        Log everything up to 10, every 10 up to 100, then every 100.
        """
        return (counter < 10) or ((counter < 100) and ((counter % 10) == 0)) or ((counter % 100) == 0)

    def LogToFile(self, message):
        """
        Write 'message' to logfile.
        """
        if self.file_path:
            try:
                with open(self.file_path, "a") as F:
                    message = filter(lambda x: x in string.printable, message)
                    F.write(message.encode('ascii', 'ignore') + "\n")
            except IOError as e:
                ##print e
                pass

    def LogToCon(self, message):
        """
        Write 'message' to /dev/console.
        This supports serial port logging if the /dev/console
        is redirected to ttys0 in kernel boot options.
        """
        if self.con_path:
            try:
                with open(self.con_path, "w") as C:
                    message = filter(lambda x: x in string.printable, message)
                    C.write(message.encode('ascii', 'ignore') + "\n")
            except IOError as e:
                pass

    def Log(self, message):
        """
        Standard Log function.
        Logs to self.file_path, and con_path
        """
        self.LogWithPrefix("", message)

    def LogWithPrefix(self, prefix, message):
        """
        Prefix each line of 'message' with current time+'prefix'.
        """
        t = time.localtime()
        t = "%04u/%02u/%02u %02u:%02u:%02u " % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
        t += prefix
        for line in message.split('\n'):
            line = t + line
            self.LogToFile(line)
            self.LogToCon(line)

    def NoLog(self, message):
        """
        Don't Log.
        """
        pass

    def LogIfVerbose(self, message):
        """
        Only log 'message' if global Verbose is True.
        """
        self.LogWithPrefixIfVerbose('', message)

    def LogWithPrefixIfVerbose(self, prefix, message):
        """
        Only log 'message' if global Verbose is True.
        Prefix each line of 'message' with current time+'prefix'.
        """
        if self.verbose == True:
            t = time.localtime()
            t = "%04u/%02u/%02u %02u:%02u:%02u " % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
            t += prefix
            for line in message.split('\n'):
                line = t + line
                self.LogToFile(line)
                self.LogToCon(line)

    def Warn(self, message):
        """
        Prepend the text "WARNING:" to the prefix for each line in 'message'.
        """
        self.LogWithPrefix("WARNING:", message)

    def Error(self, message):
        """
        Call ErrorWithPrefix(message).
        """
        ErrorWithPrefix("", message)

    def ErrorWithPrefix(self, prefix, message):
        """
        Prepend the text "ERROR:" to the prefix for each line in 'message'.
        Errors written to logfile, and /dev/console
        """
        self.LogWithPrefix("ERROR:", message)


def LoggerInit(log_file_path, log_con_path, verbose=False):
    """
    Create log object and export its methods to global scope.
    """
    global Log, LogWithPrefix, LogIfVerbose, LogWithPrefixIfVerbose, Error, ErrorWithPrefix, Warn, NoLog, ThrottleLog, myLogger
    l = Logger(log_file_path, log_con_path, verbose)
    Log, LogWithPrefix, LogIfVerbose, LogWithPrefixIfVerbose, Error, ErrorWithPrefix, Warn, NoLog, ThrottleLog, myLogger = l.Log, l.LogWithPrefix, l.LogIfVerbose, l.LogWithPrefixIfVerbose, l.Error, l.ErrorWithPrefix, l.Warn, l.NoLog, l.ThrottleLog, l

class HttpResourceGoneError(Exception):
    pass


class Util(object):
    """
    Http communication class.
    Base of GoalState, and Agent classes.
    """
    RetryWaitingInterval = 10

    def __init__(self):
        self.Endpoint = None

    def _ParseUrl(self, url):
        secure = False
        host = self.Endpoint
        path = url
        port = None

        # "http[s]://hostname[:port][/]"
        if url.startswith("http://"):
            url = url[7:]
            if "/" in url:
                host = url[0: url.index("/")]
                path = url[url.index("/"):]
            else:
                host = url
                path = "/"
        elif url.startswith("https://"):
            secure = True
            url = url[8:]
            if "/" in url:
                host = url[0: url.index("/")]
                path = url[url.index("/"):]
            else:
                host = url
                path = "/"

        if host is None:
            raise ValueError("Host is invalid:{0}".format(url))

        if (":" in host):
            pos = host.rfind(":")
            port = int(host[pos + 1:])
            host = host[0:pos]

        return host, port, secure, path

    def GetHttpProxy(self, secure):
        """
        Get http_proxy and https_proxy from environment variables.
        Username and password is not supported now.
        """
        host = Config.get("HttpProxy.Host")
        port = Config.get("HttpProxy.Port")
        return (host, port)

    def _HttpRequest(self, method, host, path, port=None, data=None, secure=False,
                     headers=None, proxyHost=None, proxyPort=None):
        resp = None
        conn = None
        try:
            if secure:
                port = 443 if port is None else port
                if proxyHost is not None and proxyPort is not None:
                    conn = httplibs.HTTPSConnection(proxyHost, proxyPort, timeout=10)
                    conn.set_tunnel(host, port)
                    # If proxy is used, full url is needed.
                    path = "https://{0}:{1}{2}".format(host, port, path)
                else:
                    conn = httplibs.HTTPSConnection(host, port, timeout=10)
            else:
                port = 80 if port is None else port
                if proxyHost is not None and proxyPort is not None:
                    conn = httplibs.HTTPConnection(proxyHost, proxyPort, timeout=10)
                    # If proxy is used, full url is needed.
                    path = "http://{0}:{1}{2}".format(host, port, path)
                else:
                    conn = httplibs.HTTPConnection(host, port, timeout=10)
            if headers == None:
                conn.request(method, path, data)
            else:
                conn.request(method, path, data, headers)
            resp = conn.getresponse()
        except httplibs.HTTPException as e:
            Error('HTTPException {0}, args:{1}'.format(e, repr(e.args)))
        except IOError as e:
            Error('Socket IOError {0}, args:{1}'.format(e, repr(e.args)))
        return resp

    def HttpRequest(self, method, url, data=None,
                    headers=None, maxRetry=3, chkProxy=False):
        """
        Sending http request to server
        On error, sleep 10 and maxRetry times.
        Return the output buffer or None.
        """
        LogIfVerbose("HTTP Req: {0} {1}".format(method, url))
        LogIfVerbose("HTTP Req: Data={0}".format(data))
        LogIfVerbose("HTTP Req: Header={0}".format(headers))
        try:
            host, port, secure, path = self._ParseUrl(url)
        except ValueError as e:
            Error("Failed to parse url:{0}".format(url))
            return None

        # Check proxy
        proxyHost, proxyPort = (None, None)
        if chkProxy:
            proxyHost, proxyPort = self.GetHttpProxy(secure)

        # If httplib module is not built with ssl support. Fallback to http
        if secure and not hasattr(httplibs, "HTTPSConnection"):
            Warn("httplib is not built with ssl support")
            secure = False
            proxyHost, proxyPort = self.GetHttpProxy(secure)

        # If httplib module doesn't support https tunnelling. Fallback to http
        if secure and \
                        proxyHost is not None and \
                        proxyPort is not None and \
                not hasattr(httplibs.HTTPSConnection, "set_tunnel"):
            Warn("httplib doesn't support https tunnelling(new in python 2.7)")
            secure = False
            proxyHost, proxyPort = self.GetHttpProxy(secure)

        resp = self._HttpRequest(method, host, path, port=port, data=data,
                                 secure=secure, headers=headers,
                                 proxyHost=proxyHost, proxyPort=proxyPort)
        for retry in range(0, maxRetry):
            if resp is not None and \
                    (resp.status == httplibs.OK or \
                                 resp.status == httplibs.CREATED or \
                                 resp.status == httplibs.ACCEPTED):
                return resp;

            if resp is not None and resp.status == httplibs.GONE:
                raise HttpResourceGoneError("Http resource gone.")

            Error("Retry={0}".format(retry))
            Error("HTTP Req: {0} {1}".format(method, url))
            Error("HTTP Req: Data={0}".format(data))
            Error("HTTP Req: Header={0}".format(headers))
            if resp is None:
                Error("HTTP Err: response is empty.".format(retry))
            else:
                Error("HTTP Err: Status={0}".format(resp.status))
                Error("HTTP Err: Reason={0}".format(resp.reason))
                Error("HTTP Err: Header={0}".format(resp.getheaders()))
                Error("HTTP Err: Body={0}".format(resp.read()))

            time.sleep(self.__class__.RetryWaitingInterval)
            resp = self._HttpRequest(method, host, path, port=port, data=data,
                                     secure=secure, headers=headers,
                                     proxyHost=proxyHost, proxyPort=proxyPort)

        return None

    def HttpGet(self, url, headers=None, maxRetry=3, chkProxy=False):
        return self.HttpRequest("GET", url, headers=headers,
                                maxRetry=maxRetry, chkProxy=chkProxy)

    def HttpHead(self, url, headers=None, maxRetry=3, chkProxy=False):
        return self.HttpRequest("HEAD", url, headers=headers,
                                maxRetry=maxRetry, chkProxy=chkProxy)

    def HttpPost(self, url, data, headers=None, maxRetry=3, chkProxy=False):
        return self.HttpRequest("POST", url, data=data, headers=headers,
                                maxRetry=maxRetry, chkProxy=chkProxy)

    def HttpPut(self, url, data, headers=None, maxRetry=3, chkProxy=False):
        return self.HttpRequest("PUT", url, data=data, headers=headers,
                                maxRetry=maxRetry, chkProxy=chkProxy)

    def HttpDelete(self, url, headers=None, maxRetry=3, chkProxy=False):
        return self.HttpRequest("DELETE", url, headers=headers,
                                maxRetry=maxRetry, chkProxy=chkProxy)

    def HttpGetWithoutHeaders(self, url, maxRetry=3, chkProxy=False):
        """
        Return data from an HTTP get on 'url'.
        """
        resp = self.HttpGet(url, headers=None, maxRetry=maxRetry,
                            chkProxy=chkProxy)
        return resp.read() if resp is not None else None

    def HttpGetWithHeaders(self, url, maxRetry=3, chkProxy=False):
        """
        Return data from an HTTP get on 'url' with
        x-ms-agent-name and x-ms-version
        headers.
        """
        resp = self.HttpGet(url, headers={
            "x-ms-agent-name": GuestAgentName,
            "x-ms-version": ProtocolVersion
        }, maxRetry=maxRetry, chkProxy=chkProxy)
        return resp.read() if resp is not None else None

    def HttpSecureGetWithHeaders(self, url, transportCert, maxRetry=3,
                                 chkProxy=False):
        """
        Return output of get using ssl cert.
        """
        resp = self.HttpGet(url, headers={
            "x-ms-agent-name": GuestAgentName,
            "x-ms-version": ProtocolVersion,
            "x-ms-cipher-name": "DES_EDE3_CBC",
            "x-ms-guest-agent-public-x509-cert": transportCert
        }, maxRetry=maxRetry, chkProxy=chkProxy)
        return resp.read() if resp is not None else None

    def HttpPostWithHeaders(self, url, data, maxRetry=3, chkProxy=False):
        headers = {
            "x-ms-agent-name": GuestAgentName,
            "Content-Type": "text/xml; charset=utf-8",
            "x-ms-version": ProtocolVersion
        }
        try:
            return self.HttpPost(url, data=data, headers=headers,
                                 maxRetry=maxRetry, chkProxy=chkProxy)
        except HttpResourceGoneError as e:
            Error("Failed to post: {0} {1}".format(url, e))
            return None


__StorageVersion = "2014-02-14"


def GetBlobType(url):
    restutil = Util()
    # Check blob type
    LogIfVerbose("Check blob type.")
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    blobPropResp = restutil.HttpHead(url, {
        "x-ms-date": timestamp,
        'x-ms-version': __StorageVersion
    }, chkProxy=True);
    blobType = None
    if blobPropResp is None:
        Error("Can't get status blob type.")
        return None
    blobType = blobPropResp.getheader("x-ms-blob-type")
    LogIfVerbose("Blob type={0}".format(blobType))
    return blobType


def PutBlockBlob(url, data):
    restutil = Util()
    LogIfVerbose("Upload block blob")
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ret = restutil.HttpPut(url, data, {
        "x-ms-date": timestamp,
        "x-ms-blob-type": "BlockBlob",
        "Content-Length": str(len(data)),
        "x-ms-version": __StorageVersion
    }, chkProxy=True)
    if ret is None:
        Error("Failed to upload block blob for status.")
        return -1
    return 0


def PutPageBlob(url, data):
    restutil = Util()
    LogIfVerbose("Replace old page blob")
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Align to 512 bytes
    pageBlobSize = ((len(data) + 511) / 512) * 512
    ret = restutil.HttpPut(url, "", {
        "x-ms-date": timestamp,
        "x-ms-blob-type": "PageBlob",
        "Content-Length": "0",
        "x-ms-blob-content-length": str(pageBlobSize),
        "x-ms-version": __StorageVersion
    }, chkProxy=True)
    if ret is None:
        Error("Failed to clean up page blob for status")
        return -1

    if url.index('?') < 0:
        url = "{0}?comp=page".format(url)
    else:
        url = "{0}&comp=page".format(url)

    LogIfVerbose("Upload page blob")
    pageMax = 4 * 1024 * 1024  # Max page size: 4MB
    start = 0
    end = 0
    while end < len(data):
        end = min(len(data), start + pageMax)
        contentSize = end - start
        # Align to 512 bytes
        pageEnd = ((end + 511) / 512) * 512
        bufSize = pageEnd - start
        buf = bytearray(bufSize)
        buf[0: contentSize] = data[start: end]
        if sys.version_info > (3,):
            buffer = memoryview
        ret = restutil.HttpPut(url, buffer(buf), {
            "x-ms-date": timestamp,
            "x-ms-range": "bytes={0}-{1}".format(start, pageEnd - 1),
            "x-ms-page-write": "update",
            "x-ms-version": __StorageVersion,
            "Content-Length": str(pageEnd - start)
        }, chkProxy=True)
        if ret is None:
            Error("Failed to upload page blob for status")
            return -1
        start = end
    return 0


def UploadStatusBlob(url, data):
    LogIfVerbose("Upload status blob")
    LogIfVerbose("Status={0}".format(data))
    blobType = GetBlobType(url)

    if blobType == "BlockBlob":
        return PutBlockBlob(url, data)
    elif blobType == "PageBlob":
        return PutPageBlob(url, data)
    else:
        Error("Unknown blob type: {0}".format(blobType))
        return -1


class TCPHandler(SocketServers.BaseRequestHandler):
    """
    Callback object for LoadBalancerProbeServer.
    Recv and send LB probe messages.
    """

    def __init__(self, lb_probe):
        super(TCPHandler, self).__init__()
        self.lb_probe = lb_probe

    def GetHttpDateTimeNow(self):
        """
        Return formatted gmtime "Date: Fri, 25 Mar 2011 04:53:10 GMT"
        """
        return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

    def handle(self):
        """
        Log LB probe messages, read the socket buffer,
        send LB probe response back to server.
        """
        self.lb_probe.ProbeCounter = (self.lb_probe.ProbeCounter + 1) % 1000000
        log = [NoLog, LogIfVerbose][ThrottleLog(self.lb_probe.ProbeCounter)]
        strCounter = str(self.lb_probe.ProbeCounter)
        if self.lb_probe.ProbeCounter == 1:
            Log("Receiving LB probes.")
        log("Received LB probe # " + strCounter)
        self.request.recv(1024)
        self.request.send(
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/html\r\nDate: " + self.GetHttpDateTimeNow() + "\r\n\r\nOK")


class LoadBalancerProbeServer(object):
    """
    Threaded object to receive and send LB probe messages.
    Load Balancer messages but be recv'd by
    the load balancing server, or this node may be shut-down.
    """

    def __init__(self, port):
        self.ProbeCounter = 0
        self.server = SocketServers.TCPServer((self.get_ip(), port), TCPHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.setDaemon(True)
        self.server_thread.start()

    def shutdown(self):
        self.server.shutdown()

    def get_ip(self):
        for retry in range(1, 6):
            ip = MyDistro.GetIpv4Address()
            if ip == None:
                Log("LoadBalancerProbeServer: GetIpv4Address() returned None, sleeping 10 before retry " + str(
                    retry + 1))
                time.sleep(10)
            else:
                return ip


class ConfigurationProvider(object):
    """
    Parse amd store key:values in waagent.conf
    """

    def __init__(self, walaConfigFile):
        self.values = dict()
        if 'MyDistro' not in globals():
            global MyDistro
            MyDistro = GetMyDistro()
        if walaConfigFile is None:
            walaConfigFile = MyDistro.getConfigurationPath()
        if os.path.isfile(walaConfigFile) == False:
            raise Exception("Missing configuration in {0}".format(walaConfigFile))
        try:
            for line in GetFileContents(walaConfigFile).split('\n'):
                if not line.startswith("#") and "=" in line:
                    parts = line.split()[0].split('=')
                    value = parts[1].strip("\" ")
                    if value != "None":
                        self.values[parts[0]] = value
                    else:
                        self.values[parts[0]] = None
        except:
            Error("Unable to parse {0}".format(walaConfigFile))
            raise
        return

    def get(self, key):
        return self.values.get(key)


class EnvMonitor(object):
    """
    Montor changes to dhcp and hostname.
    If dhcp clinet process re-start has occurred, reset routes, dhcp with fabric.
    """

    def __init__(self):
        self.shutdown = False
        self.HostName = socket.gethostname()
        self.server_thread = threading.Thread(target=self.monitor)
        self.server_thread.setDaemon(True)
        self.server_thread.start()
        self.published = False

    def monitor(self):
        """
        Monitor dhcp client pid and hostname.
        If dhcp clinet process re-start has occurred, reset routes, dhcp with fabric.
        """
        publish = Config.get("Provisioning.MonitorHostName")
        dhcpcmd = MyDistro.getpidcmd + ' ' + MyDistro.getDhcpClientName()
        dhcppid = RunGetOutput(dhcpcmd)[1]
        while not self.shutdown:
            for a in RulesFiles:
                if os.path.isfile(a):
                    if os.path.isfile(GetLastPathElement(a)):
                        os.remove(GetLastPathElement(a))
                    shutil.move(a, ".")
                    Log("EnvMonitor: Moved " + a + " -> " + LibDir)
            MyDistro.setScsiDiskTimeout()
            if publish != None and publish.lower().startswith("y"):
                try:
                    if socket.gethostname() != self.HostName:
                        Log("EnvMonitor: Detected host name change: " + self.HostName + " -> " + socket.gethostname())
                        self.HostName = socket.gethostname()
                        WaAgent.UpdateAndPublishHostName(self.HostName)
                        dhcppid = RunGetOutput(dhcpcmd)[1]
                        self.published = True
                except:
                    pass
            else:
                self.published = True
            pid = ""
            if not os.path.isdir("/proc/" + dhcppid.strip()):
                pid = RunGetOutput(dhcpcmd)[1]
            if pid != "" and pid != dhcppid:
                Log("EnvMonitor: Detected dhcp client restart. Restoring routing table.")
                WaAgent.RestoreRoutes()
                dhcppid = pid
            for child in Children:
                if child.poll() != None:
                    Children.remove(child)
            time.sleep(5)

    def SetHostName(self, name):
        """
        Generic call to MyDistro.setHostname(name).
        Complian to Log on error.
        """
        if socket.gethostname() == name:
            self.published = True
        elif MyDistro.setHostname(name):
            Error("Error: SetHostName: Cannot set hostname to " + name)
            return ("Error: SetHostName: Cannot set hostname to " + name)

    def IsHostnamePublished(self):
        """
        Return self.published  
        """
        return self.published

    def ShutdownService(self):
        """
        Stop server comminucation and join the thread to main thread.
        """
        self.shutdown = True
        self.server_thread.join()


class Certificates(object):
    """
    Object containing certificates of host and provisioned user.
    Parses and splits certificates into files.
    """

    #     <CertificateFile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="certificates10.xsd">
    #     <Version>2010-12-15</Version>
    #     <Incarnation>2</Incarnation>
    #     <Format>Pkcs7BlobWithPfxContents</Format>
    #     <Data>MIILTAY...
    #     </Data>
    #     </CertificateFile>

    def __init__(self):
        self.reinitialize()

    def reinitialize(self):
        """
        Reset the Role, Incarnation
        """
        self.Incarnation = None
        self.Role = None

    def Parse(self, xmlText):
        """
        Parse multiple certificates into seperate files.
        """
        self.reinitialize()
        SetFileContents("Certificates.xml", xmlText)
        dom = xml.dom.minidom.parseString(xmlText)
        for a in ["CertificateFile", "Version", "Incarnation",
                  "Format", "Data", ]:
            if not dom.getElementsByTagName(a):
                Error("Certificates.Parse: Missing " + a)
                return None
        node = dom.childNodes[0]
        if node.localName != "CertificateFile":
            Error("Certificates.Parse: root not CertificateFile")
            return None
        SetFileContents("Certificates.p7m",
                        "MIME-Version: 1.0\n"
                        + "Content-Disposition: attachment; filename=\"Certificates.p7m\"\n"
                        + "Content-Type: application/x-pkcs7-mime; name=\"Certificates.p7m\"\n"
                        + "Content-Transfer-Encoding: base64\n\n"
                        + GetNodeTextData(dom.getElementsByTagName("Data")[0]))
        if Run(
                                        Openssl + " cms -decrypt -in Certificates.p7m -inkey TransportPrivate.pem -recip TransportCert.pem | " + Openssl + " pkcs12 -nodes -password pass: -out Certificates.pem"):
            Error("Certificates.Parse: Failed to extract certificates from CMS message.")
            return self
        # There may be multiple certificates in this package. Split them.
        file = open("Certificates.pem")
        pindex = 1
        cindex = 1
        output = open("temp.pem", "w")
        for line in file.readlines():
            output.write(line)
            if re.match(r'[-]+END .*?(KEY|CERTIFICATE)[-]+$', line):
                output.close()
                if re.match(r'[-]+END .*?KEY[-]+$', line):
                    os.rename("temp.pem", str(pindex) + ".prv")
                    pindex += 1
                else:
                    os.rename("temp.pem", str(cindex) + ".crt")
                    cindex += 1
                output = open("temp.pem", "w")
        output.close()
        os.remove("temp.pem")
        keys = dict()
        index = 1
        filename = str(index) + ".crt"
        while os.path.isfile(filename):
            thumbprint = \
            (RunGetOutput(Openssl + " x509 -in " + filename + " -fingerprint -noout")[1]).rstrip().split('=')[
                1].replace(':', '').upper()
            pubkey = RunGetOutput(Openssl + " x509 -in " + filename + " -pubkey -noout")[1]
            keys[pubkey] = thumbprint
            os.rename(filename, thumbprint + ".crt")
            os.chmod(thumbprint + ".crt", 0o600)
            MyDistro.setSelinuxContext(thumbprint + '.crt', 'unconfined_u:object_r:ssh_home_t:s0')
            index += 1
            filename = str(index) + ".crt"
        index = 1
        filename = str(index) + ".prv"
        while os.path.isfile(filename):
            pubkey = RunGetOutput(Openssl + " rsa -in " + filename + " -pubout 2> /dev/null ")[1]
            os.rename(filename, keys[pubkey] + ".prv")
            os.chmod(keys[pubkey] + ".prv", 0o600)
            MyDistro.setSelinuxContext(keys[pubkey] + '.prv', 'unconfined_u:object_r:ssh_home_t:s0')
            index += 1
            filename = str(index) + ".prv"
        return self


class ExtensionsConfig(object):
    """
    Parse ExtensionsConfig, downloading and unpacking them to /var/lib/waagent.
    Install if <enabled>true</enabled>, remove if it is set to false.
    """

    # <?xml version="1.0" encoding="utf-8"?>
    # <Extensions version="1.0.0.0" goalStateIncarnation="6"><Plugins>
    #  <Plugin name="OSTCExtensions.ExampleHandlerLinux" version="1.5"
    # location="http://previewusnorthcache.blob.core.test-cint.azure-test.net/d84b216d00bf4d96982be531539e1513/OSTCExtensions_ExampleHandlerLinux_usnorth_manifest.xml"
    # config="" state="enabled" autoUpgrade="false" runAsStartupTask="false" isJson="true" />
    # </Plugins>
    # <PluginSettings>
    #  <Plugin name="OSTCExtensions.ExampleHandlerLinux" version="1.5">
    #    <RuntimeSettings seqNo="2">{"runtimeSettings":[{"handlerSettings":{"protectedSettingsCertThumbprint":"1BE9A13AA1321C7C515EF109746998BAB6D86FD1",
    # "protectedSettings":"MIIByAYJKoZIhvcNAQcDoIIBuTCCAbUCAQAxggFxMIIBbQIBADBVMEExPzA9BgoJkiaJk/IsZAEZFi9XaW5kb3dzIEF6dXJlIFNlcnZpY2UgTWFuYWdlbWVudCBmb3IgR
    # Xh0ZW5zaW9ucwIQZi7dw+nhc6VHQTQpCiiV2zANBgkqhkiG9w0BAQEFAASCAQCKr09QKMGhwYe+O4/a8td+vpB4eTR+BQso84cV5KCAnD6iUIMcSYTrn9aveY6v6ykRLEw8GRKfri2d6
    # tvVDggUrBqDwIgzejGTlCstcMJItWa8Je8gHZVSDfoN80AEOTws9Fp+wNXAbSuMJNb8EnpkpvigAWU2v6pGLEFvSKC0MCjDTkjpjqciGMcbe/r85RG3Zo21HLl0xNOpjDs/qqikc/ri43Y76E/X
    # v1vBSHEGMFprPy/Hwo3PqZCnulcbVzNnaXN3qi/kxV897xGMPPC3IrO7Nc++AT9qRLFI0841JLcLTlnoVG1okPzK9w6ttksDQmKBSHt3mfYV+skqs+EOMDsGCSqGSIb3DQEHATAUBggqh
    # kiG9w0DBwQITgu0Nu3iFPuAGD6/QzKdtrnCI5425fIUy7LtpXJGmpWDUA==","publicSettings":{"port":"3000"}}}]}</RuntimeSettings>
    #  </Plugin>
    # </PluginSettings>


    def __init__(self):
        self.reinitialize()

    def reinitialize(self):
        """
        Reset members.
        """
        self.Extensions = None
        self.Plugins = None
        self.Util = None

    def Parse(self, xmlText):
        """
        Write configuration to file ExtensionsConfig.xml.
        Log plugin specific activity to /var/log/azure/<Publisher>.<PluginName>/<Version>/CommandExecution.log.
        If state is enabled:
            if the plugin is installed:
                if the new plugin's version is higher
                if DisallowMajorVersionUpgrade is false or if true, the version is a minor version do upgrade:
                    download the new archive
                    do the updateCommand.
                    disable the old plugin and remove
                    enable the new plugin
                if the new plugin's version is the same or lower:
                    create the new .settings file from the configuration received
                    do the enableCommand
            if the plugin is not installed:
                download/unpack archive and call the installCommand/Enable
        if state is disabled:
            call disableCommand
        if state is uninstall:
            call uninstallCommand
            remove old plugin directory.
        """
        self.reinitialize()
        self.Util = Util()
        dom = xml.dom.minidom.parseString(xmlText)
        LogIfVerbose(xmlText)
        self.plugin_log_dir = '/var/log/azure'
        if not os.path.exists(self.plugin_log_dir):
            os.mkdir(self.plugin_log_dir)
        try:
            self.Extensions = dom.getElementsByTagName("Extensions")
            pg = dom.getElementsByTagName("Plugins")
            if len(pg) > 0:
                self.Plugins = pg[0].getElementsByTagName("Plugin")
            else:
                self.Plugins = []
            incarnation = self.Extensions[0].getAttribute("goalStateIncarnation")
            SetFileContents('ExtensionsConfig.' + incarnation + '.xml', xmlText)
        except Exception as e:
            Error('ERROR:  Error parsing ExtensionsConfig: {0}.'.format(e))
            return None
        for p in self.Plugins:
            if len(p.getAttribute("location")) < 1:  # this plugin is inside the PluginSettings
                continue
            p.setAttribute('restricted', 'false')
            previous_version = None
            version = p.getAttribute("version")
            name = p.getAttribute("name")
            plog_dir = self.plugin_log_dir + '/' + name + '/' + version
            if not os.path.exists(plog_dir):
                os.makedirs(plog_dir)
            p.plugin_log = plog_dir + '/CommandExecution.log'
            handler = name + '-' + version
            if p.getAttribute("isJson") != 'true':
                Error("Plugin " + name + " version: " + version + " is not a JSON Extension.  Skipping.")
                continue
            Log("Found Plugin: " + name + ' version: ' + version)
            if p.getAttribute("state") == 'disabled' or p.getAttribute("state") == 'uninstall':
                # disable
                zip_dir = LibDir + "/" + name + '-' + version
                mfile = None
                for root, dirs, files in os.walk(zip_dir):
                    for f in files:
                        if f in ('HandlerManifest.json'):
                            mfile = os.path.join(root, f)
                    if mfile != None:
                        break
                if mfile == None:
                    Error('HandlerManifest.json not found.')
                    continue
                manifest = GetFileContents(mfile)
                p.setAttribute('manifestdata', manifest)
                if self.launchCommand(p.plugin_log, name, version, 'disableCommand') == None:
                    self.SetHandlerState(handler, 'Enabled')
                    Error('Unable to disable ' + name)
                    SimpleLog(p.plugin_log, 'ERROR: Unable to disable ' + name)
                else:
                    self.SetHandlerState(handler, 'Disabled')
                    Log(name + ' is disabled')
                    SimpleLog(p.plugin_log, name + ' is disabled')

                # uninstall if needed
                if p.getAttribute("state") == 'uninstall':
                    if self.launchCommand(p.plugin_log, name, version, 'uninstallCommand') == None:
                        self.SetHandlerState(handler, 'Installed')
                        Error('Unable to uninstall ' + name)
                        SimpleLog(p.plugin_log, 'Unable to uninstall ' + name)
                    else:
                        self.SetHandlerState(handler, 'NotInstalled')
                        Log(name + ' uninstallCommand completed .')
                    # remove the plugin
                    Run('rm -rf ' + LibDir + '/' + name + '-' + version + '*')
                    Log(name + '-' + version + ' extension files deleted.')
                    SimpleLog(p.plugin_log, name + '-' + version + ' extension files deleted.')

                continue
                # state is enabled
            # if the same plugin exists and the version is newer or
            # does not exist then download and unzip the new plugin
            plg_dir = None

            latest_version_installed = LooseVersion("0.0")
            for item in os.listdir(LibDir):
                itemPath = os.path.join(LibDir, item)
                if os.path.isdir(itemPath) and name in item:
                    try:
                        # Split plugin dir name with '-' to get intalled plugin name and version
                        sperator = item.rfind('-')
                        if sperator < 0:
                            continue
                        installed_plg_name = item[0:sperator]
                        installed_plg_version = LooseVersion(item[sperator + 1:])

                        # Check installed plugin name and compare installed version to get the latest version installed
                        if installed_plg_name == name and installed_plg_version > latest_version_installed:
                            plg_dir = itemPath
                            previous_version = str(installed_plg_version)
                            latest_version_installed = installed_plg_version
                    except Exception as e:
                        Warn("Invalid plugin dir name: {0} {1}".format(item, e))
                        continue

            if plg_dir == None or LooseVersion(version) > LooseVersion(previous_version):
                location = p.getAttribute("location")
                Log("Downloading plugin manifest: " + name + " from " + location)
                SimpleLog(p.plugin_log, "Downloading plugin manifest: " + name + " from " + location)

                self.Util.Endpoint = location.split('/')[2]
                Log("Plugin server is: " + self.Util.Endpoint)
                SimpleLog(p.plugin_log, "Plugin server is: " + self.Util.Endpoint)

                manifest = self.Util.HttpGetWithoutHeaders(location, chkProxy=True)
                if manifest == None:
                    Error(
                        "Unable to download plugin manifest" + name + " from primary location.  Attempting with failover location.")
                    SimpleLog(p.plugin_log,
                              "Unable to download plugin manifest" + name + " from primary location.  Attempting with failover location.")
                    failoverlocation = p.getAttribute("failoverlocation")
                    self.Util.Endpoint = failoverlocation.split('/')[2]
                    Log("Plugin failover server is: " + self.Util.Endpoint)
                    SimpleLog(p.plugin_log, "Plugin failover server is: " + self.Util.Endpoint)

                    manifest = self.Util.HttpGetWithoutHeaders(failoverlocation, chkProxy=True)
                # if failoverlocation also fail what to do then?
                if manifest == None:
                    AddExtensionEvent(name, WALAEventOperation.Download, False, 0, version,
                                      "Download mainfest fail " + failoverlocation)
                    Log("Plugin manifest " + name + " downloading failed from failover location.")
                    SimpleLog(p.plugin_log, "Plugin manifest " + name + " downloading failed from failover location.")

                filepath = LibDir + "/" + name + '.' + incarnation + '.manifest'
                if os.path.splitext(location)[-1] == '.xml':  # if this is an xml file we may have a BOM
                    if ord(manifest[0]) > 128 and ord(manifest[1]) > 128 and ord(manifest[2]) > 128:
                        manifest = manifest[3:]
                SetFileContents(filepath, manifest)
                # Get the bundle url from the manifest
                p.setAttribute('manifestdata', manifest)
                man_dom = xml.dom.minidom.parseString(manifest)
                bundle_uri = ""
                for mp in man_dom.getElementsByTagName("Plugin"):
                    if GetNodeTextData(mp.getElementsByTagName("Version")[0]) == version:
                        bundle_uri = GetNodeTextData(mp.getElementsByTagName("Uri")[0])
                        break
                if len(mp.getElementsByTagName("DisallowMajorVersionUpgrade")):
                    if GetNodeTextData(mp.getElementsByTagName("DisallowMajorVersionUpgrade")[
                                           0]) == 'true' and previous_version != None and previous_version.split('.')[
                        0] != version.split('.')[0]:
                        Log('DisallowMajorVersionUpgrade is true, this major version is restricted from upgrade.')
                        SimpleLog(p.plugin_log,
                                  'DisallowMajorVersionUpgrade is true, this major version is restricted from upgrade.')
                        p.setAttribute('restricted', 'true')
                        continue
                if len(bundle_uri) < 1:
                    Error("Unable to fetch Bundle URI from manifest for " + name + " v " + version)
                    SimpleLog(p.plugin_log, "Unable to fetch Bundle URI from manifest for " + name + " v " + version)
                    continue
                Log("Bundle URI = " + bundle_uri)
                SimpleLog(p.plugin_log, "Bundle URI = " + bundle_uri)

                # Download the zipfile archive and save as '.zip'
                bundle = self.Util.HttpGetWithoutHeaders(bundle_uri, chkProxy=True)
                if bundle == None:
                    AddExtensionEvent(name, WALAEventOperation.Download, True, 0, version,
                                      "Download zip fail " + bundle_uri)
                    Error("Unable to download plugin bundle" + bundle_uri)
                    SimpleLog(p.plugin_log, "Unable to download plugin bundle" + bundle_uri)
                    continue
                AddExtensionEvent(name, WALAEventOperation.Download, True, 0, version, "Download Success")
                b = bytearray(bundle)
                filepath = LibDir + "/" + os.path.basename(bundle_uri) + '.zip'
                SetFileContents(filepath, b)
                Log("Plugin bundle" + bundle_uri + "downloaded successfully length = " + str(len(bundle)))
                SimpleLog(p.plugin_log,
                          "Plugin bundle" + bundle_uri + "downloaded successfully length = " + str(len(bundle)))

                # unpack the archive
                z = zipfile.ZipFile(filepath)
                zip_dir = LibDir + "/" + name + '-' + version
                z.extractall(zip_dir)
                Log('Extracted ' + bundle_uri + ' to ' + zip_dir)
                SimpleLog(p.plugin_log, 'Extracted ' + bundle_uri + ' to ' + zip_dir)

                # zip no file perms in .zip so set all the scripts to +x
                Run("find " + zip_dir + " -type f | xargs chmod  u+x ")
                # write out the base64 config data so the plugin can process it.
                mfile = None
                for root, dirs, files in os.walk(zip_dir):
                    for f in files:
                        if f in ('HandlerManifest.json'):
                            mfile = os.path.join(root, f)
                    if mfile != None:
                        break
                if mfile == None:
                    Error('HandlerManifest.json not found.')
                    SimpleLog(p.plugin_log, 'HandlerManifest.json not found.')
                    continue
                manifest = GetFileContents(mfile)
                p.setAttribute('manifestdata', manifest)
                # create the status and config dirs
                Run('mkdir -p ' + root + '/status')
                Run('mkdir -p ' + root + '/config')
                # write out the configuration data to goalStateIncarnation.settings file in the config path.
                config = ''
                seqNo = '0'
                if len(dom.getElementsByTagName("PluginSettings")) != 0:
                    pslist = dom.getElementsByTagName("PluginSettings")[0].getElementsByTagName("Plugin")
                    for ps in pslist:
                        if name == ps.getAttribute("name") and version == ps.getAttribute("version"):
                            Log("Found RuntimeSettings for " + name + " V " + version)
                            SimpleLog(p.plugin_log, "Found RuntimeSettings for " + name + " V " + version)

                            config = GetNodeTextData(ps.getElementsByTagName("RuntimeSettings")[0])
                            seqNo = ps.getElementsByTagName("RuntimeSettings")[0].getAttribute("seqNo")
                            break
                if config == '':
                    Log("No RuntimeSettings for " + name + " V " + version)
                    SimpleLog(p.plugin_log, "No RuntimeSettings for " + name + " V " + version)

                SetFileContents(root + "/config/" + seqNo + ".settings", config)
                # create HandlerEnvironment.json
                handler_env = '[{  "name": "' + name + '", "seqNo": "' + seqNo + '", "version": 1.0,  "handlerEnvironment": {    "logFolder": "' + os.path.dirname(
                    p.plugin_log) + '",    "configFolder": "' + root + '/config",    "statusFolder": "' + root + '/status",    "heartbeatFile": "' + root + '/heartbeat.log"}}]'
                SetFileContents(root + '/HandlerEnvironment.json', handler_env)
                self.SetHandlerState(handler, 'NotInstalled')

                cmd = ''
                getcmd = 'installCommand'
                if plg_dir != None and previous_version != None and LooseVersion(version) > LooseVersion(
                        previous_version):
                    previous_handler = name + '-' + previous_version
                    if self.GetHandlerState(previous_handler) != 'NotInstalled':
                        getcmd = 'updateCommand'
                        # disable the old plugin if it exists
                        if self.launchCommand(p.plugin_log, name, previous_version, 'disableCommand') == None:
                            self.SetHandlerState(previous_handler, 'Enabled')
                            Error('Unable to disable old plugin ' + name + ' version ' + previous_version)
                            SimpleLog(p.plugin_log,
                                      'Unable to disable old plugin ' + name + ' version ' + previous_version)
                        else:
                            self.SetHandlerState(previous_handler, 'Disabled')
                            Log(name + ' version ' + previous_version + ' is disabled')
                            SimpleLog(p.plugin_log, name + ' version ' + previous_version + ' is disabled')

                        try:
                            Log("Copy status file from old plugin dir to new")
                            old_plg_dir = plg_dir
                            new_plg_dir = os.path.join(LibDir, "{0}-{1}".format(name, version))
                            old_ext_status_dir = os.path.join(old_plg_dir, "status")
                            new_ext_status_dir = os.path.join(new_plg_dir, "status")
                            if os.path.isdir(old_ext_status_dir):
                                for status_file in os.listdir(old_ext_status_dir):
                                    status_file_path = os.path.join(old_ext_status_dir, status_file)
                                    if os.path.isfile(status_file_path):
                                        shutil.copy2(status_file_path, new_ext_status_dir)
                            mrseq_file = os.path.join(old_plg_dir, "mrseq")
                            if os.path.isfile(mrseq_file):
                                shutil.copy(mrseq_file, new_plg_dir)
                        except Exception as e:
                            Error("Failed to copy status file.")

                isupgradeSuccess = True
                if getcmd == 'updateCommand':
                    if self.launchCommand(p.plugin_log, name, version, getcmd, previous_version) == None:
                        Error('Update failed for ' + name + '-' + version)
                        SimpleLog(p.plugin_log, 'Update failed for ' + name + '-' + version)
                        isupgradeSuccess = False
                    else:
                        Log('Update complete' + name + '-' + version)
                        SimpleLog(p.plugin_log, 'Update complete' + name + '-' + version)

                    # if we updated - call unistall for the old plugin
                    if self.launchCommand(p.plugin_log, name, previous_version, 'uninstallCommand') == None:
                        self.SetHandlerState(previous_handler, 'Installed')
                        Error('Uninstall failed for ' + name + '-' + previous_version)
                        SimpleLog(p.plugin_log, 'Uninstall failed for ' + name + '-' + previous_version)
                        isupgradeSuccess = False
                    else:
                        self.SetHandlerState(previous_handler, 'NotInstalled')
                        Log('Uninstall complete' + previous_handler)
                        SimpleLog(p.plugin_log, 'Uninstall complete' + name + '-' + previous_version)

                    try:
                        # rm old plugin dir
                        if os.path.isdir(plg_dir):
                            shutil.rmtree(plg_dir)
                            Log(name + '-' + previous_version + ' extension files deleted.')
                            SimpleLog(p.plugin_log, name + '-' + previous_version + ' extension files deleted.')
                    except Exception as e:
                        Error("Failed to remove old plugin directory")

                    AddExtensionEvent(name, WALAEventOperation.Upgrade, isupgradeSuccess, 0, previous_version)
                else:  # run install
                    if self.launchCommand(p.plugin_log, name, version, getcmd) == None:
                        self.SetHandlerState(handler, 'NotInstalled')
                        Error('Installation failed for ' + name + '-' + version)
                        SimpleLog(p.plugin_log, 'Installation failed for ' + name + '-' + version)
                    else:
                        self.SetHandlerState(handler, 'Installed')
                        Log('Installation completed for ' + name + '-' + version)
                        SimpleLog(p.plugin_log, 'Installation completed for ' + name + '-' + version)

            # end if plg_dir == none or version > = prev
            # change incarnation of settings file so it knows how to name status...
            zip_dir = LibDir + "/" + name + '-' + version
            mfile = None
            for root, dirs, files in os.walk(zip_dir):
                for f in files:
                    if f in ('HandlerManifest.json'):
                        mfile = os.path.join(root, f)
                if mfile != None:
                    break
            if mfile == None:
                Error('HandlerManifest.json not found.')
                SimpleLog(p.plugin_log, 'HandlerManifest.json not found.')

                continue
            manifest = GetFileContents(mfile)
            p.setAttribute('manifestdata', manifest)
            config = ''
            seqNo = '0'
            if len(dom.getElementsByTagName("PluginSettings")) != 0:
                try:
                    pslist = dom.getElementsByTagName("PluginSettings")[0].getElementsByTagName("Plugin")
                except:
                    Error('Error parsing ExtensionsConfig.')
                    SimpleLog(p.plugin_log, 'Error parsing ExtensionsConfig.')

                    continue
                for ps in pslist:
                    if name == ps.getAttribute("name") and version == ps.getAttribute("version"):
                        Log("Found RuntimeSettings for " + name + " V " + version)
                        SimpleLog(p.plugin_log, "Found RuntimeSettings for " + name + " V " + version)

                        config = GetNodeTextData(ps.getElementsByTagName("RuntimeSettings")[0])
                        seqNo = ps.getElementsByTagName("RuntimeSettings")[0].getAttribute("seqNo")
                        break
            if config == '':
                Error("No RuntimeSettings for " + name + " V " + version)
                SimpleLog(p.plugin_log, "No RuntimeSettings for " + name + " V " + version)

            SetFileContents(root + "/config/" + seqNo + ".settings", config)

            # state is still enable
            if (self.GetHandlerState(handler) == 'NotInstalled'):  # run install first if true
                if self.launchCommand(p.plugin_log, name, version, 'installCommand') == None:
                    self.SetHandlerState(handler, 'NotInstalled')
                    Error('Installation failed for ' + name + '-' + version)
                    SimpleLog(p.plugin_log, 'Installation failed for ' + name + '-' + version)

                else:
                    self.SetHandlerState(handler, 'Installed')
                    Log('Installation completed for ' + name + '-' + version)
                    SimpleLog(p.plugin_log, 'Installation completed for ' + name + '-' + version)

            if (self.GetHandlerState(handler) != 'NotInstalled'):
                if self.launchCommand(p.plugin_log, name, version, 'enableCommand') == None:
                    self.SetHandlerState(handler, 'Installed')
                    Error('Enable failed for ' + name + '-' + version)
                    SimpleLog(p.plugin_log, 'Enable failed for ' + name + '-' + version)

                else:
                    self.SetHandlerState(handler, 'Enabled')
                    Log('Enable completed for ' + name + '-' + version)
                    SimpleLog(p.plugin_log, 'Enable completed for ' + name + '-' + version)

            # this plugin processing is complete
            Log('Processing completed for ' + name + '-' + version)
            SimpleLog(p.plugin_log, 'Processing completed for ' + name + '-' + version)

        # end plugin processing loop
        Log('Finished processing ExtensionsConfig.xml')
        try:
            SimpleLog(p.plugin_log, 'Finished processing ExtensionsConfig.xml')
        except:
            pass

        return self

    def launchCommand(self, plugin_log, name, version, command, prev_version=None):
        commandToEventOperation = {
            "installCommand": WALAEventOperation.Install,
            "uninstallCommand": WALAEventOperation.UnIsntall,
            "updateCommand": WALAEventOperation.Upgrade,
            "enableCommand": WALAEventOperation.Enable,
            "disableCommand": WALAEventOperation.Disable,
        }
        isSuccess = True
        start = datetime.datetime.now()
        r = self.__launchCommandWithoutEventLog(plugin_log, name, version, command, prev_version)
        if r == None:
            isSuccess = False
        Duration = int((datetime.datetime.now() - start).seconds)
        if commandToEventOperation.get(command):
            AddExtensionEvent(name, commandToEventOperation[command], isSuccess, Duration, version)
        return r

    def __launchCommandWithoutEventLog(self, plugin_log, name, version, command, prev_version=None):
        # get the manifest and read the command
        mfile = None
        zip_dir = LibDir + "/" + name + '-' + version
        for root, dirs, files in os.walk(zip_dir):
            for f in files:
                if f in ('HandlerManifest.json'):
                    mfile = os.path.join(root, f)
            if mfile != None:
                break
        if mfile == None:
            Error('HandlerManifest.json not found.')
            SimpleLog(plugin_log, 'HandlerManifest.json not found.')

            return None
        manifest = GetFileContents(mfile)
        try:
            jsn = json.loads(manifest)
        except:
            Error('Error parsing HandlerManifest.json.')
            SimpleLog(plugin_log, 'Error parsing HandlerManifest.json.')

            return None
        if type(jsn) == list:
            jsn = jsn[0]
        if jsn.has_key('handlerManifest'):
            cmd = jsn['handlerManifest'][command]
        else:
            Error('Key handlerManifest not found.  Handler cannot be installed.')
            SimpleLog(plugin_log, 'Key handlerManifest not found.  Handler cannot be installed.')

        if len(cmd) == 0:
            Error('Unable to read ' + command)
            SimpleLog(plugin_log, 'Unable to read ' + command)

            return None

        # for update we send the path of the old installation
        arg = ''
        if prev_version != None:
            arg = ' ' + LibDir + '/' + name + '-' + prev_version
        dirpath = os.path.dirname(mfile)
        LogIfVerbose('Command is ' + dirpath + '/' + cmd)
        # launch
        pid = None
        try:
            child = subprocess.Popen(dirpath + '/' + cmd + arg, shell=True, cwd=dirpath, stdout=subprocess.PIPE)
        except Exception as e:
            Error('Exception launching ' + cmd + str(e))
            SimpleLog(plugin_log, 'Exception launching ' + cmd + str(e))

        pid = child.pid
        if pid == None or pid < 1:
            ExtensionChildren.append((-1, root))
            Error('Error launching ' + cmd + '.')
            SimpleLog(plugin_log, 'Error launching ' + cmd + '.')

        else:
            ExtensionChildren.append((pid, root))
            Log("Spawned " + cmd + " PID " + str(pid))
            SimpleLog(plugin_log, "Spawned " + cmd + " PID " + str(pid))

        # wait until install/upgrade is finished
        timeout = 300  # 5 minutes
        retry = timeout / 5
        while retry > 0 and child.poll() == None:
            LogIfVerbose(cmd + ' still running with PID ' + str(pid))
            time.sleep(5)
            retry -= 1
        if retry == 0:
            Error('Process exceeded timeout of ' + str(timeout) + ' seconds. Terminating process ' + str(pid))
            SimpleLog(plugin_log,
                      'Process exceeded timeout of ' + str(timeout) + ' seconds. Terminating process ' + str(pid))

            os.kill(pid, 9)
            return None
        code = child.wait()
        if code == None or code != 0:
            Error('Process ' + str(pid) + ' returned non-zero exit code (' + str(code) + ')')
            SimpleLog(plugin_log, 'Process ' + str(pid) + ' returned non-zero exit code (' + str(code) + ')')

            return None
        Log(command + ' completed.')
        SimpleLog(plugin_log, command + ' completed.')

        return 0

    def ReportHandlerStatus(self):
        """
        Collect all status reports.
        """
        # { "version": "1.0", "timestampUTC": "2014-03-31T21:28:58Z",
        # "aggregateStatus": {
        # "guestAgentStatus": { "version": "2.0.4PRE", "status": "Ready", "formattedMessage": { "lang": "en-US", "message": "GuestAgent is running and accepting new configurations." } },
        # "handlerAggregateStatus": [{
        # "handlerName": "ExampleHandlerLinux", "handlerVersion": "1.0", "status": "Ready", "runtimeSettingsStatus": {
        # "sequenceNumber": "2", "settingsStatus": { "timestampUTC": "2014-03-31T23:46:00Z", "status": { "name": "ExampleHandlerLinux", "operation": "Command Execution Finished", "configurationAppliedTime": "2014-03-31T23:46:00Z", "status": "success", "formattedMessage": { "lang": "en-US", "message": "Finished executing command" },
        # "substatus": [
        # { "name": "StdOut", "status": "success", "formattedMessage": { "lang": "en-US", "message": "Goodbye world!" }  },
        # { "name": "StdErr", "status": "success", "formattedMessage": { "lang": "en-US", "message": "" } }
        # ]
        # } } } }
        # ]
        #  }}

        try:
            incarnation = self.Extensions[0].getAttribute("goalStateIncarnation")
        except:
            Error('Error parsing attribute "goalStateIncarnation".  Unable to send status reports')
            return -1
        status = ''
        statuses = ''
        for p in self.Plugins:
            if p.getAttribute("state") == 'uninstall' or p.getAttribute("restricted") == 'true':
                continue
            version = p.getAttribute("version")
            name = p.getAttribute("name")
            if p.getAttribute("isJson") != 'true':
                LogIfVerbose("Plugin " + name + " version: " + version + " is not a JSON Extension.  Skipping.")
                continue
            reportHeartbeat = False
            if len(p.getAttribute("manifestdata")) < 1:
                Error("Failed to get manifestdata.")
            else:
                reportHeartbeat = json.loads(p.getAttribute("manifestdata"))[0]['handlerManifest']['reportHeartbeat']
            if len(statuses) > 0:
                statuses += ','
            statuses += self.GenerateAggStatus(name, version, reportHeartbeat)
        tstamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # header
        # agent state
        if provisioned == False:
            if provisionError == None:
                agent_state = 'Provisioning'
                agent_msg = 'Guest Agent is starting.'
            else:
                agent_state = 'Provisioning Error.'
                agent_msg = provisionError
        else:
            agent_state = 'Ready'
            agent_msg = 'GuestAgent is running and accepting new configurations.'

        status = '{"version":"1.0","timestampUTC":"' + tstamp + '","aggregateStatus":{"guestAgentStatus":{"version":"' + GuestAgentVersion + '","status":"' + agent_state + '","formattedMessage":{"lang":"en-US","message":"' + agent_msg + '"}},"handlerAggregateStatus":[' + statuses + ']}}'
        try:
            uri = GetNodeTextData(self.Extensions[0].getElementsByTagName("StatusUploadBlob")[0]).replace('&amp;', '&')
        except:
            Error('Error parsing element "StatusUploadBlob".  Unable to send status reports')
            return -1

        LogIfVerbose('Status report ' + status + ' sent to ' + uri)
        return UploadStatusBlob(uri, status.encode("utf-8"))

    def GetCurrentSequenceNumber(self, plugin_base_dir):
        """
        Get the settings file with biggest file number in config folder
        """
        config_dir = os.path.join(plugin_base_dir, 'config')
        seq_no = 0
        for subdir, dirs, files in os.walk(config_dir):
            for file in files:
                try:
                    cur_seq_no = int(os.path.basename(file).split('.')[0])
                    if cur_seq_no > seq_no:
                        seq_no = cur_seq_no
                except ValueError:
                    continue
        return str(seq_no)

    def GenerateAggStatus(self, name, version, reportHeartbeat=False):
        """
        Generate the status which Azure can understand by the status and heartbeat reported by extension
        """
        plugin_base_dir = LibDir + '/' + name + '-' + version + '/'
        current_seq_no = self.GetCurrentSequenceNumber(plugin_base_dir)
        status_file = os.path.join(plugin_base_dir, 'status/', current_seq_no + '.status')
        heartbeat_file = os.path.join(plugin_base_dir, 'heartbeat.log')

        handler_state_file = os.path.join(plugin_base_dir, 'config', 'HandlerState')
        agg_state = 'NotReady'
        handler_state = None
        status_obj = None
        status_code = None
        formatted_message = None
        localized_message = None

        if os.path.exists(handler_state_file):
            handler_state = GetFileContents(handler_state_file).lower()
        if HandlerStatusToAggStatus.has_key(handler_state):
            agg_state = HandlerStatusToAggStatus[handler_state]
        if reportHeartbeat:
            if os.path.exists(heartbeat_file):
                d = int(time.time() - os.stat(heartbeat_file).st_mtime)
                if d > 600:  # not updated for more than 10 min
                    agg_state = 'Unresponsive'
                else:
                    try:
                        heartbeat = json.loads(GetFileContents(heartbeat_file))[0]["heartbeat"]
                        agg_state = heartbeat.get("status")
                        status_code = heartbeat.get("code")
                        formatted_message = heartbeat.get("formattedMessage")
                        localized_message = heartbeat.get("message")
                    except:
                        Error("Incorrect heartbeat file. Ignore it. ")
            else:
                agg_state = 'Unresponsive'
        # get status file reported by extension
        if os.path.exists(status_file):
            # raw status generated by extension is an array, get the first item and remove the unnecessary element
            try:
                status_obj = json.loads(GetFileContents(status_file))[0]
                del status_obj["version"]
            except:
                Error("Incorrect status file. Will NOT settingsStatus in settings. ")
        agg_status_obj = {"handlerName": name, "handlerVersion": version, "status": agg_state, "runtimeSettingsStatus":
            {"sequenceNumber": current_seq_no}}
        if status_obj:
            agg_status_obj["runtimeSettingsStatus"]["settingsStatus"] = status_obj
        if status_code != None:
            agg_status_obj["code"] = status_code
        if formatted_message:
            agg_status_obj["formattedMessage"] = formatted_message
        if localized_message:
            agg_status_obj["message"] = localized_message
        agg_status_string = json.dumps(agg_status_obj)
        LogIfVerbose("Handler Aggregated Status:" + agg_status_string)
        return agg_status_string

    def SetHandlerState(self, handler, state=''):
        zip_dir = LibDir + "/" + handler
        mfile = None
        for root, dirs, files in os.walk(zip_dir):
            for f in files:
                if f in ('HandlerManifest.json'):
                    mfile = os.path.join(root, f)
            if mfile != None:
                break
        if mfile == None:
            Error('SetHandlerState(): HandlerManifest.json not found, cannot set HandlerState.')
            return None
        Log("SetHandlerState: " + handler + ", " + state)
        return SetFileContents(os.path.dirname(mfile) + '/config/HandlerState', state)

    def GetHandlerState(self, handler):
        handlerState = GetFileContents(handler + '/config/HandlerState')
        if (handlerState):
            return handlerState.rstrip('\r\n')
        else:
            return 'NotInstalled'


class HostingEnvironmentConfig(object):
    """
    Parse Hosting enviromnet config and store in
    HostingEnvironmentConfig.xml
    """

    #
    # <HostingEnvironmentConfig version="1.0.0.0" goalStateIncarnation="1">
    #   <StoredCertificates>
    #     <StoredCertificate name="Stored0Microsoft.WindowsAzure.Plugins.RemoteAccess.PasswordEncryption" certificateId="sha1:C093FA5CD3AAE057CB7C4E04532B2E16E07C26CA" storeName="My" configurationLevel="System" />
    #   </StoredCertificates>
    #   <Deployment name="db00a7755a5e4e8a8fe4b19bc3b330c3" guid="{ce5a036f-5c93-40e7-8adf-2613631008ab}" incarnation="2">
    #     <Service name="MyVMRoleService" guid="{00000000-0000-0000-0000-000000000000}" />
    #     <ServiceInstance name="db00a7755a5e4e8a8fe4b19bc3b330c3.1" guid="{d113f4d7-9ead-4e73-b715-b724b5b7842c}" />
    #   </Deployment>
    #   <Incarnation number="1" instance="MachineRole_IN_0" guid="{a0faca35-52e5-4ec7-8fd1-63d2bc107d9b}" />
    #   <Role guid="{73d95f1c-6472-e58e-7a1a-523554e11d46}" name="MachineRole" hostingEnvironmentVersion="1" software="" softwareType="ApplicationPackage" entryPoint="" parameters="" settleTimeSeconds="10" />
    #   <HostingEnvironmentSettings name="full" Runtime="rd_fabric_stable.110217-1402.RuntimePackage_1.0.0.8.zip">
    #     <CAS mode="full" />
    #     <PrivilegeLevel mode="max" />
    #     <AdditionalProperties><CgiHandlers></CgiHandlers></AdditionalProperties>
    #   </HostingEnvironmentSettings>
    #   <ApplicationSettings>
    #     <Setting name="__ModelData" value="&lt;m role=&quot;MachineRole&quot; xmlns=&quot;urn:azure:m:v1&quot;>&lt;r name=&quot;MachineRole&quot;>&lt;e name=&quot;a&quot; />&lt;e name=&quot;b&quot; />&lt;e name=&quot;Microsoft.WindowsAzure.Plugins.RemoteAccess.Rdp&quot; />&lt;e name=&quot;Microsoft.WindowsAzure.Plugins.RemoteForwarder.RdpInput&quot; />&lt;/r>&lt;/m>" />
    #     <Setting name="Microsoft.WindowsAzure.Plugins.Diagnostics.ConnectionString" value="DefaultEndpointsProtocol=http;AccountName=osimages;AccountKey=DNZQ..." />
    #     <Setting name="Microsoft.WindowsAzure.Plugins.RemoteForwarder.Enabled" value="true" />
    #   </ApplicationSettings>
    #   <ResourceReferences>
    #     <Resource name="DiagnosticStore" type="directory" request="Microsoft.Cis.Fabric.Controller.Descriptions.ServiceDescription.Data.Policy" sticky="true" size="1" path="db00a7755a5e4e8a8fe4b19bc3b330c3.MachineRole.DiagnosticStore\" disableQuota="false" />
    #   </ResourceReferences>
    # </HostingEnvironmentConfig>
    #
    def __init__(self):
        self.reinitialize()

    def reinitialize(self):
        """
        Reset Members.
        """
        self.StoredCertificates = None
        self.Deployment = None
        self.Incarnation = None
        self.Role = None
        self.HostingEnvironmentSettings = None
        self.ApplicationSettings = None
        self.Certificates = None
        self.ResourceReferences = None

    def Parse(self, xmlText):
        """
        Parse and create HostingEnvironmentConfig.xml.
        """
        self.reinitialize()
        SetFileContents("HostingEnvironmentConfig.xml", xmlText)
        dom = xml.dom.minidom.parseString(xmlText)
        for a in ["HostingEnvironmentConfig", "Deployment", "Service",
                  "ServiceInstance", "Incarnation", "Role", ]:
            if not dom.getElementsByTagName(a):
                Error("HostingEnvironmentConfig.Parse: Missing " + a)
                return None
        node = dom.childNodes[0]
        if node.localName != "HostingEnvironmentConfig":
            Error("HostingEnvironmentConfig.Parse: root not HostingEnvironmentConfig")
            return None
        self.ApplicationSettings = dom.getElementsByTagName("Setting")
        self.Certificates = dom.getElementsByTagName("StoredCertificate")
        return self

    def DecryptPassword(self, e):
        """
        Return decrypted password.
        """
        SetFileContents("password.p7m",
                        "MIME-Version: 1.0\n"
                        + "Content-Disposition: attachment; filename=\"password.p7m\"\n"
                        + "Content-Type: application/x-pkcs7-mime; name=\"password.p7m\"\n"
                        + "Content-Transfer-Encoding: base64\n\n"
                        + textwrap.fill(e, 64))
        return RunGetOutput(Openssl + " cms -decrypt -in password.p7m -inkey Certificates.pem -recip Certificates.pem")[
            1]

    def ActivateResourceDisk(self):
        return MyDistro.ActivateResourceDisk()

    def Process(self):
        """
        Execute ActivateResourceDisk in separate thread.
        Create the user account.
        Launch ConfigurationConsumer if specified in the config.
        """
        no_thread = False
        if DiskActivated == False:
            for m in inspect.getmembers(MyDistro):
                if 'ActivateResourceDiskNoThread' in m:
                    no_thread = True
                    break
            if no_thread == True:
                MyDistro.ActivateResourceDiskNoThread()
            else:
                diskThread = threading.Thread(target=self.ActivateResourceDisk)
                diskThread.start()
        User = None
        Pass = None
        Expiration = None
        Thumbprint = None
        for b in self.ApplicationSettings:
            sname = b.getAttribute("name")
            svalue = b.getAttribute("value")
        if User != None and Pass != None:
            if User != "root" and User != "" and Pass != "":
                CreateAccount(User, Pass, Expiration, Thumbprint)
            else:
                Error("Not creating user account: " + User)
        for c in self.Certificates:
            csha1 = c.getAttribute("certificateId").split(':')[1].upper()
            if os.path.isfile(csha1 + ".prv"):
                Log("Private key with thumbprint: " + csha1 + " was retrieved.")
            if os.path.isfile(csha1 + ".crt"):
                Log("Public cert with thumbprint: " + csha1 + " was retrieved.")
        program = Config.get("Role.ConfigurationConsumer")
        if program != None:
            try:
                Children.append(subprocess.Popen([program, LibDir + "/HostingEnvironmentConfig.xml"]))
            except OSError as e:
                ErrorWithPrefix('HostingEnvironmentConfig.Process',
                                'Exception: ' + str(e) + ' occured launching ' + program)



class WALAEvent(object):
    def __init__(self):

        self.providerId = ""
        self.eventId = 1

        self.OpcodeName = ""
        self.KeywordName = ""
        self.TaskName = ""
        self.TenantName = ""
        self.RoleName = ""
        self.RoleInstanceName = ""
        self.ContainerId = ""
        self.ExecutionMode = "IAAS"
        self.OSVersion = ""
        self.GAVersion = ""
        self.RAM = 0
        self.Processors = 0

    def ToXml(self):
        strEventid = u'<Event id="{0}"/>'.format(self.eventId)
        strProviderid = u'<Provider id="{0}"/>'.format(self.providerId)
        strRecordFormat = u'<Param Name="{0}" Value="{1}" T="{2}" />'
        strRecordNoQuoteFormat = u'<Param Name="{0}" Value={1} T="{2}" />'
        strMtStr = u'mt:wstr'
        strMtUInt64 = u'mt:uint64'
        strMtBool = u'mt:bool'
        strMtFloat = u'mt:float64'
        strEventsData = u""

        for attName in self.__dict__:
            if attName in ["eventId", "filedCount", "providerId"]:
                continue

            attValue = self.__dict__[attName]
            if type(attValue) is int:
                strEventsData += strRecordFormat.format(attName, attValue, strMtUInt64)
                continue
            if type(attValue) is str:
                attValue = xml.sax.saxutils.quoteattr(attValue)
                strEventsData += strRecordNoQuoteFormat.format(attName, attValue, strMtStr)
                continue
            if str(type(attValue)).count("'unicode'") > 0:
                attValue = xml.sax.saxutils.quoteattr(attValue)
                strEventsData += strRecordNoQuoteFormat.format(attName, attValue, strMtStr)
                continue
            if type(attValue) is bool:
                strEventsData += strRecordFormat.format(attName, attValue, strMtBool)
                continue
            if type(attValue) is float:
                strEventsData += strRecordFormat.format(attName, attValue, strMtFloat)
                continue

            Log("Warning: property " + attName + ":" + str(type(attValue)) + ":type" + str(
                type(attValue)) + "Can't convert to events data:" + ":type not supported")

        return u"<Data>{0}{1}{2}</Data>".format(strProviderid, strEventid, strEventsData)

    def Save(self):
        eventfolder = LibDir + "/events"
        if not os.path.exists(eventfolder):
            os.mkdir(eventfolder)
            os.chmod(eventfolder, 0o700)
        if len(os.listdir(eventfolder)) > 1000:
            raise Exception("WriteToFolder:Too many file under " + eventfolder + " exit")

        filename = os.path.join(eventfolder, str(int(time.time() * 1000000)))
        with open(filename + ".tmp", 'wb+') as hfile:
            hfile.write(self.ToXml().encode("utf-8"))
        os.rename(filename + ".tmp", filename + ".tld")


class WALAEventOperation:
    HeartBeat = "HeartBeat"
    Provision = "Provision"
    Install = "Install"
    UnIsntall = "UnInstall"
    Disable = "Disable"
    Enable = "Enable"
    Download = "Download"
    Upgrade = "Upgrade"
    Update = "Update"


def AddExtensionEvent(name, op, isSuccess, duration=0, version="1.0", message="", type="", isInternal=False):
    event = ExtensionEvent()
    event.Name = name
    event.Version = version
    event.IsInternal = isInternal
    event.Operation = op
    event.OperationSuccess = isSuccess
    event.Message = message
    event.Duration = duration
    event.ExtensionType = type
    try:
        event.Save()
    except:
        Error("Error " + traceback.format_exc())


class ExtensionEvent(WALAEvent):
    def __init__(self):
        WALAEvent.__init__(self)
        self.eventId = 1
        self.providerId = "69B669B9-4AF8-4C50-BDC4-6006FA76E975"
        self.Name = ""
        self.Version = ""
        self.IsInternal = False
        self.Operation = ""
        self.OperationSuccess = True
        self.ExtensionType = ""
        self.Message = ""
        self.Duration = 0


class WALAEventMonitor(WALAEvent):
    def __init__(self, postMethod):
        WALAEvent.__init__(self)
        self.post = postMethod
        self.sysInfo = {}
        self.eventdir = LibDir + "/events"
        self.issysteminfoinitilized = False

    def StartEventsLoop(self):
        eventThread = threading.Thread(target=self.EventsLoop)
        eventThread.setDaemon(True)
        eventThread.start()

    def EventsLoop(self):
        LastReportHeartBeatTime = datetime.datetime.min
        try:
            while True:
                if (datetime.datetime.now() - LastReportHeartBeatTime) > \
                        datetime.timedelta(minutes=30):
                    LastReportHeartBeatTime = datetime.datetime.now()
                    AddExtensionEvent(op=WALAEventOperation.HeartBeat, name="WALA", isSuccess=True)
                self.postNumbersInOneLoop = 0
                self.CollectAndSendWALAEvents()
                time.sleep(60)
        except:
            Error("Exception in events loop:" + traceback.format_exc())

    def SendEvent(self, providerid, events):
        dataFormat = u'<?xml version="1.0"?><TelemetryData version="1.0"><Provider id="{0}">{1}' \
                     '</Provider></TelemetryData>'
        data = dataFormat.format(providerid, events)
        self.post("/machine/?comp=telemetrydata", data)

    def CollectAndSendWALAEvents(self):
        if not os.path.exists(self.eventdir):
            return
        # Throtting, can't send more than 3 events in 15 seconds
        eventSendNumber = 0
        eventFiles = os.listdir(self.eventdir)
        events = {}
        for file in eventFiles:
            if not file.endswith(".tld"):
                continue
            with open(os.path.join(self.eventdir, file), "rb") as hfile:
                # if fail to open or delete the file, throw exception
                xmlStr = hfile.read().decode("utf-8", 'ignore')
            os.remove(os.path.join(self.eventdir, file))
            params = ""
            eventid = ""
            providerid = ""
            # if exception happen during process an event, catch it and continue
            try:
                xmlStr = self.AddSystemInfo(xmlStr)
                for node in xml.dom.minidom.parseString(xmlStr.encode("utf-8")).childNodes[0].childNodes:
                    if node.tagName == "Param":
                        params += node.toxml()
                    if node.tagName == "Event":
                        eventid = node.getAttribute("id")
                    if node.tagName == "Provider":
                        providerid = node.getAttribute("id")
            except:
                Error(traceback.format_exc())
                continue
            if len(params) == 0 or len(eventid) == 0 or len(providerid) == 0:
                Error("Empty filed in params:" + params + " event id:" + eventid + " provider id:" + providerid)
                continue

            eventstr = u'<Event id="{0}"><![CDATA[{1}]]></Event>'.format(eventid, params)
            if not events.get(providerid):
                events[providerid] = ""
            if len(events[providerid]) > 0 and len(events.get(providerid) + eventstr) >= 63 * 1024:
                eventSendNumber += 1
                self.SendEvent(providerid, events.get(providerid))
                if eventSendNumber % 3 == 0:
                    time.sleep(15)
                events[providerid] = ""
            if len(eventstr) >= 63 * 1024:
                Error("Signle event too large abort " + eventstr[:300])
                continue

            events[providerid] = events.get(providerid) + eventstr

        for key in events.keys():
            if len(events[key]) > 0:
                eventSendNumber += 1
                self.SendEvent(key, events[key])
                if eventSendNumber % 3 == 0:
                    time.sleep(15)

    def AddSystemInfo(self, eventData):
        if not self.issysteminfoinitilized:
            self.issysteminfoinitilized = True
            try:
                self.sysInfo["OSVersion"] = platform.system() + ":" + "-".join(DistInfo(1)) + ":" + platform.release()
                self.sysInfo["GAVersion"] = GuestAgentVersion
                self.sysInfo["RAM"] = MyDistro.getTotalMemory()
                self.sysInfo["Processors"] = MyDistro.getProcessorCores()
                sharedConfig = xml.dom.minidom.parse("/var/lib/waagent/SharedConfig.xml").childNodes[0]
                hostEnvConfig = xml.dom.minidom.parse("/var/lib/waagent/HostingEnvironmentConfig.xml").childNodes[0]
                gfiles = RunGetOutput("ls -t /var/lib/waagent/GoalState.*.xml")[1]
                goalStateConfi = xml.dom.minidom.parse(gfiles.split("\n")[0]).childNodes[0]
                self.sysInfo["TenantName"] = hostEnvConfig.getElementsByTagName("Deployment")[0].getAttribute("name")
                self.sysInfo["RoleName"] = hostEnvConfig.getElementsByTagName("Role")[0].getAttribute("name")
                self.sysInfo["RoleInstanceName"] = sharedConfig.getElementsByTagName("Instance")[0].getAttribute("id")
                self.sysInfo["ContainerId"] = goalStateConfi.getElementsByTagName("ContainerId")[0].childNodes[
                    0].nodeValue
            except:
                Error(traceback.format_exc())

        eventObject = xml.dom.minidom.parseString(eventData.encode("utf-8")).childNodes[0]
        for node in eventObject.childNodes:
            if node.tagName == "Param":
                name = node.getAttribute("Name")
                if self.sysInfo.get(name):
                    node.setAttribute("Value", xml.sax.saxutils.escape(str(self.sysInfo[name])))

        return eventObject.toxml()



WaagentLogrotate = """\
/var/log/waagent.log {
    monthly
    rotate 6
    notifempty
    missingok
}
"""


def GetMountPoint(mountlist, device):
    """
    Example of mountlist:
        /dev/sda1 on / type ext4 (rw)
        proc on /proc type proc (rw)
        sysfs on /sys type sysfs (rw)
        devpts on /dev/pts type devpts (rw,gid=5,mode=620)
        tmpfs on /dev/shm type tmpfs (rw,rootcontext="system_u:object_r:tmpfs_t:s0")
        none on /proc/sys/fs/binfmt_misc type binfmt_misc (rw)
        /dev/sdb1 on /mnt/resource type ext4 (rw)
    """
    if (mountlist and device):
        for entry in mountlist.split('\n'):
            if (re.search(device, entry)):
                tokens = entry.split()
                # Return the 3rd column of this line
                return tokens[2] if len(tokens) > 2 else None
    return None


def FindInLinuxKernelCmdline(option):
    """
    Return match object if 'option' is present in the kernel boot options
    of the grub configuration.
    """
    m = None
    matchs = r'^.*?' + MyDistro.grubKernelBootOptionsLine + r'.*?' + option + r'.*$'
    try:
        m = FindStringInFile(MyDistro.grubKernelBootOptionsFile, matchs)
    except IOError as e:
        Error(
            'FindInLinuxKernelCmdline: Exception opening ' + MyDistro.grubKernelBootOptionsFile + 'Exception:' + str(e))

    return m


def AppendToLinuxKernelCmdline(option):
    """
    Add 'option' to the kernel boot options of the grub configuration.
    """
    if not FindInLinuxKernelCmdline(option):
        src = r'^(.*?' + MyDistro.grubKernelBootOptionsLine + r')(.*?)("?)$'
        rep = r'\1\2 ' + option + r'\3'
        try:
            ReplaceStringInFile(MyDistro.grubKernelBootOptionsFile, src, rep)
        except IOError as e:
            Error(
                'AppendToLinuxKernelCmdline: Exception opening ' + MyDistro.grubKernelBootOptionsFile + 'Exception:' + str(
                    e))
            return 1
        Run("update-grub", chk_err=False)
    return 0


def RemoveFromLinuxKernelCmdline(option):
    """
    Remove 'option' to the kernel boot options of the grub configuration.
    """
    if FindInLinuxKernelCmdline(option):
        src = r'^(.*?' + MyDistro.grubKernelBootOptionsLine + r'.*?)(' + option + r')(.*?)("?)$'
        rep = r'\1\3\4'
        try:
            ReplaceStringInFile(MyDistro.grubKernelBootOptionsFile, src, rep)
        except IOError as e:
            Error(
                'RemoveFromLinuxKernelCmdline: Exception opening ' + MyDistro.grubKernelBootOptionsFile + 'Exception:' + str(
                    e))
            return 1
        Run("update-grub", chk_err=False)
    return 0


def FindStringInFile(fname, matchs):
    """
    Return match object if found in file.
    """
    try:
        ms = re.compile(matchs)
        for l in (open(fname, 'r')).readlines():
            m = re.search(ms, l)
            if m:
                return m
    except:
        raise

    return None


def ReplaceStringInFile(fname, src, repl):
    """
    Replace 'src' with 'repl' in file.
    """
    try:
        sr = re.compile(src)
        if FindStringInFile(fname, src):
            updated = ''
            for l in (open(fname, 'r')).readlines():
                n = re.sub(sr, repl, l)
                updated += n
            ReplaceFileContentsAtomic(fname, updated)
    except:
        raise
    return


def ApplyVNUMAWorkaround():
    """
    If kernel version has NUMA bug, add 'numa=off' to
    kernel boot options.
    """
    VersionParts = platform.release().replace('-', '.').split('.')
    if int(VersionParts[0]) > 2:
        return
    if int(VersionParts[1]) > 6:
        return
    if int(VersionParts[2]) > 37:
        return
    if AppendToLinuxKernelCmdline("numa=off") == 0:
        Log("Your kernel version " + platform.release() + " has a NUMA-related bug: NUMA has been disabled.")
    else:
        "Error adding 'numa=off'.  NUMA has not been disabled."


def RevertVNUMAWorkaround():
    """
    Remove 'numa=off' from kernel boot options.
    """
    if RemoveFromLinuxKernelCmdline("numa=off") == 0:
        Log('NUMA has been re-enabled')
    else:
        Log('NUMA has not been re-enabled')


def Install():
    """
    Install the agent service.
    Check dependencies.
    Create /etc/waagent.conf and move old version to
    /etc/waagent.conf.old
    Copy RulesFiles to /var/lib/waagent
    Create /etc/logrotate.d/waagent
    Set /etc/ssh/sshd_config ClientAliveInterval to 180
    Call ApplyVNUMAWorkaround()
    """
    if MyDistro.checkDependencies():
        return 1
    os.chmod(sys.argv[0], 0o755)
    SwitchCwd()
    for a in RulesFiles:
        if os.path.isfile(a):
            if os.path.isfile(GetLastPathElement(a)):
                os.remove(GetLastPathElement(a))
            shutil.move(a, ".")
            Warn("Moved " + a + " -> " + LibDir + "/" + GetLastPathElement(a))
    MyDistro.registerAgentService()
    if os.path.isfile("/etc/waagent.conf"):
        try:
            os.remove("/etc/waagent.conf.old")
        except:
            pass
        try:
            os.rename("/etc/waagent.conf", "/etc/waagent.conf.old")
            Warn("Existing /etc/waagent.conf has been renamed to /etc/waagent.conf.old")
        except:
            pass
    SetFileContents("/etc/waagent.conf", MyDistro.waagent_conf_file)
    SetFileContents("/etc/logrotate.d/waagent", WaagentLogrotate)
    filepath = "/etc/ssh/sshd_config"
    ReplaceFileContentsAtomic(filepath, "\n".join(filter(lambda a: not
    a.startswith("ClientAliveInterval"),
                                                         GetFileContents(filepath).split(
                                                             '\n'))) + "\nClientAliveInterval 180\n")
    Log("Configured SSH client probing to keep connections alive.")
    ApplyVNUMAWorkaround()
    return 0


def GetMyDistro(dist_class_name=''):
    """
    Return MyDistro object.
    NOTE: Logging is not initialized at this point.
    """
    if dist_class_name == '':
        if 'Linux' in platform.system():
            Distro = DistInfo()[0]
        else:  # I know this is not Linux!
            if 'FreeBSD' in platform.system():
                Distro = platform.system()
            if 'NS-BSD' in platform.system():
                Distro = platform.system()
                Distro = Distro.replace("-", "")
        Distro = Distro.strip('"')
        Distro = Distro.strip(' ')
        dist_class_name = Distro + 'Distro'
        if dist_class_name not in globals():
            if ('SuSE'.lower() in Distro.lower()):
                Distro = 'SuSE'
            elif ('Ubuntu'.lower() in Distro.lower()):
                Distro = 'Ubuntu'
            elif ('centos'.lower() in Distro.lower()  or 'big-ip'.lower() in Distro.lower()):
                Distro = 'centos'
            elif ('debian'.lower() in Distro.lower()):
                Distro = 'debian'
            elif ('oracle'.lower() in Distro.lower()):
                Distro = 'oracle'
            elif ('redhat'.lower() in Distro.lower()):
                Distro = 'redhat'
            elif ('Kali'.lower() in Distro.lower()):
                Distro = 'Kali'
            elif ('FreeBSD'.lower() in  Distro.lower() or 'gaia'.lower() in Distro.lower() or 'panos'.lower() in Distro.lower()):
                Distro = 'FreeBSD'
            else:
                Distro = 'Default'
            dist_class_name = Distro + 'Distro'
    else:
        Distro = dist_class_name
    if dist_class_name not in globals():
        ##print Distro + ' is not a supported distribution.'
        return None
    return globals()[dist_class_name]()  # the distro class inside this module.

def DistInfo(fullname=0):
    try:
        if 'FreeBSD' in platform.system():
            release = re.sub('\-.*\Z', '', str(platform.release()))
            distinfo = ['FreeBSD', release]
            return distinfo
        if 'NS-BSD' in platform.system():
            release = re.sub('\-.*\Z', '', str(platform.release()))
            distinfo = ['NS-BSD', release]
            return distinfo
        if 'linux_distribution' in dir(platform):
            distinfo = list(platform.linux_distribution(full_distribution_name=0))
            # remove trailing whitespace in distro name
            if(distinfo[0] == ''):
                osfile= open("/etc/os-release", "r")
                for line in osfile:
                    lists=str(line).split("=")
                    if(lists[0]== "NAME"):
                        distname = lists[1].split("\"")
                        distinfo[0] = distname[1]
                        if(distinfo[0].lower() == "sles"):
                            distinfo[0] = "SuSE"
                osfile.close()
            distinfo[0] = distinfo[0].strip()
            return distinfo
        if 'Linux' in platform.system():
            if "ubuntu" in platform.version().lower():
                distinfo[0] = "Ubuntu"
            elif 'suse' in platform.version().lower():
                distinfo[0] = "SuSE"
            elif 'centos' in platform.version().lower():
                distinfo[0] = "centos"
            elif 'debian' in platform.version().lower():
                distinfo[0] = "debian"
            elif 'oracle' in platform.version().lower():
                distinfo[0] = "oracle"
            elif 'redhat' in platform.version().lower() or 'rhel' in platform.version().lower():
                distinfo[0] = "redhat"
            elif 'kali' in platform.version().lower():
                distinfo[0] = "Kali"
            else:
                distinfo[0] = "Default"
            return distinfo
        else:
            return platform.dist()
    except Exception as e:
        errMsg = 'Failed to retrieve the distinfo with error: %s, stack trace: %s' % (str(e), traceback.format_exc())
        logger.log(errMsg)
        distinfo = ['Abstract','1.0']
        return distinfo

def PackagedInstall(buildroot):
    """
    Called from setup.py for use by RPM.
    Generic implementation Creates directories and
    files /etc/waagent.conf, /etc/init.d/waagent, /usr/sbin/waagent,
    /etc/logrotate.d/waagent, /etc/sudoers.d/waagent under buildroot.
    Copies generated files waagent.conf, into place and exits.
    """
    MyDistro = GetMyDistro()
    if MyDistro == None:
        sys.exit(1)
    MyDistro.packagedInstall(buildroot)


def LibraryInstall(buildroot):
    pass


def Uninstall():
    """
    Uninstall the agent service.
    Copy RulesFiles back to original locations.
    Delete agent-related files.
    Call RevertVNUMAWorkaround().
    """
    SwitchCwd()
    for a in RulesFiles:
        if os.path.isfile(GetLastPathElement(a)):
            try:
                shutil.move(GetLastPathElement(a), a)
                Warn("Moved " + LibDir + "/" + GetLastPathElement(a) + " -> " + a)
            except:
                pass
    MyDistro.unregisterAgentService()
    MyDistro.uninstallDeleteFiles()
    RevertVNUMAWorkaround()
    return 0


def Deprovision(force, deluser):
    """
    Remove user accounts created by provisioning.
    Disables root password if Provisioning.DeleteRootPassword = 'y'
    Stop agent service.
    Remove SSH host keys if they were generated by the provision.
    Set hostname to 'localhost.localdomain'.
    Delete cached system configuration files in /var/lib and /var/lib/waagent.
    """

    # Append blank line at the end of file, so the ctime of this file is changed every time
    Run("echo ''>>" + MyDistro.getConfigurationPath())

    SwitchCwd()


    print("WARNING! The waagent service will be stopped.")
    print("WARNING! All SSH host key pairs will be deleted.")
    print("WARNING! Cached DHCP leases will be deleted.")
    MyDistro.deprovisionWarnUser()
    delRootPass = Config.get("Provisioning.DeleteRootPassword")
    if delRootPass != None and delRootPass.lower().startswith("y"):
        print("WARNING! root password will be disabled. You will not be able to login as root.")

    try:
        input = raw_input
    except NameError:
        pass
    if force == False and not input('Do you want to proceed (y/n)? ').startswith('y'):
        return 1

    MyDistro.stopAgentService()

    # Remove SSH host keys
    regenerateKeys = Config.get("Provisioning.RegenerateSshHostKeyPair")
    if regenerateKeys == None or regenerateKeys.lower().startswith("y"):
        Run("rm -f /etc/ssh/ssh_host_*key*")

    # Remove root password
    if delRootPass != None and delRootPass.lower().startswith("y"):
        MyDistro.deleteRootPassword()
    # Remove distribution specific networking configuration

    MyDistro.publishHostname('localhost.localdomain')
    MyDistro.deprovisionDeleteFiles()
    return 0


def SwitchCwd():
    """
    Switch to cwd to /var/lib/waagent.
    Create if not present.
    """
    CreateDir(LibDir, "root", 0o700)
    os.chdir(LibDir)


def Usage():
    """
    Print the arguments to waagent.
    """
    print("usage: " + sys.argv[
        0] + " [-verbose] [-force] [-help|-install|-uninstall|-deprovision[+user]|-version|-serialconsole|-daemon]")
    return 0


def main():
    """
    Instantiate MyDistro, exit if distro class is not defined.
    Parse command-line arguments, exit with usage() on error.
    Instantiate ConfigurationProvider.
    Call appropriate non-daemon methods and exit.
    If daemon mode, enter Agent.Run() loop.
    """
    if GuestAgentVersion == "":
        print("WARNING! This is a non-standard agent that does not include a valid version string.")

    if len(sys.argv) == 1:
        sys.exit(Usage())

    LoggerInit('/var/log/waagent.log', '/dev/console')
    global LinuxDistro
    LinuxDistro = DistInfo()[0]

    global MyDistro
    MyDistro = GetMyDistro()
    if MyDistro == None:
        sys.exit(1)
    args = []
    conf_file = None
    global force
    force = False
    for a in sys.argv[1:]:
        if re.match("^([-/]*)(help|usage|\?)", a):
            sys.exit(Usage())
        elif re.match("^([-/]*)version", a):
            print(GuestAgentVersion + " running on " + LinuxDistro)
            sys.exit(0)
        elif re.match("^([-/]*)verbose", a):
            myLogger.verbose = True
        elif re.match("^([-/]*)force", a):
            force = True
        elif re.match("^(?:[-/]*)conf=.+", a):
            conf_file = re.match("^(?:[-/]*)conf=(.+)", a).groups()[0]
        elif re.match("^([-/]*)(setup|install)", a):
            sys.exit(MyDistro.Install())
        elif re.match("^([-/]*)(uninstall)", a):
            sys.exit(Uninstall())
        else:
            args.append(a)
    global Config
    Config = ConfigurationProvider(conf_file)

    logfile = Config.get("Logs.File")
    if logfile is not None:
        myLogger.file_path = logfile
    logconsole = Config.get("Logs.Console")
    if logconsole is not None and logconsole.lower().startswith("n"):
        myLogger.con_path = None
    verbose = Config.get("Logs.Verbose")
    if verbose != None and verbose.lower().startswith("y"):
        myLogger.verbose = True
    global daemon
    daemon = False
    for a in args:
        if re.match("^([-/]*)deprovision\+user", a):
            sys.exit(Deprovision(force, True))
        elif re.match("^([-/]*)deprovision", a):
            sys.exit(Deprovision(force, False))
        elif re.match("^([-/]*)daemon", a):
            daemon = True
        elif re.match("^([-/]*)serialconsole", a):
            AppendToLinuxKernelCmdline("console=ttyS0 earlyprintk=ttyS0")
            Log("Configured kernel to use ttyS0 as the boot console.")
            sys.exit(0)
        else:
            print("Invalid command line parameter:" + a)
            sys.exit(1)

    if daemon == False:
        sys.exit(Usage())
    global modloaded
    modloaded = False

    while True:
        try:
            SwitchCwd()
            Log(GuestAgentLongName + " Version: " + GuestAgentVersion)
            if IsLinux():
                Log("Linux Distribution Detected      : " + LinuxDistro)
        except Exception as e:
            Error(traceback.format_exc())
            Error("Exception: " + str(e))
            Log("Restart agent in 15 seconds")
            time.sleep(15)


if __name__ == '__main__':
    main()
