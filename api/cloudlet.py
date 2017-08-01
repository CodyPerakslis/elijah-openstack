# Copyright (C) 2011-2013 Carnegie Mellon University
# Author: Kiryong Ha (krha@cmu.edu)
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of version 2 of the GNU General Public License as published
# by the Free Software Foundation.  A copy of the GNU General Public License
# should have been distributed along with this program in the file
# LICENSE.GPL.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#

import webob
from urlparse import urlsplit

from nova.compute import API
from nova.compute import HostAPI
from nova.compute.cloudlet_api import CloudletAPI as CloudletAPI
from nova import exception
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
try:
    # icehouse
    from nova.openstack.common.gettextutils import _
except ImportError as e:
    # kilo
    from nova.i18n import _

import logging
import logging.config


logging.config.fileConfig('/var/log/nova/cloudlet_log.conf')

LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'cloudlet')


class Cloudlet(extensions.ExtensionDescriptor):

    """Cloudlet compute API support"""

    name = "Cloudlets"
    alias = "os-cloudlet"
    namespace = "http://elijah.cs.cmu.edu/compute/ext/cloudlet/api/v1.1"
    updated = "2014-05-27T00:00:00+00:00"

    def get_controller_extensions(self):
        servers_extension = extensions.ControllerExtension(
            self, 'servers', CloudletController())
        return [servers_extension]


class CloudletController(wsgi.Controller):

    def __init__(self, *args, **kwargs):
        super(CloudletController, self).__init__(*args, **kwargs)
        self.cloudlet_api = CloudletAPI()
        self.host_api = HostAPI()
        self.compute_api = API()

    def _get_instance(self, context, instance_id, want_objects=False):
        try:
            return self.compute_api.get(context, instance_id,
                                        want_objects=want_objects)
        except exception.InstanceNotFound:
            msg = _("Server not found")
            raise webob.exc.HTTPNotFound(explanation=msg)

    def _append_synthesis_info(self, context, body, resp_obj):
        LOG.debug("return synthesis information")
        resp_obj.obj['synthesis'] = {
            "return": "success"
        }

    def _append_port_forwarding(self, context, body, resp_obj):
        LOG.debug("return handoff information")
        if 'server' not in resp_obj.obj:
            return
        compute_nodes = self.host_api.compute_node_get_all(context)
        instance_id = resp_obj.obj['server'].get('id', None)

        # wait until the VM instance is scheduled to the compute node
        # Need fix: seperate one API into two; one for assigning VM to compute
        # node, the other for setting up port forwarding
        repeat_count = 0
        MAX_COUNT = 30
        while repeat_count < MAX_COUNT:
            instance = self.compute_api.get(context, instance_id)
            instance_hostname = instance.get('node', None)
            if instance_hostname is None:
                import time
                time.sleep(0.1)
                msg = "waiting for VM scheduling %d/%d..."\
                    % (repeat_count, MAX_COUNT)
                LOG.debug(msg)
                repeat_count += 1
            else:
                break

        dest_ip = None
        for node in compute_nodes:
            node_name = node.get('hypervisor_hostname', None)
            if str(node_name) == str(instance_hostname):
                dest_ip = node.get('host_ip', None)

        # set port forwarding
        if dest_ip:
            LOG.debug("return port forwarding information")
            dest_port = 8022
            source_port = self.cloudlet_api.handoff_port_forwarding(
                dest_ip, dest_port
            )
            server_url = resp_obj.obj['server']['links'][0]['href']
            server_ipaddr = urlsplit(server_url).netloc.split(":")[0]
            resp_obj.obj['handoff'] = {
                "server_ip": str(server_ipaddr),
                "server_port": int(source_port),
            }
        else:
            resp_obj.obj['handoff'] = {
                "error": "cannot setup port forwarding"
            }

    @wsgi.extends
    def create(self, req, body):
        context = req.environ['nova.context']
        resp_obj = (yield)
        if 'server' in body and 'metadata' in body['server']:
            metadata = body['server']['metadata']
            if ('overlay_url' in metadata) and ('handoff_info' not in metadata):
                # create VM using synthesis
                self._append_synthesis_info(context, body, resp_obj)
            elif 'handoff_info' in metadata:
                # create VM using VM handoff
                self._append_port_forwarding(context, body, resp_obj)

    @wsgi.action('cloudlet-base')
    def cloudlet_base_creation(self, req, id, body):
        """Generate cloudlet base VM
        """
        context = req.environ['nova.context']

        baseVM_name = ''
        if body['cloudlet-base'] and ('name' in body['cloudlet-base']):
            baseVM_name = body['cloudlet-base']['name']
        else:
            msg = _("Name required for base VM.")
            raise webob.exc.HTTPBadRequest(explanation=msg)

        LOG.info(_("Importing base VM %r..."), id)
        instance = self._get_instance(context, id, want_objects=True)
        disk_meta, memory_meta = self.cloudlet_api.cloudlet_create_base(
            context, instance, baseVM_name)
        return {'base-disk': disk_meta, 'base-memory': memory_meta}

    @wsgi.action('cloudlet-overlay-finish')
    def cloudlet_overlay_finish(self, req, id, body):
        """Generate overlay VM from the requested instance
        """
        context = req.environ['nova.context']

        overlay_name = ''
        if 'overlay-name' in body['cloudlet-overlay-finish']:
            overlay_name = body['cloudlet-overlay-finish']['overlay-name']
        else:
            msg = _("Overlay name required.")
            raise webob.exc.HTTPNotFound(explanation=msg)

        LOG.info(_("Generating overlay VM %r..."), id)
        instance = self._get_instance(context, id, want_objects=True)
        overlay_id = self.cloudlet_api.cloudlet_create_overlay_finish(
            context,
            instance,
            overlay_name)
        return {'overlay-id': overlay_id}

    @wsgi.action('cloudlet-handoff')
    def cloudlet_handoff(self, req, id, body):
        """Perform VM migration across OpenStack
        """
        context = req.environ['nova.context']
        payload = body['cloudlet-handoff']
        handoff_url = payload.get("handoff_url", None)
        dest_token = payload.get("dest_token", None)
        dest_vmname = payload.get("dest_vmname", None)
        if handoff_url is None:
            msg = _("Handoff URL Required")
            raise webob.exc.HTTPBadRequest(explanation=msg)
        parsed_handoff_url = urlsplit(handoff_url)
        if parsed_handoff_url.scheme != "file" and\
                parsed_handoff_url.scheme != "http" and\
                parsed_handoff_url.scheme != "https":
            msg = "Invalid handoff URL (%s). " % handoff_url
            msg += "Only file and http(s) schemes are supported."
            raise webob.exc.HTTPBadRequest(explanation=msg)
        if len(parsed_handoff_url.netloc) == 0:
            msg = "Invalid handoff URL (%s). " % handoff_url
            msg += "Destination unreachable."
            raise webob.exc.HTTPBadRequest(explanation=msg)

        if parsed_handoff_url.scheme == "http" or\
                parsed_handoff_url.scheme == "https":
            if dest_token is None:
                msg = "An auth token required to handoff to the destination."
                raise webob.exc.HTTPBadRequest(explanation=msg)

        LOG.info(_("Handoff initiated for %r (destination URL:%s)..."),
                  id, handoff_url)
        instance = self._get_instance(context, id, want_objects=True)
        residue_id = self.cloudlet_api.cloudlet_handoff(context,
                                                        instance,
                                                        handoff_url,
                                                        dest_token=dest_token,
                                                        dest_vmname=dest_vmname)
        if residue_id:
            return {'handoff': "%s" % residue_id}
        else:
            return {'handoff': "%s" % handoff_url}
