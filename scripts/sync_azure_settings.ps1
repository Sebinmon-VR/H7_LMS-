<#
.SYNOPSIS
    Signs in to Azure, finds the web app, and pushes .env plus the Firebase service account
    key into its App Service application settings.

.DESCRIPTION
    Nothing secret is committed anywhere. This reads .env and firebase_credentials.json from
    the working copy (both gitignored) and writes them into the App Service configuration,
    where they arrive in the container as ordinary environment variables. Pydantic reads them
    there exactly as it reads .env locally.

    The service account key is sent as FIREBASE_CREDENTIALS_JSON, base64-encoded so the PEM
    body survives every layer between here and the container. The app decodes it at startup
    and writes it to a private temp file that FIREBASE_CREDENTIALS_PATH then points at.

    Sign-in is handled here rather than assumed. The usual failure on this project is not a
    failed login but a successful login to the wrong place: the account has several tenants
    or subscriptions, `az login` selects a default, and the web app lives somewhere else. So
    rather than trusting the default, this searches every enabled subscription for the app by
    name and selects the one that actually holds it.

.PARAMETER DeviceCode
    Sign in with a code entered in a browser on any machine, instead of launching one here.
    Use this when sign-in hangs, opens no window, or lands on a blank page - common over RDP,
    in a container, or with a locked-down default browser.

    Note that the usual "stuck after 'Select the account you want to log in with'" hang is
    handled automatically: the script disables the Windows account-broker dialog for its own
    process before signing in. -DeviceCode is the fallback if a browser still cannot open.

.PARAMETER WhatIf
    Print what would be sent (names only, never values) and exit. Touches nothing and does
    not require being signed in, so it is safe to run first.

.EXAMPLE
    ./scripts/sync_azure_settings.ps1 -WhatIf
    ./scripts/sync_azure_settings.ps1
    ./scripts/sync_azure_settings.ps1 -DeviceCode
    ./scripts/sync_azure_settings.ps1 -Tenant <tenant-id>

.NOTES
    Applying settings restarts the app. The script waits for it to come back and then checks
    /health/firestore, so a run that prints "read_probe: ok" has proved the fix end to end.
#>
param(
    [string]$AppName = "h7-lms-bknd",
    [string]$ResourceGroup,
    [string]$Subscription,
    [string]$Tenant,
    [string]$EnvFile = ".env",
    [string]$CredentialsFile = "firebase_credentials.json",
    [switch]$DeviceCode,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

# Every az call goes through here, for two reasons that bite in Windows PowerShell 5.1:
#
#   - $ErrorActionPreference does not apply to native executables, so a non-zero exit is
#     invisible unless $LASTEXITCODE is checked by hand.
#   - Redirecting a native command's stderr wraps each line in an ErrorRecord, which under
#     'Stop' throws NativeCommandError even when the command merely reported something
#     ordinary. `az account show` on a signed-out machine is exactly that case: it is a
#     question being answered "no", not a failure, and it must not take the script down.
#
# So stderr is suppressed with the preference relaxed, and the exit code is the only verdict.
function Invoke-AzRaw {
    param([string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & az @Arguments 2>$null
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Az {
    param([string[]]$Arguments)

    $result = Invoke-AzRaw $Arguments
    if ($result.ExitCode -ne 0) {
        throw "az $($Arguments -join ' ') failed with exit code $($result.ExitCode)."
    }
    return $result.Output
}

# ------------------------------------------------------------------------------------------
# 1. Local inputs. Done before touching Azure so a typo fails in a second, not after a login.
# ------------------------------------------------------------------------------------------
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($EnvFile)) { $EnvFile = Join-Path $repoRoot $EnvFile }
if (-not [System.IO.Path]::IsPathRooted($CredentialsFile)) {
    $CredentialsFile = Join-Path $repoRoot $CredentialsFile
}

if (-not (Test-Path $EnvFile)) { throw "No env file at $EnvFile" }
if (-not (Test-Path $CredentialsFile)) { throw "No service account key at $CredentialsFile" }

# Settings that must not be copied from a development .env into production as-is.
$devOnlyDefaults = @{
    "DEBUG"                    = "Leaves tracebacks and verbose errors on in production."
    "BOOTSTRAP_ADMIN_PASSWORD" = "The bootstrap admin password is live on the public API."
    "SECRET_KEY"               = "Signs legacy JWTs; a shared development key is forgeable."
}

Write-Step "Reading local configuration"

# Only full-line comments are dropped. A '#' inside a value is part of the value, and
# stripping it would quietly truncate passwords and regexes.
$settings = [ordered]@{}
foreach ($line in Get-Content $EnvFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }

    $split = $trimmed.IndexOf("=")
    if ($split -lt 1) { continue }

    $name = $trimmed.Substring(0, $split).Trim() -replace '^export\s+', ''
    $value = $trimmed.Substring($split + 1).Trim()

    # Strip one matched pair of surrounding quotes; inner quotes belong to the value.
    if ($value.Length -ge 2 -and
        (($value.StartsWith('"') -and $value.EndsWith('"')) -or
         ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    if ($name) { $settings[$name] = $value }
}

$keyJson = Get-Content $CredentialsFile -Raw
try {
    $key = $keyJson | ConvertFrom-Json
} catch {
    throw "$CredentialsFile is not valid JSON: $_"
}
foreach ($field in @("client_email", "private_key", "project_id")) {
    if (-not $key.$field) { throw "$CredentialsFile has no '$field' - not a service account key." }
}

$settings["FIREBASE_CREDENTIALS_JSON"] =
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($keyJson))

# The key file never lands in the deployment artifact, so a path setting copied from .env
# points at nothing. Clearing both makes the app fall through to the env var above.
$settings["FIREBASE_CREDENTIALS_PATH"] = ""
$settings["GOOGLE_APPLICATION_CREDENTIALS"] = ""

if ($settings["GCP_PROJECT_ID"] -and $settings["GCP_PROJECT_ID"] -ne $key.project_id) {
    Write-Warning ("GCP_PROJECT_ID in .env is '{0}' but the key belongs to '{1}'. " +
                   "Firestore reads will be empty unless that is deliberate." -f
                   $settings["GCP_PROJECT_ID"], $key.project_id)
}

Write-Host "  Service account : $($key.client_email)"
Write-Host "  Key project     : $($key.project_id)"
Write-Host "  Settings parsed : $($settings.Count)"
Write-Host ""
Write-Host "  Will set (values hidden):"
foreach ($name in $settings.Keys) {
    $shown = if ($settings[$name]) { "set" } else { "cleared" }
    Write-Host ("    {0,-36} {1}" -f $name, $shown)
}

foreach ($name in $devOnlyDefaults.Keys) {
    if ($settings.Contains($name)) {
        Write-Warning "$name is being copied from .env. $($devOnlyDefaults[$name])"
    }
}

if ($WhatIf) {
    Write-Host ""
    Write-Host "Dry run - nothing sent to Azure. Re-run without -WhatIf to apply."
    return
}

# ------------------------------------------------------------------------------------------
# 2. Sign in.
# ------------------------------------------------------------------------------------------
Write-Step "Checking Azure sign-in"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "The Azure CLI is not on PATH. Install it from https://aka.ms/installazurecliwindows, then reopen this terminal."
}

# `az login` needs a real console for the browser handoff or the device prompt. Under a
# non-interactive host it neither succeeds nor reports why - it just blocks until something
# times out, which is the confusing case.
#
# UserInteractive is not the test: it reports True in automation hosts that have no usable
# console at all. Redirected stdin is what actually predicts the hang.
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

$probe = Invoke-AzRaw @("account", "show", "-o", "json")
$account = if ($probe.ExitCode -eq 0) { $probe.Output } else { $null }

if (-not $account) {
    Write-Host "  Not signed in."

    if (-not $interactive) {
        throw ("This host cannot run an interactive sign-in. Open a normal PowerShell window " +
               "and run 'az login' (or re-run this script there), then try again.")
    }

    # Azure CLI 2.61+ signs in through the Windows Web Account Manager by default. WAM is a
    # native dialog rather than a browser tab, and it routinely opens behind the terminal or
    # never paints at all - the CLI prints "Select the account you want to log in with" and
    # then waits forever on a window nobody can see. That is not a failed login, it is an
    # invisible prompt, and it is the single most common way this script appears to freeze.
    #
    # Set as an environment variable rather than via `az config set`, so the broker is
    # bypassed for this process only and the user's global CLI configuration is left alone.
    $env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS = "false"

    $loginArgs = @("login", "--output", "none")
    if ($Tenant)     { $loginArgs += @("--tenant", $Tenant) }
    if ($DeviceCode) { $loginArgs += "--use-device-code" }

    if ($DeviceCode) {
        Write-Host "  Starting device-code sign-in. Open the URL shown below and enter the code."
    } else {
        Write-Host "  Opening a browser tab to sign in (the Windows account-picker dialog is"
        Write-Host "  bypassed). If nothing appears within a few seconds, press Ctrl+C and"
        Write-Host "  re-run with -DeviceCode."
    }

    & az @loginArgs
    if ($LASTEXITCODE -ne 0) {
        throw ("az login failed (exit $LASTEXITCODE). Re-run with -DeviceCode, which signs in " +
               "with a code instead of a browser handoff. If the account spans several " +
               "directories, add -Tenant <tenant-id>.")
    }

    $probe = Invoke-AzRaw @("account", "show", "-o", "json")
    if ($probe.ExitCode -ne 0) { throw "Signed in, but 'az account show' still fails." }
    $account = $probe.Output
}

$current = ($account | Out-String) | ConvertFrom-Json
Write-Host "  Signed in as : $($current.user.name)"
Write-Host "  Default sub  : $($current.name)"

# ------------------------------------------------------------------------------------------
# 3. Locate the app. The default subscription is a guess; the app's real home is the answer.
# ------------------------------------------------------------------------------------------
Write-Step "Locating web app '$AppName'"

if ($Subscription) {
    Invoke-Az @("account", "set", "--subscription", $Subscription) | Out-Null
    Write-Host "  Using the subscription passed on the command line."
}

function Find-App($subscriptionId) {
    # `az resource list` is a single ARM query. `az webapp list` hydrates every site in the
    # subscription and is far slower when this has to be repeated per subscription.
    $result = Invoke-AzRaw @("resource", "list", "--name", $AppName,
                             "--resource-type", "Microsoft.Web/sites",
                             "--subscription", $subscriptionId,
                             "--query", "[0].resourceGroup", "-o", "tsv")
    if ($result.ExitCode -ne 0) { return $null }

    $found = ($result.Output | Out-String).Trim()
    if ($found) { return $found }
    return $null
}

$targetSub = $null

if ($ResourceGroup) {
    $targetSub = $current.id
    Write-Host "  Using the resource group passed on the command line: $ResourceGroup"
} else {
    $ResourceGroup = Find-App $current.id
    if ($ResourceGroup) {
        $targetSub = $current.id
        Write-Host "  Found in the current subscription."
    } else {
        # This is the mismatch case: signed in successfully, wrong subscription selected.
        Write-Host "  Not in the current subscription. Searching the others..."

        $subsJson = Invoke-Az @("account", "list", "--all", "--query",
                                "[?state=='Enabled'].{id:id,name:name,tenant:tenantId}", "-o", "json")
        $subs = ($subsJson | Out-String) | ConvertFrom-Json

        foreach ($sub in $subs) {
            if ($sub.id -eq $current.id) { continue }
            Write-Host "    checking $($sub.name)"
            $candidate = Find-App $sub.id
            if ($candidate) {
                $ResourceGroup = $candidate
                $targetSub = $sub.id
                Write-Host "  Found in subscription '$($sub.name)' (tenant $($sub.tenant))." -ForegroundColor Green
                break
            }
        }
    }
}

if (-not $ResourceGroup) {
    throw ("No web app named '$AppName' in any enabled subscription this account can see. " +
           "Either the app is in a different Azure directory - sign out with 'az logout' and " +
           "re-run with -Tenant <tenant-id> - or pass -Subscription and -ResourceGroup directly.")
}

if ($targetSub -and $targetSub -ne $current.id) {
    Invoke-Az @("account", "set", "--subscription", $targetSub) | Out-Null
}

Write-Host "  Resource group : $ResourceGroup"

# ------------------------------------------------------------------------------------------
# 4. Apply. Settings go in a JSON file rather than as KEY=VALUE arguments, because values
#    here contain '=', '"', and base64 padding that argument parsing mangles silently.
# ------------------------------------------------------------------------------------------
Write-Step "Applying settings to $AppName (the app restarts)"

$payload = @(foreach ($name in $settings.Keys) {
    [pscustomobject]@{ name = $name; value = $settings[$name]; slotSetting = $false }
})

# The file holds the private key, so it is locked down to the current user and removed even
# if the CLI call throws.
$tempFile = Join-Path ([System.IO.Path]::GetTempPath()) "azure-settings-$(New-Guid).json"
try {
    $payload | ConvertTo-Json -Depth 3 | Out-File -FilePath $tempFile -Encoding utf8

    $acl = Get-Acl $tempFile
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        "$env:USERDOMAIN\$env:USERNAME", "FullControl", "Allow")))
    Set-Acl -Path $tempFile -AclObject $acl

    Invoke-Az @("webapp", "config", "appsettings", "set",
                "--resource-group", $ResourceGroup,
                "--name", $AppName,
                "--settings", "@$tempFile",
                "--output", "none") | Out-Null
} finally {
    if (Test-Path $tempFile) { Remove-Item $tempFile -Force }
}

Write-Host "  Applied $($settings.Count) settings." -ForegroundColor Green

# ------------------------------------------------------------------------------------------
# 5. Prove it worked, rather than reporting success for having sent the request.
# ------------------------------------------------------------------------------------------
Write-Step "Verifying /health/firestore"

$hostName = Invoke-Az @("webapp", "show", "--resource-group", $ResourceGroup, "--name", $AppName,
                        "--query", "defaultHostName", "-o", "tsv")
$hostName = "$hostName".Trim()
$healthUrl = "https://$hostName/health/firestore"

Write-Host "  $healthUrl"
Write-Host "  Waiting for the app to restart..."

$health = $null
foreach ($attempt in 1..10) {
    Start-Sleep -Seconds 15
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 60
        break
    } catch {
        Write-Host "    attempt $attempt - not ready yet"
    }
}

Write-Host ""
if (-not $health) {
    Write-Warning "The app did not answer in time. Check the log stream:"
    Write-Host "  az webapp log tail -g $ResourceGroup -n $AppName"
    return
}

$health | Format-List

if ($health.firestore_available -and $health.read_probe -eq "ok") {
    Write-Host "Firestore is reachable and a live read succeeded." -ForegroundColor Green
    Write-Host "If the UI is still empty, the cause is downstream: CORS, auth, or an empty database."
} else {
    Write-Warning "Firestore is still not reachable. The fields above name the credential source and the actual read error."
    Write-Host ""
    Write-Host "Most likely: the deployed code predates FIREBASE_CREDENTIALS_JSON support, so it"
    Write-Host "ignores the setting entirely (pydantic is configured with extra='ignore')."
    Write-Host "Confirm 'credentials_json_env_set' above is true. If it is false, commit and push"
    Write-Host "the app/core/config.py change and let the GitHub Actions deploy finish, then re-run."
}
