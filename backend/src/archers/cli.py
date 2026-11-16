import argparse
import getpass
import logging
import os
from pathlib import Path
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from archers.api import check_schema
from archers.config import load_settings
from archers.domain import DomainError, normalize_username
from archers.identity import hash_password
from archers.storage import AuditEntry, Tenant, User, make_engine


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def bootstrap(username: str, company: str, password: str) -> None:
    if not company.strip() or len(company) > 128:
        raise DomainError("VALIDATION_ERROR", "Company name requires 1 to 128 characters.", 422)
    password_hash = hash_password(password)
    name = normalize_username(username)
    settings = load_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        check_schema(engine)
        with Session(engine) as db, db.begin():
            db.execute(select(Tenant).with_for_update())
            # A fixed advisory lock also protects the initial empty installation.
            db.execute(text("SELECT pg_advisory_xact_lock(734818102)"))
            if db.scalar(select(Tenant.id)) is not None:
                raise DomainError("REVISION_CONFLICT", "Installation is already bootstrapped.", 409)
            tenant = Tenant(id=uuid4(), name=company.strip())
            db.add(tenant)
            db.flush()
            admin = User(
                id=uuid4(),
                tenant_id=tenant.id,
                username=name,
                password_hash=password_hash,
                role="admin",
            )
            db.add(admin)
            db.flush()
            db.add(
                AuditEntry(
                    tenant_id=tenant.id,
                    actor_id=admin.id,
                    action="installation.bootstrap",
                    resource_id=tenant.id,
                )
            )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="archers")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply versioned database migrations.")
    bootstrap_parser = sub.add_parser("bootstrap", help="Explicitly provision the first admin.")
    bootstrap_parser.add_argument("--username", required=True)
    bootstrap_parser.add_argument("--company", required=True)
    sub.add_parser("api", help="Serve the administration API on internal port 8000.")
    sub.add_parser("check-config", help="Validate without printing secrets.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        load_settings()
        if args.command == "migrate":
            command.upgrade(migration_config(), "head")
            print("Migrations applied.")
        elif args.command == "bootstrap":
            secret_file = os.environ.get("ARCHERS_BOOTSTRAP_PASSWORD_FILE")
            password = (
                Path(secret_file).read_text().rstrip("\r\n")
                if secret_file
                else getpass.getpass("Initial admin password: ")
            )
            bootstrap(args.username, args.company, password)
            print("Administrator provisioned.")
        elif args.command == "api":
            uvicorn.run(
                "archers.api:create_app",
                factory=True,
                host="0.0.0.0",
                port=8000,
                access_log=False,
                proxy_headers=False,
            )
        else:
            print("Configuration valid; no external model/source egress enabled.")
    except DomainError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except ValidationError:
        parser.exit(1, "Configuration does not meet the deployment contract.\n")
    except (SQLAlchemyError, OSError, RuntimeError):
        parser.exit(1, "Operation failed; check configuration, database and migration state.\n")


if __name__ == "__main__":
    main()
