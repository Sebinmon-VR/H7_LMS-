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

exec gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WORKERS}" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout "${TIMEOUT}" \
    --access-logfile '-' \
    --error-logfile '-'
