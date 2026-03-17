"""
Run SQL migrations against Supabase PostgreSQL using the service role key.

Usage:
    python scripts/run_migration.py migrations/001_initial_schema.sql
"""

import sys
import os
import re

from dotenv import load_dotenv
import psycopg2

load_dotenv()


def get_connection_string() -> str:
    """Build a direct Postgres connection string from SUPABASE_URL."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not service_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
        sys.exit(1)

    # Extract project ref from URL: https://<ref>.supabase.co
    match = re.match(r"https://([^.]+)\.supabase\.co", supabase_url)
    if not match:
        print(f"ERROR: Cannot parse project ref from SUPABASE_URL: {supabase_url}")
        sys.exit(1)

    project_ref = match.group(1)

    # Supabase direct connection uses the pooler on port 5432
    # Password is the service role key's corresponding database password,
    # but for transaction-mode pooling we use the project ref + password.
    # The standard Supabase connection string format:
    host = f"db.{project_ref}.supabase.co"

    # NOTE: Supabase DB password is NOT the service role key.
    # You must set DATABASE_URL in .env if the default password doesn't work.
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    print("ERROR: DATABASE_URL not found in .env")
    print()
    print("Add your Supabase database connection string to .env:")
    print(f"  DATABASE_URL=postgresql://postgres.[project-ref]:[db-password]@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres")
    print()
    print("Find it in: Supabase Dashboard → Settings → Database → Connection string (URI)")
    sys.exit(1)


def split_statements(sql: str) -> list[str]:
    """
    Split a SQL file into individual statements.
    Handles $$ dollar-quoted blocks (functions/triggers).
    """
    statements = []
    current = []
    in_dollar_quote = False

    for line in sql.split("\n"):
        stripped = line.strip()

        # Skip empty lines and comments at statement boundaries
        if not in_dollar_quote and not current and (not stripped or stripped.startswith("--")):
            continue

        current.append(line)

        # Track $$ blocks
        dollar_count = line.count("$$")
        if dollar_count % 2 == 1:
            in_dollar_quote = not in_dollar_quote

        # Statement ends with ; outside of $$ blocks
        if not in_dollar_quote and stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt and not stmt.startswith("--"):
                statements.append(stmt)
            current = []

    # Catch any trailing statement without semicolon
    if current:
        stmt = "\n".join(current).strip()
        if stmt and not stmt.startswith("--"):
            statements.append(stmt)

    return statements


def run_migration(filepath: str) -> None:
    """Execute a SQL migration file statement by statement."""
    if not os.path.exists(filepath):
        print(f"ERROR: Migration file not found: {filepath}")
        sys.exit(1)

    conn_string = get_connection_string()

    with open(filepath, "r") as f:
        sql = f.read()

    statements = split_statements(sql)
    print(f"Parsed {len(statements)} statements from {filepath}\n")

    conn = psycopg2.connect(conn_string)
    conn.autocommit = False
    cursor = conn.cursor()

    success = 0
    failed = 0

    for i, stmt in enumerate(statements, 1):
        # Extract a short label from the statement
        first_line = stmt.split("\n")[0][:80]
        try:
            cursor.execute(stmt)
            success += 1
            print(f"  [{i}/{len(statements)}] OK: {first_line}")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(statements)}] FAIL: {first_line}")
            print(f"    Error: {e}")
            conn.rollback()
            print("\nMigration ROLLED BACK due to error.")
            cursor.close()
            conn.close()
            sys.exit(1)

    conn.commit()
    cursor.close()
    conn.close()

    print(f"\nMigration complete: {success} succeeded, {failed} failed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_migration.py <migration_file.sql>")
        sys.exit(1)

    run_migration(sys.argv[1])
