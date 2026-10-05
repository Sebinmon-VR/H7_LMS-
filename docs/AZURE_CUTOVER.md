# Cutover: Google → Azure

The school's Google Workspace has expired. Meet, Calendar, Drive and Gmail already stopped
working; Firebase Auth (a separate Google Cloud project) was still answering on 2026-09-30.
The code in this branch replaces every Google service:

| Was (Google)                        | Now (Azure)                                                  |
|-------------------------------------|--------------------------------------------------------------|
| Firebase Auth (passwords, sessions) | Backend-owned sign-in: hashes in `user_credentials` (Azure SQL), tokens signed with `SECRET_KEY` |
| Google Meet + Calendar              | Azure Communication Services rooms, joined inside the LMS at `/call/<kind>/<id>` |
| Meet recording → Drive              | ACS Call Recording → Event Grid → Blob storage               |
| Google Drive / Cloud Storage        | Azure Blob storage, served through `/files/<blob>` (short-lived signed links) |
| Gmail SMTP                          | ACS Email (or any SMTP relay)                                |
| Firestore                           | Azure SQL (already live since 2026-09-24)                    |

## Already done

- **Passwords imported (2026-09-30).** `scripts/import_firebase_passwords.py --apply` copied all
  26 Firebase password hashes (the other 45 accounts never had a password) into
  `h7lms.user_credentials`, with Firebase's hash parameters. Everybody keeps their password;
  each hash is upgraded to a native one the first time its owner signs in. Nothing about the
  running site changed - the current backend ignores that table.

## 1. Azure resources (one-off)

Resource group `h7` already holds the App Service `h7-lms-bknd`.

1. **Storage - a private container of H7's own.** The `.env` currently points at the
   `vakatel` account's `uploads` container, which belongs to another project and allows
   anonymous reads. Create a dedicated account (or at least a dedicated container) with
   public access off:

   ```bash
   az storage account create -g h7 -n h7lmsfiles --location centralindia --sku Standard_LRS \
     --allow-blob-public-access false --min-tls-version TLS1_2
   az storage account show-connection-string -g h7 -n h7lmsfiles -o tsv   # → AZURE_STORAGE_CONNECTION_STRING
   ```
   The container (`AZURE_STORAGE_CONTAINER`, default `lms-files`) is created by the app.

2. **Communication Services - live classes and email.**

   ```bash
   az communication create -g h7 -n h7-lms-acs --location global --data-location UAE
   az communication list-key -g h7 -n h7-lms-acs --query primaryConnectionString -o tsv   # → ACS_CONNECTION_STRING

   # Email: the Azure-managed domain works at once (DoNotReply@<guid>.azurecomm.net).
   az communication email create -g h7 -n h7-lms-email --location global --data-location UAE
   az communication email domain create -g h7 --email-service-name h7-lms-email \
     -n AzureManagedDomain --location global --domain-management AzureManaged
   # Connect the domain to the Communication resource (portal: Communication resource ->
   # Email -> Domains -> Connect domain), then read its MailFrom address → ACS_EMAIL_SENDER.
   ```

3. **Event Grid - finished recordings.** Pick a long random `ACS_EVENTS_KEY`, deploy the
   backend (step 3) so the webhook exists, then:

   ```bash
   az eventgrid event-subscription create -n h7-lms-recordings \
     --source-resource-id $(az communication show -g h7 -n h7-lms-acs --query id -o tsv) \
     --endpoint "https://<api-host>/api/v1/calls/events?key=<ACS_EVENTS_KEY>" \
     --endpoint-type webhook \
     --included-event-types Microsoft.Communication.RecordingFileStatusUpdated
   ```
   The validation handshake is answered by the backend automatically.

## 2. App Service settings (`h7-lms-bknd`)

Add or change:

| Setting | Value |
|---|---|
| `SECRET_KEY` | **A new long random value** (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). The current one is the repository default. |
| `LMS_APP_URL` | The frontend origin, e.g. `https://<static-web-app-host>` |
| `ACS_CONNECTION_STRING` | From step 1.2 |
| `ACS_EMAIL_SENDER` | From step 1.2 |
| `ACS_EVENTS_KEY` | From step 1.3 |
| `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER` | From step 1.1 |
| `STORAGE_PROVIDER` | `AZURE_BLOB` |
| `DATABASE_BACKEND` | `azuresql` (already) |
| `SCHOOL_TIMEZONE` | `Asia/Kolkata` - only a fallback now (School Settings decides), but it still says Asia/Dubai there |

Remove (nothing reads them any more): `ALLOW_LEGACY_JWT_LOGIN`, `AUTH_PROVIDER`,
`ENABLE_GOOGLE_MEET`, `FIREBASE_CREDENTIALS_JSON`, `FIREBASE_CREDENTIALS_PATH`,
`GOOGLE_APPLICATION_CREDENTIALS`, `GCP_BUCKET_NAME`, `GCP_PROJECT_ID`, `USE_FIREBASE_DB`,
`DATABASE_URL`, `GOOGLE_CALENDAR_ID`, `GOOGLE_CALENDAR_IMPERSONATION`, `GOOGLE_CALENDAR_SCOPES`,
`GOOGLE_DRIVE_*`, `GOOGLE_IMPERSONATION_FALLBACK`, and the Gmail `SMTP_*` settings once ACS
Email works. Keep `GOOGLE_WORKSPACE_DOMAIN` / `USER_EMAIL_DOMAIN` (the domain for generated
logins) and `GOOGLE_CALENDAR_TIMEZONE` only if `SCHOOL_TIMEZONE` is unset. Leave
`FIREBASE_WEB_API_KEY` empty - the import made it unnecessary.

`scripts/sync_azure_settings.ps1` pushes `.env` and clears the Google key settings.

## 3. Deploy - backend and frontend together

The new backend no longer accepts Firebase tokens and the new frontend no longer produces
them, so deploy both in one short window (backend first). Everybody signs in again once,
with the password they already have.

The frontend build needs more memory than before (the calling UI); the workflow now sets
`NODE_OPTIONS=--max-old-space-size=6144`. The GitHub variables `VITE_FIREBASE_*` and
`VITE_GOOGLE_WORKSPACE_DOMAIN` can be deleted.

## 4. Data, after the deploy

```bash
python -m scripts.migrate_google_links                       # report
python -m scripts.migrate_google_links --apply --mark-lost   # re-point generated Meet links
```

- 1 class room, 3 sessions with their own Meet, 3 class-room sessions are moved to LMS rooms.
- 9 session links and 20 tuition links were **pasted by hand** and are left alone - they may
  be personal Gmail Meets that still work. `--convert-manual-meet` moves them too.
- 1 study material was on Google Drive and is gone; `--mark-lost` labels it. Re-upload it.
- 4 study materials are on the App Service disk (`/uploads/...`). Run
  `python -m scripts.migrate_google_links --apply --upload-local` **on the App Service**
  (SSH) to copy them into Blob storage.

## 5. Check

- `GET /health/database` → `available: true`.
- Admin → Integrations: Storage, Live classes, Recording and Email all green.
- Sign in as a teacher and a student; join a class from the dashboard (camera and
  microphone prompt, then the call); the office's live board shows both as in.
- Record a short test: the teacher's join starts it, leaving stops it, and the video appears
  on the session a few minutes later.
- "Forgot password?" on the login page delivers an email.

## Rollback

Redeploy the previous commits of both repositories. Firebase still holds every password
while the Google Cloud project lives, and the `user_credentials` table is ignored by the old
code. Meet links will not come back - the Workspace is gone.
