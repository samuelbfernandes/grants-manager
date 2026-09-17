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
2. **Extract it to a real folder first.** On **Windows**, right-click
   `GrantsManager.zip` → **Extract All…** — don't just double-click into the
   zip and run the starter from there, or Windows runs it from a temporary
   place without the app files and it fails with *"server.py: No such file."*
   On **Mac**, double-click the zip to unpack it. Put the extracted folder
   anywhere (Documents, Desktop, OneDrive — all fine); keep it together, your
   data lives inside it.
3. **Start it**
   - **Mac** — double-click **`Start Grants Manager (Mac).command`**
   - **Windows** — double-click **`Start Grants Manager (Windows).bat`**

   The **first time only**, your Mac or Windows will likely warn that it
   can't verify the app — this is normal for any program not sold through
   Apple's or Microsoft's store, and it doesn't mean anything is wrong. See
   **[If your Mac or Windows blocks it](#if-your-mac-or-windows-blocks-it)**
   just below for the one-time click that lets it through.
4. Your browser opens the app. **Leave the small black window open** while you
   use it — that's the app running. Closing it quits the app.

### If your Mac or Windows blocks it

Because the app isn't code-signed with a paid Apple/Microsoft certificate,
the operating system asks you to confirm the first launch. You only do this
**once** per computer; after that it opens normally on a double-click. Nothing
here is a real error.

**Mac** — you may see *"Apple could not verify 'Start Grants Manager
(Mac).command' is free of malware."* Click **Done**, then:

1. Open **System Settings → Privacy & Security**.
2. Scroll down to the **Security** section — you'll see a line saying the
   starter *"was blocked to protect your Mac"* with an **Open Anyway** button.
3. Click **Open Anyway**, confirm with your password or Touch ID, and click
   **Open Anyway** once more in the final dialog.

   *(On older macOS the button isn't there — instead **right-click** the
   starter → **Open** → **Open**. If neither works, open the built-in
   **Terminal** app, type `xattr -dr com.apple.quarantine ` then drag the
   unzipped folder onto the window and press Return — that clears the flag on
   the whole folder at once.)*

**Windows** — you may see a blue *"Windows protected your PC"* (SmartScreen)
box. Click **More info**, then **Run anyway**. If your browser flagged the
download instead, choose **Keep** on the download.

Neither step needs an administrator; a standard account can do it.

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
9. If a salary charge names someone who isn't in **People** yet, the dashboard
   asks who they are — one click adds the person and links their charges. You
   never have to type payroll names yourself.

If a file can't be read, the app says what it actually was — a PDF, the older
`.xls` format, a CSV — and nothing is imported until one reads cleanly. The
⇅ Workday panel also offers an **example workbook** with fake data in exactly
the columns expected, to compare your export against.

Repeat whenever you want fresh numbers. Re-importing never creates
duplicates.

### Connecting directly (optional, advanced)

Workday can serve a report at a private URL that the app pulls automatically,
configured under **⚙ Settings → Advanced → Direct connection (RaaS)**. This needs
permissions ordinary faculty accounts usually don't have — rights to create
custom reports (*Report Writer*), rights to tick **Enable As Web Service**,
and an account that accepts a username and password rather than SSO-only
(which typically means asking IT for an *Integration System User* exempt from
SSO and MFA). The Instructions tab in the app lists the exact access levels to
ask for. **If it doesn't work, nothing is wrong** — the import steps above
give you the same numbers.

---

## The monthly expense report

At the start of each month the app offers to email you last month's expenses —
and you can send one any time from **⚙ Settings → Send a report now**. It goes
to *you*, not to your accountant: you check it, then forward it.

The email itself is one line. Attached is an **Excel workbook** with a row per
expense (Date, Amount, Spend Category, Business Purpose, Grant/Worktag, Award,
Cost Center, Fund, Person, Receipt) and, for anything bought on a purchasing
card, the cardholder details reconciliation asks for. **Each receipt is
attached separately, named exactly as the workbook's Receipt column names it**,
so a row can be matched to its file by eye.

Tick **💳 P-card** when you add a card purchase and it carries those details
automatically; set the cardholder once under **⚙ Settings → 💳 P-card**.

Sending uses **Microsoft Outlook** on Mac or Windows, which is what lets the
receipts ride along as attachments. On a Mac the first send asks permission
for Grants Manager to control Outlook — click OK once.

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
