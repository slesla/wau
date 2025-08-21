import psycopg2
from psycopg2 import pool
from psycopg2.sql import SQL, Identifier
import os
from dotenv import load_dotenv
import argparse
import fileinput
import glob
import re
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

class Constants:
    AIRC_DEV_PATH = '~/airc-dev/desenv'
    DEFAULT_PROJECT = 'sigr'
    DEFAULT_LOGIN = 'admin@airc.pt'
    DEFAULT_SCHEMA = 'ME'
    DEFAULT_USER_ID = '185eda4d-8739-4c10-9211-8b8ceff4086d'
    DEFAULT_E2E_SCHEMA = 'airc-management'
    DEFAULT_E2E_USER_ID = 'a144ef55-f943-44a4-8cc1-ca30697fd3e3'

class DatabaseConfig:
    def __init__(self, project: str = None) -> None:
        self.keycloak_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': os.getenv('DB_PORT', '5432'),
            'database': os.getenv('DB_NAME', 'keycloak'),
            'user': os.getenv('DB_USER', 'postgres'),
            'password': os.getenv('DB_PASSWORD', 'postgres'),
        }
        if project:
            self.project_config = {
                'host': os.getenv('DB_HOST', 'localhost'),
                'port': os.getenv('DB_PORT', '5432'),
                'database': os.getenv('DB_NAME', f'{project}'),
                'user': os.getenv('DB_USER', 'postgres'),
                'password': os.getenv('DB_PASSWORD', 'postgres'),
            }

class PathConfig:
    def __init__(self, project: str) -> None:
        self.base_path = Path(f'{Constants.AIRC_DEV_PATH}/{project}').expanduser()
        self.e2e_db_path = Path(f'{Constants.AIRC_DEV_PATH}/{project}/{project}-frontend/cypress/plugins/resources').expanduser()
        self.cypress_config_local = Path(f'{Constants.AIRC_DEV_PATH}/{project}/{project}-frontend/cypress.config.local.ts').expanduser()

class DatabaseManager:
    def __init__(self, keycloak_config: Dict[str, Any], project_config: Dict[str, Any]):
        self.keycloak_config = keycloak_config
        self.project_config = project_config
        self.keycloak_pool = None
        self.project_pool = None

    def __enter__(self):
        self.keycloak_pool = pool.SimpleConnectionPool(1, 20, **self.keycloak_config)
        self.project_pool = pool.SimpleConnectionPool(1, 20, **self.project_config)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.keycloak_pool:
            self.keycloak_pool.closeall()
        if self.project_pool:
            self.project_pool.closeall()

    def get_user_id(self, email: str, schema: str) -> Optional[str]:
        conn = None
        try:
            conn = self.keycloak_pool.getconn()
            with conn.cursor() as cursor:
                query = SQL("""
                    SELECT id 
                    FROM {}.user_entity 
                    WHERE email_constraint = %s
                    AND realm_id = %s
                """).format(Identifier('public'))
                cursor.execute(query, (email, schema))
                result = cursor.fetchone()
                return result[0] if result else None
        except psycopg2.Error as e:
            logger.error(f"Database error in get_user_id: {e}")
            return None
        finally:
            if conn:
                self.keycloak_pool.putconn(conn)

    def drop_schema(self, schema_name: str) -> bool:
        conn = None
        try:
            conn = self.project_pool.getconn()
            with conn.cursor() as cursor:
                query = SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    Identifier(schema_name)
                )
                cursor.execute(query)
                conn.commit()
                return True
        except psycopg2.Error as e:
            logger.error(f"Database error while dropping schema: {e}")
            return False
        finally:
            if conn:
                self.project_pool.putconn(conn)

    def rename_schema(self, old_name: str, new_name: str) -> bool:
        conn = None
        try:
            conn = self.project_pool.getconn()
            with conn.cursor() as cursor:
                query = SQL("ALTER SCHEMA {} RENAME TO {}").format(
                    Identifier(old_name),
                    Identifier(new_name)
                )
                cursor.execute(query)
                conn.commit()
                return True
        except psycopg2.Error as e:
            logger.error(f"Database error while renaming schema: {e}")
            return False
        finally:
            if conn:
                self.project_pool.putconn(conn)

class FileManager:
    @staticmethod
    def find_files(directory: Path) -> List[Path]:
        return list(directory.rglob("*.sql"))

    @staticmethod
    def replace_in_file(file_path: Path, old_text: str, new_text: str) -> None:
        with fileinput.FileInput(str(file_path), inplace=True) as file:
            for line in file:
                print(line.replace(old_text, new_text), end='')

    @staticmethod
    def update_cypress_config(config_file: Path, project: str, schema: str, reverse: bool = False) -> None:
        try:
            with open(config_file, 'r') as file:
                content = file.read()

            pattern = r'//\s*(config\.env\.' + project.upper() + '_URL\s*=.*?)(\n)(config\.env\.' + project.upper() + '_URL\s*=.*?)(\n)'
            if reverse:
                content = re.sub(pattern, r'\1\2', content)
            else:
                content = re.sub(
                    r'(config\.env\.' + project.upper() + '_URL\s*=.*?)(\n)',
                    r'// \1\2' + f"config.env.{project.upper()}_URL = 'http://localhost/{project}/#/{schema}';\n",
                    content
                )

            with open(config_file, 'w') as file:
                file.write(content)
        except Exception as e:
            logger.error(f"Error updating Cypress config: {e}")

def validate_schema_name(schema_name: str) -> bool:
    return schema_name.isalnum()

def confirm_replacement(e2e_db_path: Path, e2e_schema: str, my_schema: str, e2e_user: str, user_id: str) -> bool:
    logger.info(f"In {e2e_db_path}")
    logger.info(f"Replacing schema: {e2e_schema} for {my_schema}")
    logger.info(f"Replacing user ID: {e2e_user} for {user_id}")
    consent = input("Do you want to proceed with the replacements? (y/N): ")
    return consent.lower() == 'y'

def main():
    parser = argparse.ArgumentParser(description='e2e to local conversion utility')
    parser.add_argument('-schema', help='Schema name to use', default=Constants.DEFAULT_SCHEMA)
    parser.add_argument('-project', help='Project name to use', default=Constants.DEFAULT_PROJECT)
    parser.add_argument('-login', help='Login to use', default=Constants.DEFAULT_LOGIN)
    parser.add_argument('-e2euser', help='User id to replace', default=Constants.DEFAULT_E2E_USER_ID)
    parser.add_argument('-e2eschema', help='Schema to replace', default=Constants.DEFAULT_E2E_SCHEMA)
    parser.add_argument('-getid', help='Get user id', action='store_true')
    parser.add_argument('-reverse', help='Reverse operation', action='store_true')

    args = parser.parse_args()

    if not validate_schema_name(args.schema):
        logger.error("Invalid schema name. Schema name must be alphanumeric.")
        sys.exit(1)

    paths = PathConfig(args.project)
    db_config = DatabaseConfig(args.project)

    with DatabaseManager(db_config.keycloak_config, db_config.project_config) as db:
        if args.getid:
            user_id = db.get_user_id(args.login, args.schema)
            if user_id:
                logger.info(f"User ID: {user_id}")
            else:
                logger.error(f"No user found with email '{args.login}'")
                sys.exit(1)
        else:
            user_id = Constants.DEFAULT_USER_ID

        my_schema = args.schema
        tmp_schema = f"{my_schema}-tmp"
        e2e_schema = args.e2eschema
        e2e_user = args.e2euser

        if args.reverse:
            e2e_schema, my_schema = my_schema, e2e_schema
            e2e_user, user_id = user_id, e2e_user
            logger.info("Reversing replacements...")

            if not confirm_replacement(paths.e2e_db_path, e2e_schema, my_schema, e2e_user, user_id):
                logger.info("Operation cancelled by user.")
                sys.exit(0)

            if not db.drop_schema(e2e_schema):
                logger.error(f"Failed to drop schema {e2e_schema}")
                sys.exit(1)

            if not db.rename_schema(tmp_schema, e2e_schema):
                logger.error(f"Failed to rename schema {tmp_schema} to {e2e_schema}")
                sys.exit(1)
        else:
            if not confirm_replacement(paths.e2e_db_path, e2e_schema, my_schema, e2e_user, user_id):
                logger.info("Operation cancelled by user.")
                sys.exit(0)

            if not db.rename_schema(my_schema, tmp_schema):
                logger.error(f"Failed to rename schema {my_schema}")
                sys.exit(1)

        file_manager = FileManager()
        FileManager.update_cypress_config(paths.cypress_config_local, args.project, my_schema, args.reverse)

        for file_path in file_manager.find_files(paths.e2e_db_path):
            # logger.info(f"Processing file: {file_path}")
            file_manager.replace_in_file(file_path, f"'{e2e_schema}'", f"'{my_schema}'")
            file_manager.replace_in_file(file_path, e2e_user, user_id)

        logger.info("Operation completed successfully\nPlease restart the backend service to apply changes.")

if __name__ == "__main__":
    main()

    # @Me TODO Replace email login in sql files
    # @Me TODO History of changes