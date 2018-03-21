#!/usr/bin/env python
#
# Copyright (c) 2018 SAP SE
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

import click
import logging
import os
import six
import time
import sys

from openstack import connection, exceptions, utils
from keystoneauth1 import loading
from keystoneauth1 import session
from cinderclient import client

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(message)s')

# cmdline handling
@click.command()
# every how many minutes the check should be preformed
@click.option('--interval', prompt='Interval in minutes')
# how often a vm should be continously a canditate for some action (delete etc.) before
# we actually do it - the idea behind is that we want to avoid actions due to short
# temporary technical problems of any kind
@click.option('--iterations', prompt='Iterations')
# work on nova db (vms) or cinder db (volumes)?
@click.option('--nova', is_flag=True)
@click.option('--cinder', is_flag=True)
# dry run mode - only say what we would do without actually doing it
@click.option('--dry-run', is_flag=True)
class Cleanup:
    def __init__(self, interval, iterations, nova, cinder, dry_run):
        self.interval = interval
        self.iterations = iterations
        self.novacmdline = nova
        self.cindercmdline = cinder
        self.dry_run = dry_run

        # a dict of all projects we have in openstack
        self.projects = dict()

        # dicts for the ids we have seen and the ones we want to do something with
        self.seen_dict = dict()
        self.to_be_dict = dict()
        # list of servers, snapshots and volumes we have seen or plan to delete
        self.servers_seen = dict()
        self.servers_to_be_deleted = dict()
        self.snapshots_seen = dict()
        self.snapshots_to_be_deleted = dict()
        self.volumes_seen = dict()
        self.volumes_to_be_deleted = dict()

        self.run_me()

    def connection_buildup(self):
        # a dict of all projects we have in openstack
        self.projects = dict()
        # openstack connection
        try:
            self.conn = connection.Connection(auth_url=os.getenv('OS_AUTH_URL'),
                                     project_name=os.getenv('OS_PROJECT_NAME'),
                                     project_domain_name=os.getenv('OS_PROJECT_DOMAIN_NAME'),
                                     username=os.getenv('OS_USERNAME'),
                                     user_domain_name=os.getenv('OS_USER_DOMAIN_NAME'),
                                     password=os.getenv('OS_PASSWORD'),
                                     identity_api_version="3")
        except Exception as e:
            log.warn("- PLEASE CHECK MANUALLY - problems connecting to openstack: %s - retrying in next loop run",
                     str(e))
        else:
            # get all openstack projects
            # no exception handling is done here as it would complicate things and we just
            # successfully created the connection, so that chance is low to fail
            for project in self.conn.identity.projects(details=False, all_tenants=1):
            # this might be required for openstacksdk > 0.9.19
            #for project in self.conn.identity.projects():
                self.projects[project.id] = project.name

        if self.cindercmdline:
            # cinder client session reusing the auth from the openstacksdk connection session
            # this is needed to set the state of volumes and snapshots, which is not yet implemented in the openstacksdk
            auth = self.conn.session.auth
            sess = session.Session(auth=auth)
            self.cinder = client.Client("2.0", session=sess)

    def init_seen_dict(self):
        for i in self.seen_dict:
            self.seen_dict[i] = 0

    # reset dict of all vms or volumes we plan to delete from the db
    def reset_to_be_dict(self):
        for i in self.seen_dict:
            if self.seen_dict[i] == 0:
                self.to_be_dict[i] = 0

    def run_me(self):
        if self.novacmdline or self.cindercmdline:
            while True:
                self.connection_buildup()
                if len(self.projects) > 0:
                    self.os_cleanup_items()
                self.wait_a_moment()
        else:
            log.info("either the --nova or the --cinder flag should be given - giving up!")
            sys.exit(0)

    # main cleanup function
    def os_cleanup_items(self):

        # get all instances from nova sorted by their id
        try:
            self.servers = sorted(self.conn.compute.servers(details=True, all_tenants=1), key=lambda x: x.id)
        except exceptions.HttpException as e:
            log.warn("PLEASE CHECK MANUALLY - got an http exception: %s - retrying in next loop run", str(e))
            return
        except exceptions.SDKException as e:
            log.warn("PLEASE CHECK MANUALLY - got an sdk exception: %s - retrying in next loop run", str(e))
            return
        if self.novacmdline:
            self.seen_dict = self.servers_seen
            self.to_be_dict = self.servers_to_be_deleted
            self.entity = self.servers
            self.check_for_project_id("server")

        if self.cindercmdline:

            # get all snapshots from cinder sorted by their id - do the snapshots before the volumes,
            # as they are created from them and thus should be deleted first
            try:
                self.snapshots = sorted(self.conn.block_store.snapshots(details=True, all_tenants=1), key=lambda x: x.id)
            except exceptions.HttpException as e:
                log.warn("PLEASE CHECK MANUALLY - got an http exception: %s - retrying in next loop run", str(e))
                return
            except exceptions.SDKException as e:
                log.warn("PLEASE CHECK MANUALLY - got an sdk exception: %s - retrying in next loop run", str(e))
                return

            self.snapshot_from = dict()

            # build a dict to check which volume a snapshot was created from quickly
            for i in self.snapshots:
                self.snapshot_from[i.id] = i.volume_id

            self.seen_dict = self.snapshots_seen
            self.to_be_dict = self.snapshots_to_be_deleted
            self.entity = self.snapshots
            self.check_for_project_id("snapshot")

            self.is_server = dict()
            self.attached_to = dict()
            self.volume_project_id = dict()

            # get all volumes from cinder sorted by their id
            try:
                self.volumes = sorted(self.conn.block_store.volumes(details=True, all_tenants=1), key=lambda x: x.id)
            except exceptions.HttpException as e:
                log.warn("PLEASE CHECK MANUALLY - got an http exception: %s - retrying in next loop run", str(e))
                return
            except exceptions.SDKException as e:
                log.warn("PLEASE CHECK MANUALLY - got an sdk exception: %s - retrying in next loop run", str(e))
                return

            # build a dict to check later if a server exists quickly
            for i in self.servers:
                self.is_server[i.id] = i.id

            # build a dict to check which server a volume is possibly attached to quickly
            for i in self.volumes:
                self.volume_project_id[i.id] = i.project_id
                # only record attachments where we have any
                try:
                    self.attached_to[i.attachments[0]["id"]] = i.attachments[0]["server_id"]
                except IndexError:
                    pass

            self.seen_dict = self.volumes_seen
            self.to_be_dict = self.volumes_to_be_deleted
            self.entity = self.volumes
            self.check_for_project_id("volume")


    def wait_a_moment(self):
        # wait the interval time
        log.info("waiting %s minutes before starting the next loop run", str(self.interval))
        time.sleep(60 * int(self.interval))

    def check_for_project_id(self, type):
        self.init_seen_dict()
        for element in self.entity:
            # element has an existing project id - we keep it
            if self.projects.get(element.project_id):
                log.debug("%s %s has a valid project id: %s", type, str(element.id), str(element.project_id))
                pass
            # element has no existing project id - we plan to delete it
            else:
                log.debug("%s %s has no valid project id!", type, str(element.id))
                self.now_or_later(element.id, "delete of " + type)
        # reset the dict of instances we plan to do delete from the db for all machines we did not see or which disappeared
        self.reset_to_be_dict()

    # here we decide to wait longer before doings the delete from the db or finally doing it
    def now_or_later(self, id, what_to_do):
        default = 0
        self.seen_dict[id] = 1
        # if we did not see this more often than iteration times, do or dry-run print what to do - otherwise do not print anything, so that dry-run mode looks like real mode
        if self.to_be_dict.get(id, default) <= int(self.iterations):
            # we have seen it iteration times, time to act
            if self.to_be_dict.get(id, default) == int(self.iterations):
                # ... or print if we are only in dry-run mode
                if self.dry_run:
                    log.info("- dry-run: %s %s", what_to_do, id)
                else:
                    if what_to_do == "delete of server":
                        log.info("- action: %s %s", what_to_do, id)
                        try:
                            self.conn.compute.delete_server(id)
                        except exceptions.HttpException as e:
                            log.warn("- PLEASE CHECK MANUALLY - got an http exception: %s - this has to be handled manually", str(e))
                    elif what_to_do == "delete of snapshot":
                        log.info("- action: %s %s created from volume %s", what_to_do, id,
                                 self.snapshot_from[id])
                        try:
                            self.conn.block_store.delete_snapshot(id)
                        except exceptions.HttpException as e:
                            log.warn("-- got an http exception: %s", str(e))
                            log.info("--- setting the status of the snapshot %s to error in preparation to delete it", id)
                            self.cinder.volume_snapshots.reset_state(id, "error")
                            log.info("--- deleting the snapshot %s", id)
                            try:
                                self.conn.block_store.delete_snapshot(id)
                            except exceptions.HttpException as e:
                                log.warn("PLEASE CHECK MANUALY - got an http exception: %s - this has to be handled manually", str(e))
                    elif what_to_do == "delete of volume":
                        log.info("- action: %s %s", what_to_do, id)
                        try:
                            self.conn.block_store.delete_volume(id)
                        except exceptions.HttpException as e:
                            log.warn("-- got an http exception: %s", str(e))
                            log.warn("--- maybe this volume is still connected to an already deleted instance? - checking ...")
                            if self.attached_to.get(id):
                                log.info("---- volume is still attached to instance: %s", self.attached_to.get(id))
                                if not self.is_server.get(self.attached_to.get(id)):
                                    log.info("---- server %s does no longer exist - the volume can thus be deleted", self.attached_to.get(id))
                                    log.info("---- detaching the volume %s in preparation to delete it", id)
                                    self.cinder.volumes.detach(id)
                                    log.info("---- setting the status of the volume %s to error in preparation to delete it", id)
                                    self.cinder.volumes.reset_state(id, "error")
                                    log.info("---- deleting the volume %s", id)
                                    try:
                                        self.conn.block_store.delete_volume(id)
                                    except exceptions.HttpException as e:
                                        log.warn("PLEASE CHECK MANUALLY - got an http exception: %s - this has to be handled manually", str(e))
                            else:
                                log.info("---- volume is not attached to any instance - must be another problem ...")
                    else:
                        log.warn("- PLEASE CHECK MANUALLY - unsupported action requested for id: %s", id)
            # otherwise print out what we plan to do in the future
            else:
                log.info("- plan: %s %s (%i/%i)", what_to_do, id, self.to_be_dict.get(id, default) + 1, int(self.iterations))
            self.to_be_dict[id] = self.to_be_dict.get(id, default) + 1

if __name__ == '__main__':
    c = Cleanup()
