# -*- coding: utf-8 -*-
"""
Sync bridge to perform synchronization of tasks between two independent projects
(like, between Todoist and GitHub, for example)
"""
import hashlib
import json
import datetime
from email.utils import parsedate
from logging import getLogger
from collections import namedtuple
from django.utils.encoding import force_bytes, force_text
from .models import ItemMapping


logger = getLogger(__name__)


TASK_FIELDS = ['checked', 'content', 'date_string', 'due_date', 'in_history',
               'indent', 'item_order', 'priority', 'tags']


class SyncBridge(object):
    """
    Every bridge has two adapters: "left" and "right".
    """

    def __init__(self, integration, left, right, name=None):
        if name is None:
            name = '%s-%s' % (left.name, right.name)
        self.integration = integration
        self.left = left
        self.right = right
        self.adapters = [self.left, self.right]
        self.name = name
        for adapter in self.adapters:
            adapter.set_bridge(self)

    def push_task(self, source, task_id, data):
        """
        Pass through the task across the bridge. Source has to be
        the SyncAdapter object either "left" or "right"

        Task id has to be a local unique id of the task in the system backed by
        the source adapter. The task has to be a `Task` object.

        Data has to be any object of any nature. It will be later on passed
        (along with optional stored extra data) to `task_from_data` adapter
        method, which will return a Task instance.

        The function has to be called whenever a user creates or updates a
        task.
        """
        source, target, source_side, target_side = self.find_direction(source)

        # find the mapping
        kw = {'%s_id' % source_side: task_id}  # i.e. left_id: 15
        try:
            mapping = ItemMapping.objects.bridge_get(self, **kw)
        except ItemMapping.DoesNotExist:
            mapping = ItemMapping.objects.bridge_create(self, **kw)
        # make a task out of data
        source_extra = getattr(mapping, '%s_extra' % source_side)
        target_extra = getattr(mapping, '%s_extra' % target_side)
        task = source.task_from_data(data, source_extra)
        if task is None:
            logger.debug('%s refuses to send %r (extra: %r)', source, data, source_extra)
            return

        # calculate hashes
        source_hash = get_hash(task, source.ESSENTIAL_FIELDS)
        target_hash = get_hash(task, target.ESSENTIAL_FIELDS)

        logger.debug('push task %s (%s) %s -> %s', task_id, source_hash,
                     source, target)
        logger.debug('... task: %s', task)
        logger.debug('... extra: %s', source_extra)

        mapping_source_hash = getattr(mapping, '%s_hash' % source_side)
        mapping_target_hash = getattr(mapping, '%s_hash' % target_side)
        if mapping_source_hash == source_hash or mapping_target_hash == target_hash:
            logger.debug('... hashes match, skip')
            return

        target_id = getattr(mapping, '%s_id' % target_side)  # -> mapping.right_id, for example

        # push the task and get a new local id and extra data back
        new_target_id, new_target_extra = target.push_task(target_id, task, target_extra)

        # save the updated version of the id and extra information
        setattr(mapping, '%s_id' % target_side, force_text(new_target_id))
        setattr(mapping, '%s_extra' % target_side, new_target_extra)
        # save hashes
        setattr(mapping, '%s_hash' % source_side, source_hash)
        setattr(mapping, '%s_hash' % target_side, target_hash)
        mapping.save()

    def delete_task(self, source, task_id):
        """
        Delete a task from another side of the bridge

        The source has to be the SyncAdapter object. Task id has to be
        a local unique id of the task in the system backed by the source
        adapter.

        The function has to be called whenever a user deletes a task
        """
        source, target, source_side, target_side = self.find_direction(source)

        # find the mapping
        kw = {'%s_id' % source_side: task_id}  # i.e. left_id: 15
        try:
            mapping = ItemMapping.objects.bridge_get(self, **kw)
        except ItemMapping.DoesNotExist:
            # the task is not known to the bridge, ignore it
            return

        # push the delete command
        target_id = getattr(mapping, '%s_id' % target_side)  # -> mapping.right_id, for example
        target_extra = getattr(mapping, '%s_extra' % target_side)
        if target_id is None:
            return

        target.delete_task(target_id, target_extra)

        # clean up all traces of the object
        mapping.delete()

    def find_direction(self, source):
        if source == self.left:
            return self.left, self.right, 'left', 'right'
        elif source == self.right:
            return self.right, self.left, 'right', 'left'
        raise RuntimeError("Unknown source %r" % source)

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<%s(%s)>' % (self.__class__.__name__, self)


class SyncAdapter(object):
    """
    Abstract `bridge adapter` accepting and executing commands
    """
    DEFAULT_NAME = 'default'
    ESSENTIAL_FIELDS = TASK_FIELDS

    def __init__(self, name=None):
        self.name = name or self.DEFAULT_NAME
        self.bridge = None

    def push_task(self, task_id, task, extra):
        """
        Add task to the storage
        """
        raise NotImplementedError("Has to be implemented in a subclass")

    def delete_task(self, task_id, extra):
        """
        Delete task from the storage
        """
        raise NotImplementedError("Has to be implemented in a subclass")

    def task_from_data(self, data, extra):
        """
        Convert "raw application data" and optional extra information to
        a generic task objects. If None is returned, then the task doesn't have
        to be forwarded at all.
        """
        raise NotImplementedError("Has to be implemented in a subclass")

    def set_bridge(self, bridge):
        if self.bridge and bridge != self.bridge:
            raise RuntimeError('Unable to set the bridge for the adapter')
        self.bridge = bridge

    @property
    def user(self):
        """:rtype: powerapp.core.models.User"""
        return self.bridge.integration.user

    @property
    def api(self):
        """:rtype: powerapp.core.sync.StatelessTodoistAPI"""
        return self.bridge.integration.api

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<%s(%s)>' % (self.__class__.__name__, self)


# A "more or less generic" task object. Used as a container to move tasks
# around. Actually, it's based on a copy-paste of a Todoist task, bit in
# your integration you don't always have to use all these fields. Just use
# ones that make sense for your particular case

Task = namedtuple('Task', TASK_FIELDS)


def task(checked=False, content='', date_string=None, due_date=None,
         in_history=False, indent=1, item_order=0, priority=1, tags=None):
    """
    A helper function to create tasks
    """
    # for the sake of unification (we need to have equal hashes),
    # make sure we have same types for empty values
    checked = bool(checked)
    content = content or ''
    date_string = date_string or None
    due_date = normalize_due_date(due_date)
    in_history = bool(in_history)
    indent = int(indent)
    item_order = int(item_order)
    priority = int(priority)
    tags = tags or []

    # create a task object itself
    return Task(checked, content, date_string, due_date, in_history,
                indent, item_order, priority, tags)


def normalize_due_date(due_date):
    """
    Make an attempt to parse a due date: convert it from the string or integer
    to datetime object
    """
    if not due_date:
        return None

    if isinstance(due_date, str):
        parsed_due_date = parsedate(due_date)
        if parsed_due_date:
            return datetime.datetime(*parsed_due_date[:6])
    elif isinstance(due_date, int):
        return datetime.datetime.fromtimestamp(due_date)

    if not isinstance(due_date, datetime.datetime):
        raise RuntimeError('Due date has to be None or a datetime object')

    return due_date


def get_hash(obj, essential_fields):
    subset = {k: v for k, v in obj._asdict().items() if k in essential_fields}
    str_obj = json.dumps(subset, separators=',:', sort_keys=True, default=json_default)
    return hashlib.sha256(force_bytes(str_obj)).hexdigest()


def json_default(obj):
    if isinstance(obj, datetime.date):
        return obj.strftime('%Y-%m-%d')
    elif isinstance(obj, datetime.datetime):
        return obj.strftime('%Y-%m-%dT%TZ')
    raise TypeError('%r is not JSON serializable' % obj)
