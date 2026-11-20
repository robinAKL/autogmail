# Mail Autopilot

Reads your unread Gmail and Outlook mail and answers it the way you would, using only your
profile and your own knowledge base. Replies that would commit you to something are sent to
you on WhatsApp first, and go out only after you say yes.

It starts in **draft only** mode: replies are written and saved, nothing is sent.

## What it does

- **Inbox** – Gmail and Outlook side by side. Letters are shown with their original formatting
  (HTML), in a locked frame that cannot run scripts or submit forms.
- **Knowledge base** – upload files (PDF, Word, PowerPoint, Excel, text, HTML, .eml), write notes,
  let it summarise your past sent and received email, or read documents from SharePoint.
  From all of that plus your rules it writes **“What I understand about you”**, which you can edit.
  Every reply is drafted from the profile, that understanding, your rules and the most relevant
  passages of your documents.
- **Approvals** – each reply is marked either routine or *needs your OK* (dates, meetings, prices,
  money, decisions, sensitive topics, missing facts, or your own rules say so). Waiting replies can
  be sent, edited, rewritten or dropped on the Approvals page or on WhatsApp.
- **WhatsApp** – you get the letter, the reason and the draft. Answer `YES` / `同意` to send,
  `NO` to drop it, or type what to change and it rewrites the draft. `LIST`, `STATUS`, `PAUSE`
  and plain questions about your mail also work. A morning summary of yesterday is optional.
- **Dashboard** – yesterday at a glance (checked, sent automatically and after approval, reply time,
  time saved, skipped, waiting), and 7 / 30 / 90-day history: outcomes per day, why mail was skipped,
  who writes most, when mail arrives, how often the knowledge base was used, your approval rate and
  how long you take to decide.

### Sending modes (Settings page)

| Mode | What happens |
|---|---|
| Draft only | Replies are saved. Nothing is sent. |
| Ask every time | Every reply waits for your OK. |
| Ask when needed | Routine replies go out; anything with a commitment waits for your OK. |
| Send automatically | Everything is sent without asking. |

Real sending cannot be turned on while the profile is still the placeholder example.

### What is never answered

Mail to or from yourself, no-reply and system addresses, mailing lists, automatic replies,
promotions, social and forum mail, and anything mentioning passwords, codes, ID or bank details
(those never reach the model). A reply containing a phone number, link or email address that is not
in your material is held back.

## Run it on this computer

1. Python 3.10 or newer, then in this folder:

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```

2. Put your `OPENAI_API_KEY` in `.env`.
3. **Gmail**: in [Google Cloud](https://console.cloud.google.com/) enable the Gmail API, create an
   OAuth client of type *Desktop app*, download the JSON into this folder as `credentials.json`.
4. Start it:

   ```powershell
   python main.py
   ```

   The first time, a browser opens to approve Gmail. Then open <http://127.0.0.1:8765>.
   `python main.py --once` checks mail once and exits; `python main.py --no-check` opens only
   the web page without checking mail.

### Outlook and SharePoint

1. In the [Microsoft Entra admin center](https://entra.microsoft.com/) → *App registrations* →
   *New registration*. Supported account types: any organisational directory and personal accounts
   (or only your organisation).
2. Redirect URI, type *Web*: `http://localhost:8765/oauth/microsoft/callback`
   (add `https://<your-domain>/oauth/microsoft/callback` for the hosted site).
3. *Certificates & secrets* → new client secret. Put the IDs in `.env` as `MS_CLIENT_ID`,
   `MS_CLIENT_SECRET` (and `MS_TENANT_ID` to limit it to your organisation).
4. Open **Connections** and choose *Connect Outlook* and/or *Connect SharePoint*. Outlook asks for
   `Mail.ReadWrite`, `Mail.Send` and `Calendars.ReadWrite`; SharePoint asks for read-only `Sites.Read.All` and
   `Files.Read.All`, which some organisations require an admin to approve. Gmail also asks for
   `calendar.events` so Calendar can list and create meetings. Connect again after this change.

Then on **Knowledge**, paste a link to a SharePoint site, library folder or single file.

### WhatsApp

Use either the **Meta WhatsApp Cloud API** (`WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`) or **Twilio** (`TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`). Save your own number on **Connections** and send a
test message.

To answer from your phone, the provider must reach the webhook `https://<address>/whatsapp/webhook`.
On this computer that means a tunnel to port 8765 (for example `ngrok http 8765`); on Vercel it is
the site address. Only your number is written to and obeyed, and Meta / Twilio signatures are checked.

WhatsApp lets a business write freely only within 24 hours of your last message to it. If the chat
has been quiet longer, send the bot any message (or set `WHATSAPP_TEMPLATE` to an approved template
that invites you to).

## Hosted on Vercel

Set the variables from `.env.example` in Vercel, connect **Vercel KV (Upstash)** — without it the
knowledge base, approvals, WhatsApp and dashboard cannot keep anything — and set `CRON_SECRET`.
`vercel.json` calls `/cron` every 5 minutes (needs a Vercel Pro plan; Hobby only allows a daily cron). Uploads are limited to about 4 MB per request there.

## Your data

`.env`, `token.json`, `credentials.json` and the `data/` folder hold keys and your mail records:
do not share them. `profile.json` becomes your personal profile; look at it before committing.

## Tests

```powershell
python -m unittest discover -s tests -t . -v
```

## Hosted SaaS

Set `AUTOMAIL_SAAS=1` only for a separate SaaS deployment. Keep the existing
single-user deployment and its storage separate. SaaS starts each verified
login address with its own workspace. A Gmail or Outlook connection stays
with the workspace that authorized it, even if that mailbox address is
later used to sign in to another workspace.

Configure these services once on the server:

| Setting | Platform configuration |
| --- | --- |
| `AUTOMAIL_PUBLIC_ORIGIN` | Canonical HTTPS origin, without a path |
| `FLASK_SECRET_KEY` | Strong random session signing secret, shared across instances |
| Redis | Persistent Redis or Upstash credentials, dedicated to SaaS |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | Platform delivery of sign-in codes |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` | Google Web OAuth client |
| `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT_ID` | Microsoft Web registration with organizational and personal accounts, using `common` where appropriate |
| `R2_*` | Persistent downloads and uploads, required for hosted attachments |
| AI model credentials | Platform model for draft generation, if enabled |

Register the public origin plus `/oauth/callback` in Google and the same origin
plus `/oauth/microsoft/callback` in Microsoft. Users connect through the
providers' consent screens. The inherited single-user cron and WhatsApp
routes are disabled in SaaS mode. Use manual refresh and approve sending in
the web app during the initial rollout.

Before accepting real accounts, smoke-test on the deployment domain:

1. Verify a non-Gmail sign-in address receives a code and can create a workspace.
2. Connect a different Gmail address. Sign out and sign in as that Gmail
   address. Confirm the new workspace starts without the first workspace's
   connections, mail, settings, approvals, or attachments.
3. Connect an Outlook.com address and a Microsoft 365 mailbox separately.
   Check refresh, manual reply, disconnect, reconnect, and access after logout.
4. Check that a duplicate approval sends once and that a timed-out delivery
   appears as unconfirmed. Check the provider's Sent folder before resending.
5. Complete the applicable Google verification and security assessment
   before opening public onboarding for Gmail.

Single-user records are not imported automatically into the hosted deployment.
