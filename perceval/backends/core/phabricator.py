# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2017 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, 51 Franklin Street, Fifth Floor, Boston, MA 02110-1335, USA.
#
# Authors:
#     Santiago Dueñas <sduenas@bitergia.com>
#

import json
import logging

from grimoirelab.toolkit.datetime import datetime_to_utc

from ...backend import (Backend,
                        BackendCommand,
                        BackendCommandArgumentParser)
from ...client import HttpClient
from ...errors import BaseError
from ...utils import DEFAULT_DATETIME

CATEGORY_TASK = "task"

logger = logging.getLogger(__name__)


class Phabricator(Backend):
    """Phabricator backend.

    This class allows to fetch the tasks stored on a Phabricator
    server. Initialize this class passing the URL of this server
    and the API token. The origin of the data will be set to this
    URL.

    :param url: URL of the server
    :param api_token: token needed to use the API
    :param tag: label used to mark the data
    :param archive: archive to store/retrieve items
    """
    version = '0.9.1'

    CATEGORIES = [CATEGORY_TASK]

    def __init__(self, url, api_token, tag=None, archive=None):
        origin = url

        super().__init__(origin, tag=tag, archive=archive)
        self.url = url
        self.api_token = api_token
        self.client = None

        self._users = {}
        self._projects = {}

    def fetch(self, category=CATEGORY_TASK, from_date=DEFAULT_DATETIME):
        """Fetch the tasks from the server.

        This method fetches the tasks stored on the server that were
        updated since the given date. The transactions data related
        to each task is also included within them.

        :param category: the category of items to fetch
        :param from_date: obtain tasks updated since this date

        :returns: a generator of tasks
        """
        if not from_date:
            from_date = DEFAULT_DATETIME

        kwargs = {'from_date': from_date}
        items = super().fetch(category, **kwargs)

        return items

    def fetch_items(self, **kwargs):
        """Fetch the tasks"""

        from_date = kwargs['from_date']

        logger.info("Fetching tasks of '%s' from %s", self.url, str(from_date))

        ntasks = 0

        for task in self.__fetch_tasks(from_date):
            yield task
            ntasks += 1

        logger.info("Fetch process completed: %s tasks fetched", ntasks)

    @classmethod
    def has_archiving(cls):
        """Returns whether it supports archiving items on the fetch process.

        :returns: this backend supports items archive
        """
        return True

    @classmethod
    def has_resuming(cls):
        """Returns whether it supports to resume the fetch process.

        :returns: this backend supports items resuming
        """
        return True

    @staticmethod
    def metadata_id(item):
        """Extracts the identifier from a Phabricator item."""

        return str(item['id'])

    @staticmethod
    def metadata_updated_on(item):
        """Extracts and coverts the update time from a Phabricator item.

        The timestamp is extracted from 'dateModified' field. This date is
        in UNIX timestamp format but needs to be converted to a float
        number.

        :param item: item generated by the backend

        :returns: a UNIX timestamp
        """
        return float(item['fields']['dateModified'])

    @staticmethod
    def metadata_category(item):
        """Extracts the category from a Phabricator item.

        This backend only generates one type of item which is
        'task'.
        """
        return CATEGORY_TASK

    @staticmethod
    def parse_tasks(raw_json):
        """Parse a Phabricator tasks JSON stream.

        The method parses a JSON stream and returns a list iterator.
        Each item is a dictionary that contains the task parsed data.

        :param raw_json: JSON string to parse

        :returns: a generator of parsed tasks
        """
        results = json.loads(raw_json)

        tasks = results['result']['data']
        for t in tasks:
            yield t

    @staticmethod
    def parse_tasks_transactions(raw_json):
        """Parse a Phabricator tasks transactions JSON stream.

        The method parses a JSON stream and returns a dictionary
        with the parsed transactions.

        :param raw_json: JSON string to parse

        :returns: a dict with the parsed transactions
        """
        results = json.loads(raw_json)
        return results['result']

    @staticmethod
    def parse_users(raw_json):
        """Parse a Phabricator users JSON stream.

        The method parses a JSON stream and returns a list iterator.
        Each item is a dictionary that contais the user parsed data.

        :param raw_json: JSON string to parse

        :returns: a generator of parsed users
        """
        results = json.loads(raw_json)

        users = results['result']
        for u in users:
            yield u

    @staticmethod
    def parse_phids(results):
        """Parse a Phabicator PHIDs JSON stream.

        This method parses a JSON stream and returns a list iterator.
        Each item is a dictionary that contains the PHID parsed data.

        :param results: JSON to parse

        :returns: a generator of parsed PHIDs
        """

        for phid in results['result'].values():
            yield phid

    def _init_client(self, from_archive=False):
        """Init client"""

        return ConduitClient(self.url, self.api_token, self.archive, from_archive)

    def __fetch_tasks(self, from_date):
        for raw_tasks in self.client.tasks(from_date=from_date):

            tasks = [t for t in self.parse_tasks(raw_tasks)]

            if not tasks:
                break

            tasks_ids = [t['id'] for t in tasks]
            tasks_trans = self.__fetch_and_parse_tasks_transactions(*tasks_ids)

            for task in tasks:
                # Task check point

                tid = str(task['id'])
                author_id = task['fields']['authorPHID']
                owner_id = task['fields']['ownerPHID']

                task['fields']['authorData'] = self.__get_or_fetch_user(author_id)

                if owner_id:
                    task['fields']['ownerData'] = self.__get_or_fetch_user(owner_id)

                project_ids = task['attachments']['projects']['projectPHIDs']
                task_projects = [self.__get_or_fetch_project(project_id)
                                 for project_id in project_ids]

                task['transactions'] = tasks_trans[tid]
                task['projects'] = task_projects

                yield task

    def __get_or_fetch_user(self, user_id):
        if user_id in self._users:
            return self._users[user_id]

        logger.debug("User %s not found on client cache; fetching it", user_id)

        if user_id.startswith('PHID-USER-'):
            users = self.__fetch_and_parse_users(user_id)
        else:
            logger.debug("User %s is not a real user. Using PHID API to fetch it",
                         user_id)
            users = self.__fetch_and_parse_phids(user_id)

        if len(users) == 0:
            logger.warning("User %s not found on the server. Setting empty data",
                           user_id)
            user = None
        else:
            user = users[0]

        self._users[user_id] = user
        return user

    def __get_or_fetch_project(self, project_id):
        if project_id in self._projects:
            return self._projects[project_id]

        logger.debug("Project %s not found on client cache; fetching it", project_id)

        phids = self.__fetch_and_parse_phids(project_id)
        project = None
        if phids:
            project = phids[0]

        self._projects[project_id] = project
        return project

    def __fetch_and_parse_tasks_transactions(self, *tasks_ids):
        logger.debug("Fetching and parsing tasks transactions")

        raw_json = self.client.transactions(*tasks_ids)
        tasks_trans = self.parse_tasks_transactions(raw_json)

        for trans in tasks_trans.values():
            for tt in trans:
                author_id = tt['authorPHID']
                author = self.__get_or_fetch_user(author_id)
                tt['authorData'] = author

        return tasks_trans

    def __fetch_and_parse_users(self, *users_ids):
        logger.debug("Fetching and parsing users data")
        raw_json = self.client.users(*users_ids)
        users = self.parse_users(raw_json)
        return [user for user in users]

    def __fetch_and_parse_phids(self, *phids):
        logger.debug("Fetching and parsing phids data")
        raw_phids = self.client.phids(*phids)
        phids = json.loads(raw_phids)

        result = []
        if phids['result']:
            # PHID checkpoint
            result = self.parse_phids(phids)

        return [phid for phid in result]


class PhabricatorCommand(BackendCommand):
    """Class to run Phabricator backend from the command line."""

    BACKEND = Phabricator

    @staticmethod
    def setup_cmd_parser():
        """Returns the Phabricator argument parser."""

        parser = BackendCommandArgumentParser(from_date=True,
                                              token_auth=True,
                                              archive=True)

        # Required arguments
        parser.parser.add_argument('url',
                                   help="URL of the Phabricator server")

        return parser


class ConduitError(BaseError):
    """Raised when an error occurs using Conduit"""

    message = "%(error)s (code: %(code)s)"


class ConduitClient(HttpClient):
    """Conduit API Client.

    Phabricator uses Conduit as the Phabricator REST API.
    This class implements some of its methods to retrieve the
    contents from a Phabricator server.

    :param base_url: URL of the Phabricator server
    :param api_token: token to get access to restricted methods
        of the API
    :param archive: an archive to store/read fetched data
    :param from_archive: it tells whether to write/read the archive
    """
    URL = '%(base)s/api/%(method)s'

    # Methods
    MANIPHEST_TASKS = 'maniphest.search'
    MANIPHEST_TRANSACTIONS = 'maniphest.gettasktransactions'
    PHAB_PHIDS = 'phid.query'
    PHAB_USERS = 'user.query'

    PAFTER = 'after'
    PATTACHMENTS = 'attachments'
    PCONSTRAINTS = 'constraints'
    PHIDS = 'phids'
    PIDS = 'ids'
    PPROJECTS = 'projects'
    PORDER = 'order'
    PMODIFIED_START = 'modifiedStart'

    VOUTDATED = 'outdated'

    def __init__(self, base_url, api_token, archive=None, from_archive=False):
        super().__init__(base_url.rstrip('/'), max_retries=3, extra_status_forcelist=[502, 503],
                         archive=archive, from_archive=from_archive)
        self.api_token = api_token

    def tasks(self, from_date=DEFAULT_DATETIME):
        """Retrieve tasks.

        :param from_date: retrieve tasks that where updated from that date;
            dates are converted epoch time.
        """
        # Convert 'from_date' to epoch timestamp.
        # Zero value (1970-01-01 00:00:00) is not allowed for
        # 'modifiedStart' so it will be set to 1, by default.
        ts = int(datetime_to_utc(from_date).timestamp()) or 1

        consts = {
            self.PMODIFIED_START: ts
        }

        attachments = {
            self. PPROJECTS: True
        }

        params = {
            self.PCONSTRAINTS: consts,
            self.PATTACHMENTS: attachments,
            self.PORDER: self.VOUTDATED,
        }

        while True:
            r = self._call(self.MANIPHEST_TASKS, params)
            yield r
            j = json.loads(r)
            after = j['result']['cursor']['after']
            if not after:
                break
            params[self.PAFTER] = after

    def transactions(self, *phids):
        """Retrieve tasks transactions.

        :param phids: list of tasks identifiers
        """
        params = {
            self.PIDS: phids
        }

        response = self._call(self.MANIPHEST_TRANSACTIONS, params)

        return response

    def users(self, *phids):
        """Retrieve users.

        :params phids: list of users identifiers
        """
        params = {
            self.PHIDS: phids
        }

        response = self._call(self.PHAB_USERS, params)

        return response

    def phids(self, *phids):
        """Retrieve data about PHIDs.

        :params phids: list of PHIDs
        """
        params = {
            self.PHIDS: phids
        }

        response = self._call(self.PHAB_PHIDS, params)

        return response

    def _call(self, method, params):
        """Call a method.

        :param method: method to call
        :param params: dict with the HTTP parameters needed to call
            the given method

        :raises ConduitError: when an error is returned by the server
        """
        url = self.URL % {'base': self.base_url, 'method': method}

        # Conduit and POST parameters
        params['__conduit__'] = {'token': self.api_token}

        data = {
            'params': json.dumps(params),
            'output': 'json',
            '__conduit__': True
        }

        logger.debug("Phabricator Conduit client requests: %s params: %s",
                     method, str(data))

        r = self.fetch(url, payload=data, method=HttpClient.POST, verify=False)

        # Check for possible Conduit API errors
        result = r.json()

        if result['error_code']:
            raise ConduitError(error=result['error_info'],
                               code=result['error_code'])

        return r.text
