# Database setup on your Mac (about 15 minutes)

This starts the graph database (FalkorDB) on your own computer and checks that it works.
Do the steps in order. If a step fails, stop and send me the error message.

## What you need first

- **Docker Desktop.** Download it from docker.com, install it, open it, and wait until it says it is running.
- **Python 3.10 or newer.** In Terminal, type `python3 --version`.
  If it shows 3.9 or lower, install a newer one: `brew install python@3.12`, then use `python3.12` instead of `python3` below.

## Steps

1. **Put this folder inside your main Foreman project folder**, then open Terminal in it.

2. **Start the database:**

   ```
   docker compose up -d
   ```

   The first time it downloads the database, so it takes a minute.

3. **Look at the graph tool in your browser:** open http://localhost:3000.
   If it asks where the database is: host `localhost`, port `6379`, leave user and password empty.
   You will use this screen to *see* the graph (claims, tasks, who calls what).

4. **Make a private Python space and install the database library:**

   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. **Health check:**

   ```
   python check_setup.py
   ```

   You want to see `OK: FalkorDB at localhost:6379 accepted a write...`

6. **Safe-claim test** (the most important test for our project):

   ```
   python claim_spike.py
   ```

   It makes 50 fake workers grab the same task at the same instant, 30 times.
   You want to see these two lines say **30/30**:

   - `2 atomic ... rounds with exactly 1 winner: 30/30`
   - `3 conflict ... rounds with exactly 1: 30/30`

   Line 1 (`naive`) is supposed to look bad (many winners). That is the point: it shows what goes wrong
   when you do not claim safely.

7. **Crash test** (proves the "memory survives a restart" part of our demo):

   ```
   bash check_persistence.sh
   ```

   You want to see `OK: memory survived the restart`.

## Daily use

- Start: `docker compose up -d`
- Stop (data is kept): `docker compose stop`
- Look at logs if something is odd: `docker compose logs falkordb`
- Wipe everything and start fresh (careful, deletes the graph): `docker compose down -v`

## Keys and secrets

Copy `.env.example` to `.env` and put your NVIDIA key in `.env`. The `.gitignore` here already keeps `.env`
out of GitHub. Never paste the key into code or chat.

## What I already tested (on my side, not on your Docker)

- Safe claiming: with 50 and with 100 fake workers fighting over one task, exactly one won every time
  (100 rounds each, no errors). That includes two tasks that conflict with each other.
- The "check first, then write" way of claiming gave duplicates in every round, so we must never use it.
- Data survived a hard kill of the database.

I tested on the same database engine but not the exact Docker image you will run, so steps 6 and 7 are
your proof on the real setup. The crash test (step 7) has not been run against real Docker yet. If it fails,
the likely cause is where the image stores its data, and the fix is a one-line change in `docker-compose.yml`.
