# Frontend Note — User Creation & Credential Generation

What changed in the backend and what the frontend needs to build.
Base URL: `/api/v1`. All admin endpoints require `Authorization: Bearer <token>`.

---

## What changed

| Change | Impact on frontend |
|---|---|
| `email` is now **optional** on user creation | Email field can be left blank; backend generates `firstname.lastname@<domain>` |
| `password` is optional and no longer entered by the admin | Remove any password input from the create-user form |
| New endpoint `POST /admin/users/{id}/generate-credentials` | New "Generate Credentials" button on the user list/detail |
| Generated password is returned once in the response | Needs a one-time reveal modal — it cannot be fetched again |
| Credentials are emailed to the user automatically | Show delivery status from `email_sent` |

Nothing else changed. Login, roles, and all other endpoints are unchanged.

---

## Screens to build

### 1. Create User form

`POST /api/v1/admin/users`

**Request**
```json
{
  "full_name": "Anita Fernandes",
  "role": "TEACHER",
  "email": null,
  "password": null
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string | **yes** | Only required field |
| `role` | `"STUDENT"` \| `"TEACHER"` \| `"ADMIN"` | no | Defaults to `STUDENT` |
| `email` | string \| null | no | Omit to auto-generate. Show as an optional "override" field, collapsed by default |
| `password` | string \| null | no | **Do not expose in the UI.** Leave null; password comes from step 2 |

**Response `201`** — `UserOut`
```json
{
  "id": 1754912345678,
  "full_name": "Anita Fernandes",
  "email": "anita.fernandes@h7ed.com",
  "role": "TEACHER",
  "is_active": true,
  "created_at": "2026-08-08T10:22:41.123456",
  "firebase_uid": "xYz..."
}
```

Show the generated email back to the admin — it's the first time they see it.

**Errors**
- `400` — `"User with this email already exists"` (only when email was typed manually)
- `400` — `"full_name is required to generate an email address"`
- `400` — `"No email domain is configured..."` (server config problem, not user error)
- `403` — caller is not an admin

Email generation rules, so the UI can preview it:
`"Anita Rose Fernandes"` → `anita.fernandes@h7ed.com` (middle names dropped, accents stripped, lowercased). On collision a number is appended: `anita.fernandes2@h7ed.com`. Don't compute this client-side as truth — the server decides — but a live hint under the field is fine.

---

### 2. "Generate Credentials" button

Placed on each row of the user list and on the user detail page. This is the action that gives the user a working login.

`POST /api/v1/admin/users/{user_id}/generate-credentials`

**Request** — body optional, send `{}` for the normal case
```json
{ "send_email": true, "revoke_sessions": true }
```

| Field | Default | Notes |
|---|---|---|
| `send_email` | `true` | Set `false` when the user has no mailbox yet — admin relays the password manually |
| `revoke_sessions` | `true` | Signs the user out everywhere. Leave `true` for password resets |

**Response `200`** — `CredentialsIssued`
```json
{
  "user_id": 1754912345678,
  "full_name": "Anita Fernandes",
  "email": "anita.fernandes@h7ed.com",
  "role": "TEACHER",
  "password": "Min6LYrWJ!H!",
  "email_sent": true,
  "detail": "Credentials generated and emailed to anita.fernandes@h7ed.com."
}
```

**Errors**
- `404` — user not found
- `400` — `"This user has no email address..."`
- `503` — Firebase Auth unreachable; nothing was changed, safe to retry
- `403` — caller is not an admin

**Confirmation dialog required.** If the user already has credentials, this resets their password and signs them out. Warn before firing: *"Generate new credentials for Anita Fernandes? Their current password will stop working."*

---

### 3. Credentials reveal modal

Opens on success. This is the only time the password exists in the UI.

- Show `email` and `password` with a **copy** button on each and a copy-all.
- Drive the status line off `email_sent`:
  - `true` → success state, show `detail`.
  - `false` → **warning state, not an error.** The account works; only delivery failed. Show `detail` verbatim — it explains whether SMTP is unconfigured, delivery failed, or email was skipped on purpose. Tell the admin to copy the password now.
- Require an explicit "I've saved this" / Close action. Do not auto-dismiss on timer or outside-click.
- Never log the password to the console, analytics, or error reporting. Don't persist it in client state after the modal closes.

---

### 4. Login

Unchanged, but this is the only login the provisioned users have. Email and password only — no Google button needed for these accounts.

`POST /api/v1/auth/login`

```json
{ "email": "anita.fernandes@h7ed.com", "password": "Min6LYrWJ!H!" }
```

**Response `200`**
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer",
  "role": "TEACHER",
  "user_id": 1754912345678,
  "full_name": "Anita Fernandes"
}
```

Send as `Authorization: Bearer <access_token>` on every subsequent call. Route by `role` after login: `ADMIN` → admin console, `TEACHER` → teacher dashboard, `STUDENT` → student dashboard.

**Errors**
- `401` — `"Incorrect email or password"`
- `403` — `"No LMS profile exists for this account..."` or `"Inactive user account"`
- `501` — password login not configured on the server (`FIREBASE_WEB_API_KEY` unset). Surface as a server config message, not a credentials error.

⚠️ **Token expiry:** `access_token` is a Firebase ID token valid for **1 hour**, and this endpoint returns no refresh token. Handle `401` on any call by redirecting to login. If you want silent refresh, use the Firebase Web SDK for sign-in instead and send its ID token as the bearer — the backend accepts both.

---

## Existing endpoints the admin screens still use

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/users?role=TEACHER` | List users, optional role filter |
| `PUT` | `/admin/users/{id}` | Edit `full_name`, `email`, `is_active` |
| `DELETE` | `/admin/users/{id}` | Deactivate (soft delete, `204`) |
| `POST` | `/admin/users/{id}/reactivate` | Re-enable |
| `GET` | `/auth/me` | Current user profile |

---

## Suggested admin user-list row

```
Anita Fernandes   anita.fernandes@h7ed.com   TEACHER   ● Active   [Generate Credentials] [Edit] [Deactivate]
```

A user created but never issued credentials cannot log in. There is no API flag for this state — if you want a "Credentials pending" badge, track it client-side from whether Generate Credentials has been pressed, or ask backend to expose it.

Generating credentials on an inactive user reactivates them automatically. Reflect that by refetching the row after the call.

---

## Full API reference

Interactive docs with live request/response: `http://<server>/docs`
