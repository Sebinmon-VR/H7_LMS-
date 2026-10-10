# Online admissions: from application to welcome email

```
website form ─► NEW ─► (UNDER_REVIEW / WAITLISTED)
                 │
                 ▼  office: "Select & request documents"
        DOCUMENTS_REQUESTED ◄──────────────┐  office rejects a document (with a reason)
                 │  family uploads + submits │
                 ▼                           │
        DOCUMENTS_SUBMITTED ─────────────────┘
                 │  office accepts every required document
                 ▼
        DOCUMENTS_VERIFIED
                 │  office: "Request payment" (Razorpay link, or a link/bank details of its own)
                 ▼
        PAYMENT_REQUESTED ◄────────────────┐  office sends the proof back (with a note)
                 │  family pays            │
                 │   ├─ Razorpay: confirmed automatically ──────────┐
                 │   └─ proof: transaction id and/or screenshot     │
                 ▼                         │                        │
        PAYMENT_SUBMITTED ─────────────────┘                        │
                 │  office confirms                                 │
                 ▼                                                  │
        PAYMENT_VERIFIED ◄──────────────────────────────────────────┘
                 │  office: "Admit"
                 ▼
             ADMITTED ─► one welcome email: admission details, student + parent logins,
                         web app and mobile app links; fee entered in Finance as a receipt
```

The office can skip steps (request the fee without documents, admit without a fee). Code:
`app/services/admission_pipeline.py` (the steps), `app/services/admission_requests.py`
(submit, decisions, admit), `app/core/mailer.py` (every family-facing letter).

## The family's page

Every email carries `ADMISSION_PORTAL_URL?t=<token>`; the page is `admission-status.html` on
the website repo. The token is random, per application, and is the only key. Uploads are PDF
or images, up to `ADMISSION_DOCUMENT_MAX_MB` each, stored privately under `admissions/` in
Blob storage; the admin screen opens them through links signed for an hour.

## Settings

School Settings (admin screen) holds the school's name, contact email/phone, address, logo,
web app link, mobile app link and portal page address. The environment values in
`app/core/config.py` are only the fallback. The contact email is the Reply-To of every
family email, because the sender is a no-reply address.

`h7ed.com` is currently a registrar frame around the Static Web App: only its home page
works, so the defaults link to `blue-cliff-058b88c10.1.azurestaticapps.net` directly. Once
the domain is pointed at the Static Web App properly (a CNAME, or the apex via Azure DNS),
change the portal, logo and app links in School Settings to `https://h7ed.com/...`.

## Razorpay (optional)

Without keys the office pastes any payment link or bank/UPI instructions and confirms the
family's proof by hand. With keys, payment links are created through Razorpay and confirm
themselves:

1. App Service settings: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
2. Razorpay Dashboard → Webhooks → add
   `https://<backend>/api/v1/admissions/requests/payments/razorpay/webhook`, event
   `payment_link.paid`, and put its secret in `RAZORPAY_WEBHOOK_SECRET`. Without the webhook
   the status is still read back when the family returns from paying and when the office
   presses "Check payment status".

## The sender's name

ACS takes the display name from the sender configured on the email domain, so mail shows as
"DoNotReply" until it is changed. With the Azure-managed domain:

```
az communication email domain sender-username update --resource-group h7 \
  --email-service-name h7-lms-email --domain-name AzureManagedDomain \
  --sender-username donotreply --username DoNotReply --display-name "H7 EdTech Admissions"
```

A custom domain (e.g. `admissions@h7ed.com`) reads better still and lands in spam less often.
