# Creates the console's SELECT-only PostgreSQL role (docs/observability.md section 4) and writes
# ui/.env.local with its connection URL. Run once, from anywhere:
#
#   powershell -ExecutionPolicy Bypass -File ui\scripts\create-readonly-role.ps1
#
# - The password is random, is sent to psql on stdin (never on a command line) and is stored only
#   in ui/.env.local, which git ignores. The bot's own .env and its read-write password are untouched.
# - Safe to re-run: it rotates the password. Grants cover tables the bot creates later
#   (ALTER DEFAULT PRIVILEGES), so it can run before the first `trader paper run`.
$ErrorActionPreference = 'Stop'

$ui = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $ui

# Only the port and database name are read from the bot's .env (never the password).
# The console reads the database the paper bot writes: configs/paper.yaml names the variable
# with the bot's database URL (the same rule as scripts/console-db.mjs).
$port = '5433'
$db = 'trader'
$urlEnv = 'TRADER_DATABASE_URL'
$paperYaml = Join-Path $repo 'configs\paper.yaml'
if (Test-Path $paperYaml) {
    foreach ($line in Get-Content $paperYaml) {
        if ($line -match '^\s*url_env:\s*([A-Z][A-Z0-9_]*)') { $urlEnv = $Matches[1]; break }
    }
}
if ($urlEnv -eq 'TRADER_PRACTICE_DATABASE_URL') { $db = 'trader_practice' }
$dotenv = Join-Path $repo '.env'
if (Test-Path $dotenv) {
    foreach ($line in Get-Content $dotenv) {
        if ($line -match '^\s*TRADER_PG_PORT\s*=\s*(\d+)\s*$') { $port = $Matches[1] }
        if ($urlEnv -eq 'TRADER_DATABASE_URL' -and $line -match '^\s*TRADER_PG_DB\s*=\s*([A-Za-z0-9_]+)\s*$') { $db = $Matches[1] }
        if ($line -match ('^\s*' + $urlEnv + '\s*=\s*\S+/([A-Za-z0-9_]+)(\?\S*)?\s*$')) { $db = $Matches[1] }
    }
}

$bytes = New-Object byte[] 24
(New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
$pw = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')

$sql = @'
SELECT 'CREATE ROLE trader_ro LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trader_ro')\gexec
ALTER ROLE trader_ro WITH LOGIN PASSWORD '__PASSWORD__';
ALTER ROLE trader_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE :"db" TO trader_ro;
GRANT USAGE ON SCHEMA public TO trader_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO trader_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT SELECT ON TABLES TO trader_ro;
'@.Replace('__PASSWORD__', $pw)

Push-Location $repo
try {
    $sql | docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -v db="$0" -v owner="$POSTGRES_USER" -U "$POSTGRES_USER" -d "$0"' $db
    if ($LASTEXITCODE -ne 0) { throw "psql failed (exit $LASTEXITCODE). Is the database running? docker compose up -d" }
}
finally {
    Pop-Location
}

$envLocal = Join-Path $ui '.env.local'
$content = "# Local and gitignored. Read-only role for the console (docs/observability.md section 4).`nUI_DATABASE_URL=postgresql://trader_ro:$pw@127.0.0.1:$port/$db`n"
[System.IO.File]::WriteAllText($envLocal, $content, (New-Object System.Text.UTF8Encoding $false))
Write-Host "Created/updated role trader_ro (SELECT only, read-only transactions) and wrote ui/.env.local."
Write-Host "Restart the console (pnpm dev) to pick it up."
