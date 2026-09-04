# Grants Manager

A grant-tracking app for research faculty who manage their awards in
**Workday**. Budgets by category and year, expenses with receipts,
salary/fringe projections from appointments, and reports you can hand to your
sponsor.

It's built so you can **download your reports from Workday or enter expenses
manually**, and finally get a visual, at-a-glance understanding of your
accounts and where the money is going.

Built by **Sam Fernandes** — <samuelbf@uark.edu>

---

## Install (about 2 minutes, no admin rights)

1. **Download** — [Latest release](../../releases/latest) → download
   `GrantsManager.zip`.
2. **Unzip it** anywhere you like (Documents, Desktop, OneDrive — all fine).
   Keep the folder together; your data will live inside it.
3. **Start it**
   - **Mac** — double-click **`Start Grants Manager (Mac).command`**
     *(first time only: if macOS says it can't verify the developer,
     right-click the file → **Open** → **Open**)*
   - **Windows** — double-click **`Start Grants Manager (Windows).bat`**
     *(first time only: if a blue "Windows protected your PC" box appears,
     click **More info** → **Run anyway**)*
4. Your browser opens the app. **Leave the small black window open** while you
   use it — that's the app running. Closing it quits the app.

### If it says Python isn't installed

Python is a free, one-time install and **does not need admin rights**.
The starter offers to open the right page for you:

- **Windows** — it opens the Microsoft Store to Python 3; click **Get**, wait,
  then double-click the starter again.
- **Mac** — macOS offers to install "command line developer tools"; click
  **Install**, wait, then double-click the starter again.

---

## Getting your Workday numbers in

The app **works offline by default** — it never asks you to sign in to
anything, and you can simply type expenses in by hand. Most people can't
connect to Workday directly (single sign-on and Duo block it), so the normal
way to use it is: **download a report from Workday, import the file here.**

**In Workday**

1. Sign in as you normally do.
2. Search for `Grant Budget Vs Actuals` and open the report (usually
   **RPT - Grant Budget Vs Actuals**). Names vary by university — any
   budget-versus-actuals report for your grant works.
3. Fill in **Grant** or **Award** with your grant, click **OK**.
4. At the top-right of the report table, click the small **Excel icon**
   (*Export to Excel*) → **Download**. You get an `.xlsx` in your Downloads
   folder. That's your **balances** file.
5. *Optional but recommended:* click an **Actuals amount** to drill into the
   individual transactions, and export that screen too. That's your
   **transactions** file — it's what shows you each charge, not just totals.

**In the app**

6. Click **⇅ Workday** in the top bar.
7. Click **📁 Choose files**, select the `.xlsx` file(s) — you can pick
   several at once. They're read immediately; you never need to find or use
   any folder yourself.
8. First time only: match Workday's grant codes and object classes to your
   grants and categories. It won't ask again.

Repeat whenever you want fresh numbers. Re-importing never creates
duplicates.

### Connecting directly (optional, advanced)

Workday can serve a report at a private URL that the app pulls automatically,
configured under **⚙ Settings → Direct connection (RaaS)**. This needs
permissions ordinary faculty accounts usually don't have — rights to create
custom reports (*Report Writer*), rights to tick **Enable As Web Service**,
and an account that accepts a username and password rather than SSO-only
(which typically means asking IT for an *Integration System User* exempt from
SSO and MFA). The Instructions tab in the app lists the exact access levels to
ask for. **If it doesn't work, nothing is wrong** — the import steps above
give you the same numbers.

---

## Using it on your phone

With the app running on your computer and your phone on the **same Wi-Fi**,
open the `http://192.168.x.x:8765/?k=…` address the app prints at startup. On
iPhone, Safari → Share → **Add to Home Screen** installs it like an app.

**That link ends in an access key — treat it like a password.** Anything on
your network that has it can read and change your grants; anything without it
is refused. The key is created on first run and kept in
`data/access_key.txt`. On the computer running the app you never need it —
`http://127.0.0.1:8765` just works.

### Showing your numbers to someone else

Anyone with that link and key has full access, so to share figures with a
co-PI or department admin use **🖨 Print report** on a grant (a clean page
with the charts, ready to print or save as PDF) or **⬇ Export CSV**. Both are
a snapshot they can keep, with nothing connected back to your app.

---

## Updates

The bell in the top bar tells you when a newer version is out, and shows
what changed in it. About once every 15 days the app asks GitHub for the
latest released version number — it sends nothing about you or your grants,
and if you're offline it quietly does nothing and tries again later.

To update: download the new `GrantsManager.zip`, unzip it, and copy your
existing `data` folder into the new folder, replacing the empty one. Your
grants, expenses and receipts all live in there.

---

## Your data, and one important warning

Everything is stored in a single file inside the app folder:
`data/grants.db`. The app makes an automatic dated backup every time it
starts (kept 30 days, in `data/backups/`), and deleted items are recoverable
for 30 days from **⚙ Settings → Recently deleted**.

**If you keep the folder in OneDrive/Dropbox:** never run the app on two
computers at the same time, and let sync finish before opening it elsewhere.
Databases don't merge like documents — two copies open at once can produce a
"conflicted copy" file. If you see one, don't delete it; it may hold work
missing from the main file. Recover from **⚙ Settings → Backups**.

---

## Requirements

- macOS or Windows (Linux works too)
- Python 3.8 or newer — the starter helps you install it if missing
- A web browser

No other dependencies: the app uses only what ships with Python, plus a
bundled copy of Chart.js for the graphs.

## Tested

Automatically tested on every change against **Windows, macOS and Linux**
with **Python 3.9 and 3.13** — including a real end-to-end launch of the
Windows starter script.

## License

Provided as-is for academic use. Please keep the credit line in the app.
