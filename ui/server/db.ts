import pg from 'pg';
import { DATABASE_URL } from './config';

/**
 * A small pool on the console's SELECT-only role (docs/observability.md section 4), with every
 * session forced read-only on top. Used by the Desk's goal bar; LiveSource keeps its own pool.
 * Returns null when ui/.env.local names no database.
 */
export function readOnlyPool(applicationName: string): pg.Pool | null {
  if (!DATABASE_URL) return null;
  const pool = new pg.Pool({
    connectionString: DATABASE_URL,
    max: 1,
    application_name: applicationName,
    options: '-c default_transaction_read_only=on -c TimeZone=UTC -c statement_timeout=5000',
    connectionTimeoutMillis: 4000,
  });
  pool.on('error', () => undefined); // idle-client errors surface on the next query
  return pool;
}
