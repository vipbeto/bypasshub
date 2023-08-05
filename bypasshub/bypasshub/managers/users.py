import time
import logging
import sqlite3
import functools
from uuid import uuid4
from re import compile
from io import StringIO
from typing import Self
from pathlib import Path
from shutil import copyfileobj
from operator import itemgetter
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

from .. import errors
from ..config import config
from ..database import Database
from ..utils import current_time, convert_size
from ..type import Credentials, Plan, Traffic, Param, Return

USERNAME_MIN_LENGTH = 1
USERNAME_MAX_LENGTH = 64
username_pattern = compile(r"\w+$")  # Letters and numbers plus underscore
temp_path = Path(config["main"]["temp_path"])
logger = logging.getLogger(__name__)


class Users:
    """The interface to manage the users on the database."""

    _list_generated = None

    def __init__(self) -> None:
        self._closed = None
        self._database = Database()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception) -> None:
        self.close()

    @staticmethod
    def _validate_username(method: Callable[Param, Return]) -> Callable[Param, Return]:
        """Validates the `username` positional parameter of the passed method."""

        @functools.wraps(method)
        def wrapper(
            self: Self, username: str, *args: Param.args, **kwargs: Param.kwargs
        ) -> Return:
            return method(self, self.validate_username(username), *args, **kwargs)

        return wrapper

    @staticmethod
    def _is_unlimited_time_plan(plan: Plan) -> bool:
        """Whether it is an unlimited time plan."""
        return plan["plan_start_date"] is None or plan["plan_duration"] is None

    @staticmethod
    def _is_unlimited_traffic_plan(plan: Plan) -> bool:
        """Whether it is an unlimited traffic plan."""
        return plan["plan_traffic"] is None

    def _is_exist(self, username: str) -> bool:
        """Whether the user is exist in the database."""
        if self._database.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE username = ?) AS exist",
            (username,),
        ).fetchone()["exist"]:
            return True

        return False

    def _update_traffic(
        self,
        username: str,
        traffic_usage: int,
        extra_traffic_usage: int,
        upload: int,
        download: int,
    ) -> None:
        """Appends user's traffic usage by the given values."""
        with self._database:
            self._database.execute(
                """
                UPDATE
                    users
                SET
                    plan_traffic_usage = plan_traffic_usage + ?,
                    plan_extra_traffic_usage = plan_extra_traffic_usage + ?,
                    total_upload = total_upload + ?,
                    total_download = total_download + ?
                WHERE
                    username = ?
                """,
                (
                    traffic_usage,
                    extra_traffic_usage,
                    upload,
                    download,
                    username,
                ),
            )

    @staticmethod
    def validate_username(username: str) -> str:
        """Returns the lowercased version of the passed value after the validation.

        The minimum allowed length is 1 character.
        The maximum allowed length is 64 characters.

        Raises:
            ``errors.InvalidUsernameError``:
                When the username contains illegal characters or length.
        """
        if (
            not USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH
        ) or not username_pattern.match(username):
            raise errors.InvalidUsernameError(username)
        return username.lower()

    def validate_credentials(self, credentials: Credentials) -> bool:
        """Whether the user credentials is valid and exist in the database."""
        if self._database.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE username = ? AND uuid = ?) AS exist",
            (self.validate_username(credentials["username"]), credentials["uuid"]),
        ).fetchone()["exist"]:
            return True

        return False

    @_validate_username
    def is_exist(self, username: str) -> bool:
        """
        Checks whether the user is exist in the
        database after validating the username.
        """
        return self._is_exist(username)

    @_validate_username
    def add_user(self, username: str) -> Credentials:
        """Adds the user to the database.

        Returns:
            The credentials of created user that can be used
            to connect to the services.

        Raises:
            ``errors.UserExistError``:
                When the specified user already exists.

            ``errors.UsersCapacityError``:
                When cannot create the user due to user capacity limit.
                This limit can be configured with `max_users`
                property in the configuration file.

            ``errors.ActiveUsersCapacityError``:
                When cannot create the user due to capacity limit
                of the maximum users that have an active plan is reached.
                This limit can be configured with `max_active_users`
                property in the configuration file.

            ``errors.UUIDOverlapError``:
                When cannot create the user due to overlapped UUIDs.
                It's very unlikely this exception ever rises on collisions.
        """
        max_users = config["main"]["max_users"]
        max_active_users = config["main"]["max_active_users"]
        if max_users > 0 and self.capacity >= max_users:
            raise errors.UsersCapacityError()
        elif max_active_users > 0 and self.active_capacity >= max_active_users:
            raise errors.ActiveUsersCapacityError()

        with self._database:
            for retry in range(3):
                uuid = str(uuid4())
                try:
                    self._database.execute(
                        """
                        INSERT INTO users (username, uuid, user_creation_date)
                        VALUES (?, ?, ?)
                        """,
                        (username, uuid, current_time().isoformat()),
                    )
                except sqlite3.IntegrityError as error:
                    if error.sqlite_errorcode == errors.SQLITE_CONSTRAINT_PRIMARYKEY:
                        raise errors.UserExistError(username)
                    elif error.sqlite_errorcode == errors.SQLITE_CONSTRAINT_UNIQUE:
                        if retry < 2:
                            continue
                        raise errors.UUIDOverlapError()
                    raise

                logger.debug(f"User '{username}' is created in database")
                return {"username": username, "uuid": uuid}

    @_validate_username
    def delete_user(self, username: str) -> None:
        """Deletes the user from the database.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.
        """
        if not self._is_exist(username):
            raise errors.UserNotExistError(username)

        with self._database:
            self._database.execute("DELETE FROM users WHERE username = ?", (username,))

        logger.debug(f"User '{username}' is deleted from the database")

    @_validate_username
    def get_credentials(self, username: str) -> Credentials:
        """Returns the user's credentials.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.
        """
        credentials = self._database.execute(
            "SELECT username, uuid FROM users WHERE username = ?", (username,)
        ).fetchone()

        if not credentials:
            raise errors.UserNotExistError(username)

        return credentials

    @_validate_username
    def get_plan(self, username: str) -> Plan:
        """Returns the user's plan.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.
        """
        plan = self._database.execute(
            """
            SELECT
                plan_start_date,
                plan_duration,
                plan_traffic,
                plan_traffic_usage,
                plan_extra_traffic,
                plan_extra_traffic_usage
            FROM
                users
            WHERE
                username = ?
            """,
            (username,),
        ).fetchone()

        if not plan:
            raise errors.UserNotExistError(username)

        return plan

    @_validate_username
    def set_plan(
        self,
        username: str,
        *,
        id: int | None = None,
        start_date: datetime | str | int | float | None = None,
        duration: timedelta | int | None = None,
        traffic: int | None = None,
        preserve_traffic_usage: bool | None = None,
    ) -> None:
        """Updates the user's plan in the database.

        Args:
            `id`:
                The unique identifier related to this plan update
                that would be stored in the database plan history table.
            `start_date`:
                The plan start date in ISO 8601 format if provided value is `str`
                or UNIX timestamp if provided value is a number.
                If omitted, no time restriction will be applied.
                `ValueError` will be raised if the `duration` parameter is not
                specified.
            `duration`:
                The Plan duration in seconds.
                `ValueError` will be raised if value is not a positive integer
                or the `start_date` parameter is not specified.
            `traffic`:
                The plan traffic limit in bytes.
                If omitted, no time restriction will be applied.
                `ValueError` will be raised if value is not a positive integer.
            `preserve_traffic_usage`:
                If `True` provided, recorded traffic usage will not reset
                and the value from the previous plan will be preserved.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.

            ``errors.DuplicatedPlanIDError``:
                When cannot update the plan due to provided `id` already exists.
        """
        if start_date is not None:
            if (start_date_type := type(start_date)) is not datetime:
                if start_date_type is str:
                    start_date = datetime.fromisoformat(start_date)
                elif start_date_type in (int, float):
                    start_date = datetime.fromtimestamp(start_date, tz=timezone.utc)
            if start_date.tzinfo != timezone.utc:
                start_date = start_date.astimezone(timezone.utc)
            start_date = start_date.replace(microsecond=0).isoformat()

            if duration is None:
                raise ValueError(f"The 'duration' parameter must be specified")
        elif duration is not None:
            raise ValueError(f"The 'start_date' parameter must be specified")

        if type(duration) is int:
            if duration <= 0:
                raise ValueError(f"The 'duration' parameter should be grater than zero")
        elif type(duration) is timedelta:
            duration = int(duration.total_seconds())  # ignoring milliseconds

        if traffic is not None:
            if traffic <= 0:
                raise ValueError("The 'traffic' parameter should be grater than zero")
            preserve_traffic_usage = None if preserve_traffic_usage else 0
        else:
            preserve_traffic_usage = 0

        if not self._is_exist(username):
            raise errors.UserNotExistError(username)

        values = (
            start_date,
            duration,
            traffic,
            preserve_traffic_usage,
            username,
        )

        with self._database:
            try:
                self._database.execute(
                    """
                    INSERT INTO history (
                        id,
                        date,
                        username,
                        plan_start_date,
                        plan_duration,
                        plan_traffic
                    )
                    VALUES
                        (?, ?, ?, ?, ?, ?)
                    """,
                    (id, current_time().isoformat(), username) + values[:-2],
                )
            except sqlite3.IntegrityError as error:
                if error.sqlite_errorcode == errors.SQLITE_CONSTRAINT_UNIQUE:
                    raise errors.DuplicatedPlanIDError(id)
                raise

            self._database.execute(
                """
                UPDATE
                    users
                SET
                    plan_start_date = ?,
                    plan_duration = ?,
                    plan_traffic = ?,
                    plan_traffic_usage = IFNULL(?, plan_traffic_usage),
                    /* flatting the remaining traffic and ignoring the negative values */
                    plan_extra_traffic =
                        MAX(plan_extra_traffic - plan_extra_traffic_usage, 0),
                    plan_extra_traffic_usage = 0
                WHERE
                    username = ?
                """,
                values,
            )

        logger.info(
            f"Plan is updated for user '{username}'"
            f""" {f"starting from '{start_date}'" if start_date else 'with unlimited time'} and"""
            f""" {f"'{convert_size(traffic)}'" if traffic is not None else 'unlimited'} traffic"""
        )

    @_validate_username
    def set_plan_extra_traffic(
        self,
        username: str,
        *,
        id: int | None = None,
        extra_traffic: int | None = None,
    ) -> None:
        """Updates the user's plan extra traffic limit in the database.

        Args:
            `id`:
                The unique identifier related to this plan update
                that would be stored in the database plan history table.
            `extra_traffic`:
                The plan extra traffic limit in bytes.
                If user's plan traffic limit is reached, this value will
                be consumed instead for managing the user's traffic usage.
                `ValueError` will be raised if value is not a positive integer.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.

            ``errors.NoTrafficLimitError``:
                When user's plan has no traffic limit.

            ``errors.DuplicatedPlanIDError``:
                When cannot update the plan due to provided `id` already exists.
        """
        if append := extra_traffic is not None:
            if extra_traffic <= 0:
                raise ValueError(
                    "The 'extra_traffic' parameter should be grater than zero"
                )
            elif self._is_unlimited_traffic_plan(self.get_plan(username)):
                raise errors.NoTrafficLimitError(username)

        if not self._is_exist(username):
            raise errors.UserNotExistError(username)

        with self._database:
            try:
                self._database.execute(
                    """
                    INSERT INTO history (
                        id,
                        date,
                        username,
                        plan_extra_traffic
                    )
                    VALUES
                        (?, ?, ?, ?)
                    """,
                    (id, current_time().isoformat(), username, extra_traffic),
                )
            except sqlite3.IntegrityError as error:
                if error.sqlite_errorcode == errors.SQLITE_CONSTRAINT_UNIQUE:
                    raise errors.DuplicatedPlanIDError(id)
                raise

            self._database.execute(
                f"""
                UPDATE
                    users
                SET
                    /* flatting the remaining traffic and ignoring the negative values */
                    plan_extra_traffic =
                        MAX(IFNULL(plan_extra_traffic + ? - plan_extra_traffic_usage, 0), 0),
                    plan_extra_traffic_usage = 0
                WHERE
                    username = ?
                """,
                (extra_traffic, username),
            )

        logger.info(
            (f"Appended '{convert_size(extra_traffic)}'" if append else f"Reset the")
            + f" plan extra traffic for user '{username}'"
        )

    def has_active_plan(self, username: str, *, plan: Plan | None = None) -> bool:
        """
        Whether the user has an active plan.
        A plan is considered active when still has time and traffic.
        """
        plan = plan or self.get_plan(username)
        (
            plan_start_date,
            plan_duration,
            plan_traffic,
            plan_traffic_usage,
            plan_extra_traffic,
            plan_extra_traffic_usage,
        ) = itemgetter(*plan.keys())(plan)

        has_time = self._is_unlimited_time_plan(plan) or current_time() < (
            # plan due date
            datetime.fromisoformat(plan_start_date)
            + timedelta(seconds=plan_duration)
        )
        has_traffic = (
            self._is_unlimited_traffic_plan(plan)
            or plan_traffic_usage < plan_traffic
            or plan_extra_traffic_usage < plan_extra_traffic
        )

        return True if has_time and has_traffic else False

    def has_unlimited_time_plan(
        self, username: str, *, plan: Plan | None = None
    ) -> bool:
        """Whether user has an unlimited time plan."""
        return self._is_unlimited_time_plan(plan or self.get_plan(username))

    def has_unlimited_traffic_plan(
        self, username: str, *, plan: Plan | None = None
    ) -> bool:
        """Whether user has an unlimited traffic plan."""
        return self._is_unlimited_traffic_plan(plan or self.get_plan(username))

    @_validate_username
    def get_total_traffic(self, username: str) -> Traffic:
        """Returns the user's total traffic consumption in bytes.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.
        """
        traffic = self._database.execute(
            "SELECT total_upload, total_download FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if not traffic:
            raise errors.UserNotExistError(username)

        return {
            "uplink": traffic["total_upload"],
            "downlink": traffic["total_download"],
        }

    @_validate_username
    def reset_total_traffic(self, username: str) -> None:
        """Resets the user's total traffic consumption.

        Raises:
            ``errors.UserNotExistError``:
                When the specified user does not exist.
        """
        if not self._is_exist(username):
            raise errors.UserNotExistError(username)

        with self._database:
            self._database.execute(
                """
                UPDATE
                    users
                SET
                    total_upload = 0,
                    total_download = 0
                WHERE
                    username = ?
                """,
                (username,),
            )

        logger.info(f"The total consumed traffic is reset for user '{username}'")

    def generate_list(self) -> None:
        """
        Generates and stores credentials of all the
        users that have an active plan on the disk.

        The services should read this list and generate
        the users on the boot time.
        """
        last_generate = temp_path.joinpath("last-generate")
        last_generate.write_text("")
        stream = StringIO()
        try:
            for credentials in self._database.execute(
                "SELECT username, uuid FROM users"
            ).fetchall():
                if self.has_active_plan(credentials["username"]):
                    stream.write(f"{credentials['username']} {credentials['uuid']}\n")

            stream.seek(0)
            with open(temp_path.joinpath("users"), "w") as file:
                copyfileobj(stream, file)

            last_generate.write_text(str(int(time.time())))
            logger.debug(
                f"The users list is {'re' if self._list_generated else ''}generated"
            )
        finally:
            stream.close()
            self._list_generated = True

    def close(self) -> None:
        """Closes the database connection."""
        if not self._closed:
            self._database.close()
            self._closed = True

    @property
    def usernames(self) -> list[str]:
        """The list of all the users."""
        return [
            user["username"]
            for user in self._database.execute("SELECT username FROM users").fetchall()
        ]

    @property
    def capacity(self) -> int:
        """The count of all the users."""
        return self._database.execute("SELECT COUNT(*) AS count FROM users").fetchone()[
            "count"
        ]

    @property
    def active_capacity(self) -> int:
        """The count of all the users that have an active plan."""
        count = 0
        for username in self.usernames:
            if self.has_active_plan(username):
                count += 1
        return count
