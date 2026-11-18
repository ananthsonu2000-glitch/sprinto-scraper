# Connect Gmail + Google Calendar (about 10 minutes)

Needed for the remaining live items: sending demo emails, reading replies, checking the calendar
and booking meetings. Use a **demo Google account** (a separate Gmail is best). The tool only
ever sends to `DEMO_RECIPIENT_EMAIL` and only invites that address.

## 1. Google Cloud project
1. Open https://console.cloud.google.com/ while signed in as the demo account, and create a project (any name).
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.

## 2. OAuth consent screen
1. **APIs & Services → OAuth consent screen** (or *Google Auth Platform → Branding/Audience*).
2. User type **External**, publishing status **Testing**.
3. Add the demo account under **Test users**.
4. Scopes don't need adding here; the script requests exactly these four:
   `gmail.send`, `gmail.readonly`, `calendar.events`, `calendar.freebusy`.

## 3. OAuth client
1. **Credentials → Create credentials → OAuth client ID** → application type **Desktop app**.
2. Copy the **Client ID** and **Client secret** into `.env`:
   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```

## 4. Sign in once
```bash
.venv/Scripts/python scripts/google_oauth.py
```
A browser opens. Sign in as the demo account and approve. Google may say the app isn't verified;
that's expected for a Testing app you own (choose *Continue*). The script saves
`GOOGLE_REFRESH_TOKEN` into `.env` itself and prints which account was authorised.

## 5. Fill in the addresses in `.env`
```
GMAIL_SENDER_ADDRESS=<the demo account you just authorised>
DEMO_RECIPIENT_EMAIL=<the inbox that should receive every demo email and invite>
SENDER_DISPLAY_NAME=<how emails are signed>
```
The two addresses can be the same account (self-addressed demo threads are supported).

## 6. Check
```bash
.venv/Scripts/python scripts/check_setup.py
```
`Google (Gmail + Calendar)` should show OK. Everything is still **dry run** at this point:
the calendar is read, but nothing is sent or booked.

## 7. Go live (your decision)
Set `APP_MODE=real_demo` and `REAL_SENDS_ENABLED=true`, then restart the dashboard.
Start with one company, reply from your demo inbox, and watch the agent propose times and
book a `[DEMO]` event on the demo calendar.
