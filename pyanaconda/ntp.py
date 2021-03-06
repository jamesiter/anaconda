#
# Copyright (C) 2012-2013  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

"""
Module facilitating the work with NTP servers and NTP daemon's configuration

"""

import re
import os
import tempfile
import shutil
import ntplib
import socket

from pyanaconda import isys
from pyanaconda.anaconda_loggers import get_module_logger
from pyanaconda.core.i18n import N_, _
from pyanaconda.core.constants import THREAD_SYNC_TIME_BASENAME, NTP_SERVER_QUERY, \
    THREAD_NTP_SERVER_CHECK, NTP_SERVER_OK, NTP_SERVER_NOK
from pyanaconda.modules.common.structures.timezone import TimeSourceData
from pyanaconda.threading import threadMgr, AnacondaThread

NTP_CONFIG_FILE = "/etc/chrony.conf"

#example line:
#server 0.fedora.pool.ntp.org iburst
SRV_LINE_REGEXP = re.compile(r"^\s*(server|pool)\s*([-a-zA-Z.0-9]+)\s?([a-zA-Z0-9\s]*)$")
SRV_NOARG_OPTIONS = ["burst", "iburst", "nts", "prefer", "require", "trust", "noselect", "xleave"]
SRV_ARG_OPTIONS = ["key", "minpoll", "maxpoll"]

#treat pools as four servers with the same name
SERVERS_PER_POOL = 4

# Description of an NTP server status.
NTP_SERVER_STATUS_DESCRIPTIONS = {
    NTP_SERVER_OK: N_("status: working"),
    NTP_SERVER_NOK: N_("status: not working"),
    NTP_SERVER_QUERY: N_("checking status")
}

log = get_module_logger(__name__)


class NTPconfigError(Exception):
    """Exception class for NTP related problems"""
    pass


def get_ntp_server_summary(server, states):
    """Generate a summary of an NTP server and its status.

    :param server: an NTP server
    :type server: an instance of TimeSourceData
    :param states: a cache of NTP server states
    :type states: an instance of NTPServerStatusCache
    :return: a string with a summary
    """
    return "{} ({})".format(
        server.hostname,
        states.get_status_description(server)
    )


def get_ntp_servers_summary(servers, states):
    """Generate a summary of NTP servers and their states.

    :param servers: a list of NTP servers
    :type servers: a list of TimeSourceData
    :param states: a cache of NTP server states
    :type states: an instance of NTPServerStatusCache
    :return: a string with a summary
    """
    summary = _("NTP servers:")

    for server in servers:
        summary += "\n" + get_ntp_server_summary(server, states)

    if not servers:
        summary += " " + _("not configured")

    return summary


def ntp_server_working(server_hostname, nts_enabled):
    """Tries to do an NTP request to the server (timeout may take some time).

    If NTS is enabled, try making a TCP connection to the NTS-KE port instead.

    :param server_hostname: a host name or an IP address of an NTP server
    :type server_hostname: string
    :return: True if the given server is reachable and working, False otherwise
    :rtype: bool
    """
    try:
        # ntplib doesn't support NTS
        if nts_enabled:
            s = socket.create_connection((server_hostname, 4460), 2)
            s.close()
        else:
            client = ntplib.NTPClient()
            client.request(server_hostname)
    except ntplib.NTPException:
        return False
    # address related error
    except socket.gaierror:
        return False
    # socket related error
    # (including "Network is unreachable")
    except socket.error:
        return False

    return True


def get_servers_from_config(conf_file_path=NTP_CONFIG_FILE):
    """Get NTP servers from a configuration file.

    Goes through the chronyd's configuration file looking for lines starting
    with 'server'.

    :param conf_file_path: a path to the chronyd's configuration file
    :return: servers found in the chronyd's configuration
    :rtype: a list of TimeSourceData instances
    """
    servers = []

    try:
        with open(conf_file_path, "r") as conf_file:
            for line in conf_file:
                match = SRV_LINE_REGEXP.match(line)

                if not match:
                    continue

                server = TimeSourceData()
                server.type = match.group(1).upper()
                server.hostname = match.group(2)
                server.options = []

                words = match.group(3).lower().split()
                skip_argument = False

                for i in range(len(words)):
                    if skip_argument:
                        skip_argument = False
                        continue
                    if words[i] in SRV_NOARG_OPTIONS:
                        server.options.append(words[i])
                    elif words[i] in SRV_ARG_OPTIONS and i + 1 < len(words):
                        server.options.append(' '.join(words[i:i+2]))
                        skip_argument = True
                    else:
                        log.debug("Unknown NTP server option %s", words[i])

                servers.append(server)

    except IOError as ioerr:
        msg = "Cannot open config file {} for reading ({})."
        raise NTPconfigError(msg.format(conf_file_path, ioerr.strerror))

    return servers


def save_servers_to_config(servers, conf_file_path=NTP_CONFIG_FILE, out_file_path=None):
    """Save NTP servers to a configuration file.

    Replaces the pools and servers defined in the chronyd's configuration file
    with the given ones. If the out_file is not None, then it is used for the
    resulting config.

    :param servers: a list of NTP servers and pools
    :type servers: a list of TimeSourceData instances
    :param conf_file_path: a path to the chronyd's configuration file
    :param out_file_path: a path to the file used for the resulting config
    """
    temp_path = None

    try:
        old_conf_file = open(conf_file_path, "r")
    except IOError as ioerr:
        msg = "Cannot open config file {} for reading ({})."
        raise NTPconfigError(msg.format(conf_file_path, ioerr.strerror))

    if out_file_path:
        try:
            new_conf_file = open(out_file_path, "w")
        except IOError as ioerr:
            msg = "Cannot open new config file {} for writing ({})."
            raise NTPconfigError(msg.format(out_file_path, ioerr.strerror))
    else:
        try:
            (fields, temp_path) = tempfile.mkstemp()
            new_conf_file = os.fdopen(fields, "w")
        except IOError as ioerr:
            msg = "Cannot open temporary file {} for writing ({})."
            raise NTPconfigError(msg.format(temp_path, ioerr.strerror))

    heading = "# These servers were defined in the installation:\n"

    # write info about the origin of the following lines
    new_conf_file.write(heading)

    # write new servers and pools
    for server in servers:
        args = [server.type.lower(), server.hostname] + server.options
        line = " ".join(args) + "\n"
        new_conf_file.write(line)

    new_conf_file.write("\n")

    # copy non-server lines from the old config and skip our heading
    for line in old_conf_file:
        if not SRV_LINE_REGEXP.match(line) and line != heading:
            new_conf_file.write(line)

    old_conf_file.close()
    new_conf_file.close()

    if not out_file_path:
        try:
            # Use copy rather then move to get the correct selinux context
            shutil.copyfile(temp_path, conf_file_path)
            os.unlink(temp_path)

        except OSError as oserr:
            msg = "Cannot replace the old config with the new one ({})."
            raise NTPconfigError(msg.format(oserr.strerror))


def _one_time_sync(server, callback=None):
    """Synchronize the system time with a given NTP server.

    Synchronize the system time with a given NTP server. Note that this
    function is blocking and will not return until the time gets synced or
    querying server fails (may take some time before timeouting).

    :param server: an NTP server
    :type server: an instance of TimeSourceData
    :param callback: callback function to run after sync or failure
    :type callback: a function taking one boolean argument (success)
    :return: True if the sync was successful, False otherwise
    """

    client = ntplib.NTPClient()
    try:
        results = client.request(server.hostname)
        isys.set_system_time(int(results.tx_time))
        success = True
    except ntplib.NTPException:
        success = False
    except socket.gaierror:
        success = False

    if callback is not None:
        callback(success)

    return success


def one_time_sync_async(server, callback=None):
    """Asynchronously synchronize the system time with a given NTP server.

    Asynchronously synchronize the system time with a given NTP server. This
    function is non-blocking it starts a new thread for synchronization and
    returns. Use callback argument to specify the function called when the
    new thread finishes if needed.

    :param server: an NTP server
    :type server: an instance of TimeSourceData
    :param callback: callback function to run after sync or failure
    :type callback: a function taking one boolean argument (success)
    """
    thread_name = "%s_%s" % (THREAD_SYNC_TIME_BASENAME, server.hostname)

    # syncing with the same server running
    if threadMgr.get(thread_name):
        return

    threadMgr.add(AnacondaThread(
        name=thread_name,
        target=_one_time_sync,
        args=(server, callback)
    ))


class NTPServerStatusCache(object):
    """The cache of NTP server states."""

    def __init__(self):
        self._cache = {}

    def get_status(self, server):
        """Get the status of the given NTP server.

        :param TimeSourceData server: an NTP server
        :return int: a status of the NTP server
        """
        return self._cache.get(
            server.hostname,
            NTP_SERVER_QUERY
        )

    def get_status_description(self, server):
        """Get the status description of the given NTP server.

        :param TimeSourceData server: an NTP server
        :return str: a status description of the NTP server
        """
        status = self.get_status(server)
        return _(NTP_SERVER_STATUS_DESCRIPTIONS[status])

    def check_status(self, server):
        """Asynchronously check if given NTP servers appear to be working.

        :param TimeSourceData server: an NTP server
        """
        # Get a hostname and NTS option.
        hostname = server.hostname
        nts_enabled = "nts" in server.options

        # Reset the current status.
        self._set_status(hostname, NTP_SERVER_QUERY)

        # Start the check.
        threadMgr.add(AnacondaThread(
            prefix=THREAD_NTP_SERVER_CHECK,
            target=self._check_status,
            args=(hostname, nts_enabled))
        )

    def _set_status(self, hostname, status):
        """Set the status of the given NTP server.

        :param str hostname: a hostname of an NTP server
        :return int: a status of the NTP server
        """
        self._cache[hostname] = status

    def _check_status(self, hostname, nts_enabled):
        """Check if an NTP server appears to be working.

        :param str hostname: a hostname of an NTP server
        """
        log.debug("Checking NTP server %s", hostname)
        result = ntp_server_working(hostname, nts_enabled)

        if result:
            log.debug("NTP server %s appears to be working.", hostname)
            self._set_status(hostname, NTP_SERVER_OK)
        else:
            log.debug("NTP server %s appears not to be working.", hostname)
            self._set_status(hostname, NTP_SERVER_NOK)
