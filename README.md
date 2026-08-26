# Grants Manager

A small, private grant-tracking app for research faculty. Budgets by category
and year, expenses with receipts, salary/fringe projections from appointments,
and reports you can hand to your sponsor.

Everything runs **on your own computer**. No accounts, no cloud service, no
internet connection needed. Your data never leaves your machine.

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

## Using it on your phone

With the app running on your computer and your phone on the **same Wi-Fi**,
open the `http://192.168.x.x:8765` address the app prints at startup. On
iPhone, Safari → Share → **Add to Home Screen** installs it like an app.

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
