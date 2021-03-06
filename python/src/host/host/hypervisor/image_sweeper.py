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

from enum import Enum
import logging
import threading

from common.lock import locked

from host.hypervisor.task_runner import TaskRunner, TaskAlreadyRunning
from host.hypervisor.image_scanner import InvalidStateTransition

"""
Execute the task of running an image sweep
on a datastore.
"""


class DatastoreImageSweeperTaskRunner(TaskRunner):
    def __init__(self, name, ds_image_sweeper):
        super(DatastoreImageSweeperTaskRunner, self).__init__(name)
        self.logger = logging.getLogger(__name__)
        self._ds_image_sweeper = ds_image_sweeper
        self._image_manager = ds_image_sweeper.image_manager

    # Override
    def execute_task(self):
        try:
            self._ds_image_sweeper.set_state(
                DatastoreImageSweeper.State.IMAGE_SWEEP)
            deleted_images = self._image_manager.\
                delete_unused(self._ds_image_sweeper)
            self._ds_image_sweeper.set_deleted_images(deleted_images)
        except Exception as e:
            self.logger.warning("Exception caught by image "
                                "sweeper thread, %s, ds_id : %s, state: %s, "
                                "target images: %d, deleted images: %d"
                                % (e,
                                   self._ds_image_sweeper.datastore_id,
                                   self._ds_image_sweeper.get_state(),
                                   len(self._ds_image_sweeper.
                                       get_target_images()),
                                   len(self._ds_image_sweeper.
                                       get_deleted_images()))
                                )
            # Re-raise exception so it can be saved in
            # the task_runner
            raise e
        finally:
            self._ds_image_sweeper.set_state(
                DatastoreImageSweeper.State.IDLE)

"""
The purpose of the object in this class is to
remove unused image from a datastore.
The main object is a Sweeper. The Sweeper
can be IDLE when no scanning activity is running.
When the sweeper is activated, it creates a TaskRunner
runner thread which executes the sweep.
The sweep initiator must provide a list of
all the images that are candidates for removal.
"""


class DatastoreImageSweeper:
    DEFAULT_IMAGE_SWEEP_RATE = 10
    DEFAULT_TIMEOUT = 7 * 24 * 60 * 60
    IMAGE_SWEEP_SINGLE_OP_DURATION = 0.200
    IMAGE_SWEEP_GRACE_PERIOD = 60

    """
    The following class represents the state of
    this image sweeper:
    IDLE, no active thread running
    INIT, thread has been created but not running yet
    SWEEP, thread is running a image sweep (step 1)
    """

    class State(Enum):
        IDLE = 0
        INIT = 1
        IMAGE_SWEEP = 2

    def __init__(self, image_manager, datastore_id):
        self.logger = logging.getLogger(__name__)
        self.image_manager = image_manager
        self.datastore_id = datastore_id
        self.image_sweep_rate = DatastoreImageSweeper.DEFAULT_IMAGE_SWEEP_RATE
        self._target_images = list()
        self._deleted_images = list()
        self._state = DatastoreImageSweeper.State.IDLE
        self._timeout = DatastoreImageSweeper.DEFAULT_TIMEOUT
        self._grace_period = DatastoreImageSweeper.IMAGE_SWEEP_GRACE_PERIOD
        self._task_runner = DatastoreImageSweeperTaskRunner(__name__, self)
        self.lock = threading.Lock()

    @locked
    def start(self, target_image_list, timeout=None,
              sweep_rate=None, grace_period=None):
        self.logger.info("IMAGE SCANNER: starting sweeper: %s, %s, %s, %s" %
                         (target_image_list, timeout,
                          sweep_rate, grace_period))

        if self._state != DatastoreImageSweeper.State.IDLE:
            self.logger.info("Image sweeper thread already running: %s"
                             % self._state)
            raise TaskAlreadyRunning
        self._state = DatastoreImageSweeper.State.INIT
        if timeout:
            self._timeout = timeout
        if sweep_rate:
            self.image_sweep_rate = sweep_rate
        if grace_period is not None:
            self._grace_period = grace_period
        # Start task
        self._target_images = target_image_list
        self._task_runner.start(self._timeout)

    @locked
    def set_state(self, state):
        if not self._validate_state_transition(state):
            raise InvalidStateTransition
        self._state = state

    def stop(self):
        self._task_runner.stop()

    def _validate_state_transition(self, state):
        # Idle state can be reached from
        # any other state due to errors,
        # stop and timeout
        if state == DatastoreImageSweeper.State.IDLE:
            return True
        #
        # The normal state transition diagram is:
        # IDLE -> INIT -> IMAGE_SWEEP -> IDLE
        if self._state == DatastoreImageSweeper.State.IDLE:
            return state == DatastoreImageSweeper.State.INIT
        elif self._state == DatastoreImageSweeper.State.INIT:
            return state == DatastoreImageSweeper.State.IMAGE_SWEEP
        elif self._state == DatastoreImageSweeper.State.IMAGE_SWEEP:
            return state == DatastoreImageSweeper.State.IDLE
        else:
            return False

    def get_state(self):
        return self._state

    def is_stopped(self):
        return self._task_runner.is_stopped()

    def wait_for_task_end(self):
        return self._task_runner.wait_for_task_end()

    def get_exception(self):
        return self._task_runner.get_exception()

    def set_target_images(self, target_images):
        self._target_images = target_images

    def get_target_images(self):
        return self._target_images

    def set_deleted_images(self, deleted_images):
        self._deleted_images = deleted_images

    @locked
    def get_deleted_images(self):
        return self._deleted_images, self._task_runner.end_time

    def get_image_sweep_rest_interval(self):
        return (60.0 / self.image_sweep_rate) - \
            self.IMAGE_SWEEP_SINGLE_OP_DURATION

    def get_grace_period(self):
        return self._grace_period
