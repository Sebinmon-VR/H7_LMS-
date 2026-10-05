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

Sign-in moved off Firebase Auth on 2026-09-30: the backend now holds the password hashes itself and issues its own session tokens. The login request and response shapes are unchanged; token lifetime, renewal and the new password endpoints are described under [Login](#4-login).

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
  "created_at": "2026-08-08T10:22:41.123456"
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
- `503` — the database is unreachable; nothing was changed, safe to retry
- `403` — caller is not an admin

**Confirmation dialog required.** If the user already has credentials, this resets their password and signs them out. Warn before firing: *"Generate new credentials for Anita Fernandes? Their current password will stop working."*

---

### 3. Credentials reveal modal

Opens on success. This is the only time the password exists in the UI.

- Show `email` and `password` with a **copy** button on each and a copy-all.
- Drive the status line off `email_sent`:
  - `true` → success state, show `detail`.
  - `false` → **warning state, not an error.** The account works; only delivery failed. Show `detail` verbatim — it explains whether email (Azure Communication Services Email, or SMTP) is unconfigured, delivery failed, or email was skipped on purpose. Tell the admin to copy the password now.
- Require an explicit "I've saved this" / Close action. Do not auto-dismiss on timer or outside-click.
- Never log the password to the console, analytics, or error reporting. Don't persist it in client state after the modal closes.

---

### 4. Login

This is the only login there is. Email and password only — there is no Google (or any other) sign-in button. The backend checks the password against the hash it stores and returns a session token it signs itself; accounts moved over from Firebase Auth keep their existing passwords.

`POST /api/v1/auth/login`

```json
{ "email": "anita.fernandes@h7ed.com", "password": "Min6LYrWJ!H!" }
```

**Response `200`**
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer",
  "expires_in": 86400,
  "role": "TEACHER",
  "user_id": 1754912345678,
  "full_name": "Anita Fernandes"
}
```

Send as `Authorization: Bearer <access_token>` on every subsequent call. Route by `role` after login: `ADMIN` → admin console, `TEACHER` → teacher dashboard, `STUDENT` → student dashboard.

**Errors**
- `401` — `"Incorrect email or password"`
- `403` — `"Inactive user account"`
- `429` — too many failed attempts for this address (`LOGIN_MAX_FAILURES` within `LOGIN_LOCKOUT_MINUTES`, 10 in 15 minutes by default). Show the `detail`; it suggests waiting or resetting the password.

⚠️ **Token expiry and renewal:** `access_token` is a session token signed by the backend, valid for `expires_in` seconds (`ACCESS_TOKEN_EXPIRE_MINUTES`, 24 hours by default). Renew it before it runs out with `POST /api/v1/auth/refresh` (bearer = the current token), which returns the same `Token` shape. A session can be renewed for up to 30 days after the password was last typed; after that `/auth/refresh` answers `401` and the user signs in again. Handle `401` on any other call by redirecting to login — it also means the session was revoked (password changed or reset, account deactivated, or credentials regenerated with `revoke_sessions`).

**Password endpoints**

| Method | Path | Body | Notes |
|---|---|---|---|
| `POST` | `/auth/password/change` | `{ "current_password", "new_password" }` | Signed-in user. Signs out every other device; the response carries a fresh `token` for this one — replace the stored token with it |
| `POST` | `/auth/password/reset-request` | `{ "email" }` | Public. Always answers the same way, whether or not the address has an account. Emails a link to `<LMS_APP_URL>/reset-password?token=...` |
| `POST` | `/auth/password/reset` | `{ "token", "new_password" }` | Public. The page at `/reset-password` posts the token from its query string. `400` when the link has expired or was already used |

New passwords must be at least `PASSWORD_MIN_LENGTH` (8) characters and may not start or end with a space; a refused password answers `400` with the reason in `detail`.

---

## Existing endpoints the admin screens still use

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/users?role=TEACHER` | List users, optional role filter |
| `PUT` | `/admin/users/{id}` | Edit `full_name`, `email`, `is_active` |
| `DELETE` | `/admin/users/{id}` | Deactivate (soft delete; `200` with a `UserDeleted` summary) — signs the user out everywhere |
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
