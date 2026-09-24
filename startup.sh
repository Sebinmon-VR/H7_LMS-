#!/usr/bin/env bash
#
# Azure App Service startup command.
#
# Without this, Oryx guesses. Its guess for a Python app is `gunicorn app:app`, which fails
# here twice over: `app` is a package whose __init__.py exports nothing, and FastAPI is ASGI
# while gunicorn's default worker is WSGI. The container then exits and the site serves
# "Application Error" even though the build and deployment both reported success.
#
# Point the App Service startup command at this file:
#   az webapp config set -g <rg> -n <app> --startup-file "bash /home/site/wwwroot/startup.sh"
set -euo pipefail

# One worker by default, deliberately.
#
# The reference cache, the background job registry, and the class-reminder scheduler all
# live in process memory. A second worker means a second scheduler, and every student gets
# their reminder email twice. Raise this only once those move to a shared store.
WORKERS="${GUNICORN_WORKERS:-1}"

# App Service terminates an idle request at 230s. Gunicorn's 30s default is well under the
# tail of a cold Firestore round trip, so it would recycle workers mid-request.
TIMEOUT="${GUNICORN_TIMEOUT:-600}"

# The Azure SQL backend needs Microsoft's ODBC driver, which the App Service Python image
# may or may not ship. Install it on first boot when it is missing, from Microsoft's Debian
# repository; skipped in a second when it is already there. A failed install is logged and
# the app still starts - the driver fallback in app/core/sqldb.py picks whatever exists.
if [ "${DATABASE_BACKEND:-firestore}" = "azuresql" ] && command -v apt-get >/dev/null 2>&1; then
    if ! (odbcinst -q -d 2>/dev/null | grep -q "ODBC Driver 1[78] for SQL Server"); then
        echo "startup: no Microsoft ODBC driver for SQL Server found; installing msodbcsql18"
        (
            set +e
            export DEBIAN_FRONTEND=noninteractive ACCEPT_EULA=Y
            . /etc/os-release
            curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
                -o /etc/apt/trusted.gpg.d/microsoft.asc
            echo "deb [arch=amd64] https://packages.microsoft.com/debian/${VERSION_ID}/prod ${VERSION_CODENAME} main" \
                > /etc/apt/sources.list.d/mssql-release.list
            apt-get update -qq && apt-get install -y -qq unixodbc msodbcsql18
            if [ $? -ne 0 ]; then
                echo "startup: ODBC driver install failed; SQL connections will fail until it is fixed"
            fi
        )
    fi
fi

exec gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WORKERS}" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout "${TIMEOUT}" \
    --access-logfile '-' \
    --error-logfile '-'
