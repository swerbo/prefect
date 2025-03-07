from time import sleep
import uuid

import requests
from requests import RequestException

from prefect import Task
from prefect.engine.signals import FAIL
from prefect.utilities.tasks import defaults_from_attrs


class ConnectionNotFoundException(Exception):
    pass


class AirbyteServerNotHealthyException(Exception):
    pass


class JobNotFoundException(Exception):
    pass


class AirbyteConnectionTask(Task):
    """
    Task for triggering Airbyte Connections, where "A connection is
    a configuration for syncing data between a source and a destination."
    For more information refer to the
    [Airbyte docs](https://docs.airbyte.io/understanding-airbyte/connections)

    This task assumes that the Airbyte Open-Source, since "For
    Airbyte Open-Source you don't need the API Token for
    Authentication! All endpoints are possible to access using the
    API without it."
    For more information refer to the [Airbyte docs](https://docs.airbyte.io/api-documentation)

    Args:
        - airbyte_server_host (str, optional): Hostname of Airbyte server where connection is configured.
            Defaults to localhost.
        - airbyte_server_port (str, optional): Port that the Airbyte server is listening on.
            Defaults to 8000.
        - airbyte_api_version (str, optional): Version of Airbyte API to use to trigger connection sync.
            Defaults to v1.
        - connection_id (str, optional): Default connection id to
            use for sync jobs, if none is specified to `run`.
        - **kwargs (Any, optional): additional kwargs to pass to the
            base Task constructor
    """

    # Connection statuses
    CONNECTION_STATUS_ACTIVE = "active"
    CONNECTION_STATUS_INACTIVE = "inactive"
    CONNECTION_STATUS_DEPRECATED = "deprecated"

    # Job statuses
    JOB_STATUS_SUCCEEDED = "succeeded"
    JOB_STATUS_FAILED = "failed"
    JOB_STATUS_PENDING = "pending"

    def __init__(
        self,
        airbyte_server_host: str = "localhost",
        airbyte_server_port: int = 8000,
        airbyte_api_version: str = "v1",
        connection_id: str = None,
        **kwargs,
    ):
        self.airbyte_server_host = airbyte_server_host
        self.airbyte_server_port = airbyte_server_port
        self.airbyte_api_version = airbyte_api_version
        self.connection_id = connection_id
        super().__init__(**kwargs)

    def _check_health_status(self, session, airbyte_base_url):
        get_connection_url = airbyte_base_url + "/health/"
        try:
            response = session.get(get_connection_url)
            self.logger.info(response.json())
            key = "available" if "available" in response.json() else "db"
            health_status = response.json()[key]
            if not health_status:
                raise AirbyteServerNotHealthyException(
                    f"Airbyte Server health status: {health_status}"
                )
            return True
        except RequestException as e:
            raise AirbyteServerNotHealthyException(e)

    def _get_connection_status(self, session, airbyte_base_url, connection_id):
        get_connection_url = airbyte_base_url + "/connections/get/"

        # TODO - Missing authentication because Airbyte servers currently do not support authentication
        try:
            response = session.post(
                get_connection_url, json={"connectionId": connection_id}
            )
            self.logger.info(response.json())

            response.raise_for_status()

            # check whether a schedule exists ...
            schedule = response.json()["schedule"]
            if schedule:
                self.logger.warning("Found existing Connection schedule, removing ...")

                # mandatory fields for Connection update ...
                sync_catalog = response.json()["syncCatalog"]
                connection_status = response.json()["status"]

                update_connection_url = airbyte_base_url + "/connections" "/update/"
                response2 = session.post(
                    update_connection_url,
                    json={
                        "connectionId": connection_id,
                        "syncCatalog": sync_catalog,
                        "schedule": None,
                        "status": connection_status,
                    },
                )
                self.logger.info(response2.json())

                if response2.status_code == 200:
                    self.logger.info("Schedule removed ok.")
                else:
                    self.logger.warning("Schedule not removed.")

            connection_status = response.json()["status"]
            return connection_status
        except RequestException as e:
            raise AirbyteServerNotHealthyException(e)

    def _trigger_manual_sync_connection(self, session, airbyte_base_url, connection_id):
        """
        Trigger a manual sync of the Connection, see:
        https://airbyte-public-api-docs.s3.us-east-2.amazonaws.com/rapidoc
        -api-docs.html#post-/v1/connections/sync

        Args:
            session: requests session with which to make call to Airbyte server
            airbyte_base_url: URL of Airbyte server
            connection_id: ID of connection to sync

        Returns: created_at - timestamp of sync job creation

        """
        get_connection_url = airbyte_base_url + "/connections/sync/"

        # TODO - missing authentication ...
        try:
            response = session.post(
                get_connection_url, json={"connectionId": connection_id}
            )
            if response.status_code == 200:
                self.logger.info(response.json())
                job_id = response.json()["job"]["id"]
                job_created_at = response.json()["job"]["createdAt"]
                return job_id, job_created_at
            elif response.status_code == 404:
                # connection_id not found
                self.logger.warn(
                    f"Connection {connection_id} not found, please double "
                    f"check the connection_id ..."
                )
                raise ConnectionNotFoundException(
                    f"Connection {connection_id} not found, please double "
                    f"check the connection_id ..."
                )
        except RequestException as e:
            raise AirbyteServerNotHealthyException(e)

    def _get_job_status(self, session, airbyte_base_url, job_id):
        get_connection_url = airbyte_base_url + "/jobs/get/"
        try:
            response = session.post(get_connection_url, json={"id": job_id})
            if response.status_code == 200:
                self.logger.info(response.json())
                job_status = response.json()["job"]["status"]
                job_created_at = response.json()["job"]["createdAt"]
                job_updated_at = response.json()["job"]["updatedAt"]
                return job_status, job_created_at, job_updated_at
            elif response.status_code == 404:
                self.logger.error(f"Job {job_id} not found...")
                raise JobNotFoundException(f"Job {job_id} not found...")
        except RequestException as e:
            raise AirbyteServerNotHealthyException(e)

    @defaults_from_attrs(
        "airbyte_server_host",
        "airbyte_server_port",
        "airbyte_api_version",
        "connection_id",
    )
    def run(
        self,
        airbyte_server_host: str = None,
        airbyte_server_port: int = None,
        airbyte_api_version: str = None,
        connection_id: str = None,
        poll_interval_s: int = 15,
    ) -> dict:
        """
        Task run method for triggering an Airbyte Connection.

        *It is assumed that the user will have previously configured
        a Source & Destination into a Connection.*
        e.g. MySql -> CSV

        An invocation of `run` will attempt to start a sync job for
        the specified `connection_id` representing the Connection in
        Airbyte.

        `run` will poll Airbyte Server for the Connection status and
        will only complete when the sync has completed or
        when it receives an error status code from an API call.

        Args:
            - airbyte_server_host (str, optional): Hostname of Airbyte server where connection is
                configured. Will overwrite the value provided at init if provided.
            - airbyte_server_port (str, optional): Port that the Airbyte server is listening on.
                Will overwrite the value provided at init if provided.
            - airbyte_api_version (str, optional): Version of Airbyte API to use to trigger connection
                sync. Will overwrite the value provided at init if provided.
            - connection_id (str, optional): if provided,
                will overwrite the value provided at init.
            - poll_interval_s (int, optional): this task polls the
                Airbyte API for status, if provided this value will
                override the default polling time of 15 seconds.

        Returns:
            - dict: connection_id (str) and succeeded_at (timestamp str)
        """
        if not connection_id:
            raise ValueError(
                "Value for parameter `connection_id` *must* \
            be provided."
            )

        try:
            uuid.UUID(connection_id)
        except (TypeError, ValueError):
            raise ValueError(
                "Parameter `connection_id` *must* be a valid UUID \
                i.e. 32 hex characters, including hyphens."
            )

        # see https://airbyte-public-api-docs.s3.us-east-2.amazonaws.com
        # /rapidoc-api-docs.html#overview
        airbyte_base_url = (
            f"http://{airbyte_server_host}:"
            f"{airbyte_server_port}/api/{airbyte_api_version}"
        )

        session = requests.Session()
        self._check_health_status(session, airbyte_base_url)
        self.logger.info(
            f"Getting Airbyte Connection {connection_id}, poll interval "
            f"{poll_interval_s} seconds, airbyte_base_url {airbyte_base_url}"
        )

        connection_status = self._get_connection_status(
            session, airbyte_base_url, connection_id
        )
        if connection_status == self.CONNECTION_STATUS_ACTIVE:
            # Trigger manual sync on the Connection ...
            job_id, job_created_at = self._trigger_manual_sync_connection(
                session, airbyte_base_url, connection_id
            )

            job_status = self.JOB_STATUS_PENDING

            while job_status not in [self.JOB_STATUS_FAILED, self.JOB_STATUS_SUCCEEDED]:
                job_status, job_created_at, job_updated_at = self._get_job_status(
                    session, airbyte_base_url, job_id
                )

                # pending┃running┃incomplete┃failed┃succeeded┃cancelled
                if job_status == self.JOB_STATUS_SUCCEEDED:
                    self.logger.info(f"Job {job_id} succeeded.")
                elif job_status == self.JOB_STATUS_FAILED:
                    self.logger.error(f"Job {job_id} failed.")
                else:
                    # wait for next poll interval
                    sleep(poll_interval_s)

            return {
                "connection_id": connection_id,
                "status": connection_status,
                "job_status": job_status,
                "job_created_at": job_created_at,
                "job_updated_at": job_updated_at,
            }
        elif connection_status == self.CONNECTION_STATUS_INACTIVE:
            self.logger.error(
                f"Please enable the Connection {connection_id} in Airbyte Server."
            )
            raise FAIL(
                f"Please enable the Connection {connection_id} in Airbyte Server."
            )
        elif connection_status == self.CONNECTION_STATUS_DEPRECATED:
            self.logger.error(f"Connection {connection_id} is deprecated.")
            raise FAIL(f"Connection {connection_id} is deprecated.")
