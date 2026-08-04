# eagm.ps1 — the Makefile's tasks, for Windows PowerShell.
#
#   .\eagm.ps1                     list the tasks
#   .\eagm.ps1 build
#   .\eagm.ps1 dashboard
#   .\eagm.ps1 recon https://shop.everythingautoglass.com
#   .\eagm.ps1 cli plan --limit 100
#
# Settings live in .env, which docker compose reads by itself — so nothing here
# needs the `VAR=value command` prefix that only works in a POSIX shell.
#
# If PowerShell refuses to run this, it is the execution policy, not the script:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

param(
    [Parameter(Position = 0)]
    [string]$Task = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    & docker compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-Eagm {
    param([string[]]$EagmArgs)
    Invoke-Compose (@("run", "--rm", "--entrypoint", "eagm", "migrator") + $EagmArgs)
}

function Initialize-Env {
    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example - edit it with your real details."
    }
}

function Get-DashboardPort {
    if ($env:EAGM_DASHBOARD_PORT) { return $env:EAGM_DASHBOARD_PORT }
    if (Test-Path ".env") {
        $line = Select-String -Path ".env" -Pattern '^\s*EAGM_DASHBOARD_PORT\s*=\s*(\S+)' |
                Select-Object -First 1
        if ($line) { return $line.Matches[0].Groups[1].Value }
    }
    return "19080"
}

function Require-Argument {
    param([string]$What)
    if (-not $Rest -or $Rest.Count -eq 0) {
        Write-Host "This task needs $What." -ForegroundColor Yellow
        exit 1
    }
}

switch ($Task) {

    "help" {
        Write-Host ""
        Write-Host "  Setup"
        Write-Host "    setup            create .env from .env.example"
        Write-Host "    build            build the image (includes Chromium)"
        Write-Host "    build-slim       build without Chromium (database-only)"
        Write-Host "    build-browser    rebuild with Chromium and restart the dashboard"
        Write-Host ""
        Write-Host "  Dashboard"
        Write-Host "    dashboard        start it, then open the URL it prints"
        Write-Host "    dashboard-logs   tail its logs"
        Write-Host "    dashboard-stop   stop it"
        Write-Host ""
        Write-Host "  Reading v2 over HTTP"
        Write-Host "    recon <url>      what is this app? is it behind a login?"
        Write-Host "    login <url>      store a session (set EAGM_COOKIE in .env first)"
        Write-Host "    capture <url>    record the API it calls"
        Write-Host "    draft-html <url> read a list screen's HTML into selectors"
        Write-Host "    harvest          pull it into state/staging.sqlite"
        Write-Host "    staging          show what was harvested"
        Write-Host ""
        Write-Host "  Migrating"
        Write-Host "    doctor           check both database connections"
        Write-Host "    discover         introspect both schemas"
        Write-Host "    scaffold         draft a mapping"
        Write-Host "    plan             dry run - writes nothing"
        Write-Host "    migrate          run it for real"
        Write-Host "    verify           check what landed"
        Write-Host "    runs             list previous runs"
        Write-Host ""
        Write-Host "  Anything else"
        Write-Host "    cli <args...>    run any eagm command"
        Write-Host "    shell            a prompt inside the container"
        Write-Host "    test             run the test suite in the container"
        Write-Host "    up / down        start / stop the containers"
        Write-Host "    logs             tail all container logs"
        Write-Host "    clean            stop everything and delete the volumes"
        Write-Host ""
    }

    "setup"         { Initialize-Env }
    "build"         { Initialize-Env; Invoke-Compose @("build") }
    "build-slim"    { Initialize-Env; $env:WITH_BROWSER = "false"; Invoke-Compose @("build") }

    "build-browser" {
        Initialize-Env
        $env:WITH_BROWSER = "true"
        Invoke-Compose @("build")
        # The running container keeps the old image otherwise, so the rebuild
        # would look like it had not worked.
        Invoke-Compose @("up", "-d", "--force-recreate", "dashboard")
    }

    "dashboard" {
        Initialize-Env
        Invoke-Compose @("up", "-d", "dashboard")
        Write-Host ("Dashboard: http://127.0.0.1:" + (Get-DashboardPort))
    }
    "dashboard-logs" { Invoke-Compose @("logs", "-f", "dashboard") }
    "dashboard-stop" { Invoke-Compose @("stop", "dashboard") }

    "recon"   { Require-Argument "a URL"; Invoke-Eagm (@("recon") + $Rest) }
    "capture"    { Require-Argument "a URL"; Invoke-Eagm (@("capture") + $Rest) }
    "draft-html" { Require-Argument "a URL"; Invoke-Eagm (@("draft-html") + $Rest) }
    "login" {
        Require-Argument "a URL"
        Invoke-Compose (@("run", "--rm", "-e", "EAGM_COOKIE", "-e", "EAGM_AUTH_TOKEN",
                          "--entrypoint", "eagm", "migrator", "login") + $Rest)
    }

    "harvest"  { Invoke-Eagm @("harvest") }
    "staging"  { Invoke-Eagm @("staging") }
    "doctor"   { Invoke-Eagm @("doctor") }
    "discover" { Invoke-Eagm @("discover", "--side", "both") }
    "scaffold" { Invoke-Eagm @("scaffold") }
    "plan"     { Invoke-Eagm @("plan") }
    "migrate"  { Invoke-Eagm @("run", "--yes") }
    "verify"   { Invoke-Eagm @("verify") }
    "runs"     { Invoke-Eagm @("runs") }

    "cli"      { Require-Argument "a command"; Invoke-Eagm $Rest }
    "shell"    { Invoke-Compose @("run", "--rm", "--entrypoint", "bash", "migrator") }

    "up"       { Initialize-Env; Invoke-Compose @("up", "-d") }
    "down"     { Invoke-Compose @("down") }
    "logs"     { Invoke-Compose @("logs", "-f") }
    "clean"    { Invoke-Compose @("down", "-v") }
    "test"     {
        Invoke-Compose @("run", "--rm", "--entrypoint", "sh", "migrator",
                         "-c", "pip install -q pytest && python -m pytest -q")
    }

    default {
        Write-Host "Unknown task '$Task'. Run .\eagm.ps1 for the list." -ForegroundColor Yellow
        exit 1
    }
}
