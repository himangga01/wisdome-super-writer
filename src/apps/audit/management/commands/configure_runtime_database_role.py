import base64
import hashlib
import hmac
import os
import re
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from psycopg import sql


AUDIT_TABLE = "audit_auditevent"
PUBLIC_SCHEMA = "public"
RUNTIME_USERNAME_ENV = "POSTGRES_RUNTIME_USER"
RUNTIME_PASSWORD_ENV = "POSTGRES_RUNTIME_PASSWORD"
SCRAM_ITERATIONS = 4096
SCRAM_SALT_BYTES = 16
SAFE_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
SAFE_URL_PASSWORD = re.compile(r"^[A-Za-z0-9._~-]+$")


class Command(BaseCommand):
    help = (
        "Create or update the restricted PostgreSQL runtime role after migrations "
        "and enforce its application-object privileges."
    )

    def handle(self, *args, **options):
        runtime_username = os.environ.get(RUNTIME_USERNAME_ENV, "").strip()
        runtime_password = os.environ.get(RUNTIME_PASSWORD_ENV, "")
        if not runtime_username:
            raise CommandError(f"{RUNTIME_USERNAME_ENV} must be set")
        if not runtime_password:
            raise CommandError(f"{RUNTIME_PASSWORD_ENV} must be set")
        if SAFE_ROLE_NAME.fullmatch(runtime_username) is None:
            raise CommandError(
                f"{RUNTIME_USERNAME_ENV} must be a URL-safe PostgreSQL role name"
            )
        if SAFE_URL_PASSWORD.fullmatch(runtime_password) is None:
            raise CommandError(
                f"{RUNTIME_PASSWORD_ENV} must contain only URL-safe unreserved characters"
            )

        connection = connections[DEFAULT_DB_ALIAS]
        if connection.vendor != "postgresql":
            raise CommandError(
                "configure_runtime_database_role supports PostgreSQL only"
            )

        try:
            with transaction.atomic(using=DEFAULT_DB_ALIAS):
                self._configure(connection, runtime_username, runtime_password)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                "Could not configure the PostgreSQL runtime role"
            ) from exc

        self.stdout.write(
            self.style.SUCCESS("PostgreSQL runtime role privileges configured")
        )

    def _configure(self, connection, runtime_username, runtime_password):
        runtime_role = sql.Identifier(runtime_username)
        public_schema = sql.Identifier(PUBLIC_SCHEMA)
        audit_table = sql.Identifier(PUBLIC_SCHEMA, AUDIT_TABLE)
        password_secret = sql.Literal(self._build_scram_secret(runtime_password))

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, current_database()")
            owner_username, database_name = cursor.fetchone()
            if runtime_username == owner_username:
                raise CommandError(
                    "The PostgreSQL runtime role must differ from the migration owner"
                )

            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                [runtime_username],
            )
            role_exists = cursor.fetchone()[0]
            if role_exists:
                self._reject_owned_objects(cursor, runtime_username)
                cursor.execute(
                    sql.SQL("ALTER ROLE {} RESET ALL").format(runtime_role)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER ROLE {} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {} "
                        "VALID UNTIL 'infinity' CONNECTION LIMIT -1"
                    ).format(runtime_role, password_secret)
                )
            else:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {} "
                        "VALID UNTIL 'infinity' CONNECTION LIMIT -1"
                    ).format(runtime_role, password_secret)
                )

            self._revoke_role_memberships(cursor, runtime_username, runtime_role)
            self._require_audit_table(cursor)

            owner_role = sql.Identifier(owner_username)
            database = sql.Identifier(database_name)

            cursor.execute(
                sql.SQL(
                    "REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC"
                ).format(database)
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                    database,
                    runtime_role,
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    database,
                    runtime_role,
                )
            )

            cursor.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(
                    public_schema
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
                    public_schema,
                    runtime_role,
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    public_schema,
                    runtime_role,
                )
            )

            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM PUBLIC"
                ).format(public_schema)
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}"
                ).format(public_schema, runtime_role)
            )
            cursor.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    "IN SCHEMA {} TO {}"
                ).format(public_schema, runtime_role)
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {} FROM {}").format(
                    audit_table,
                    runtime_role,
                )
            )
            cursor.execute(
                sql.SQL("GRANT SELECT, INSERT ON TABLE {} TO {}").format(
                    audit_table,
                    runtime_role,
                )
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
                    "ON TABLE {} FROM {}"
                ).format(audit_table, runtime_role)
            )

            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC"
                ).format(public_schema)
            )
            cursor.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}"
                ).format(public_schema, runtime_role)
            )
            cursor.execute(
                sql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
                ).format(public_schema, runtime_role)
            )

            self._configure_default_privileges(
                cursor,
                owner_role=owner_role,
                public_schema=public_schema,
                runtime_role=runtime_role,
            )

    @staticmethod
    def _reject_owned_objects(cursor, runtime_username):
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_shdepend AS dependency
                JOIN pg_roles AS runtime_role
                  ON runtime_role.oid = dependency.refobjid
                JOIN pg_database AS current_db
                  ON current_db.datname = current_database()
                WHERE runtime_role.rolname = %s
                  AND dependency.deptype = 'o'
                  AND dependency.dbid IN (0, current_db.oid)
            )
            """,
            [runtime_username],
        )
        if cursor.fetchone()[0]:
            raise CommandError(
                "The PostgreSQL runtime role owns database objects; "
                "transfer ownership before restricting it"
            )

    @staticmethod
    def _revoke_role_memberships(cursor, runtime_username, runtime_role):
        cursor.execute(
            """
            SELECT granted_role.rolname
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE member_role.rolname = %s
            """,
            [runtime_username],
        )
        for (granted_role_name,) in cursor.fetchall():
            cursor.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(granted_role_name),
                    runtime_role,
                )
            )
        cursor.execute(
            """
            SELECT member_role.rolname
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE granted_role.rolname = %s
            """,
            [runtime_username],
        )
        for (member_role_name,) in cursor.fetchall():
            cursor.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    runtime_role,
                    sql.Identifier(member_role_name),
                )
            )

    @staticmethod
    def _require_audit_table(cursor):
        cursor.execute("SELECT to_regclass(%s)", [f"{PUBLIC_SCHEMA}.{AUDIT_TABLE}"])
        if cursor.fetchone()[0] is None:
            raise CommandError(
                "The audit table is missing; run all migrations before this command"
            )

    @staticmethod
    def _build_scram_secret(password):
        try:
            password_bytes = password.encode("ascii")
        except UnicodeEncodeError as exc:
            raise CommandError(
                f"{RUNTIME_PASSWORD_ENV} must use URL-safe ASCII characters"
            ) from exc

        salt = secrets.token_bytes(SCRAM_SALT_BYTES)
        salted_password = hashlib.pbkdf2_hmac(
            "sha256",
            password_bytes,
            salt,
            SCRAM_ITERATIONS,
        )
        client_key = hmac.new(
            salted_password,
            b"Client Key",
            hashlib.sha256,
        ).digest()
        stored_key = hashlib.sha256(client_key).digest()
        server_key = hmac.new(
            salted_password,
            b"Server Key",
            hashlib.sha256,
        ).digest()

        encoded_salt = base64.b64encode(salt).decode("ascii")
        encoded_stored_key = base64.b64encode(stored_key).decode("ascii")
        encoded_server_key = base64.b64encode(server_key).decode("ascii")
        return (
            f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{encoded_salt}"
            f"${encoded_stored_key}:{encoded_server_key}"
        )

    @staticmethod
    def _configure_default_privileges(
        cursor,
        *,
        owner_role,
        public_schema,
        runtime_role,
    ):
        prefix = sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} "
        ).format(owner_role, public_schema)

        cursor.execute(
            prefix
            + sql.SQL("REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC")
        )
        cursor.execute(
            prefix
            + sql.SQL("REVOKE ALL PRIVILEGES ON TABLES FROM {}").format(
                runtime_role
            )
        )
        cursor.execute(
            prefix
            + sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(runtime_role)
        )
        cursor.execute(
            prefix
            + sql.SQL("REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC")
        )
        cursor.execute(
            prefix
            + sql.SQL("REVOKE ALL PRIVILEGES ON SEQUENCES FROM {}").format(
                runtime_role
            )
        )
        cursor.execute(
            prefix
            + sql.SQL("GRANT USAGE, SELECT ON SEQUENCES TO {}").format(
                runtime_role
            )
        )
