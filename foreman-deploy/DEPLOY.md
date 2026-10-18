# Putting Foreman online (simple steps)

**What goes online:** the screen, playing the recorded run (a "replay"). It needs no database and no model key, so it cannot break while judges look at it, and it cannot cost you anything. The replay is a real run that was recorded from FalkorDB. The page says "Replay" in its top corner so nobody is misled.

**What stays on your computer:** the live crew (needs your NVIDIA key) and the database. The README tells judges how to run those themselves.

## Before you start: make the replay files

Run this once on your computer, in the `foreman-code` folder, with the database running and the run in it:

```
source .venv/bin/activate
python export_replay.py --repo tinydb --run run-5
```

This writes three things into `foreman-code/ui/`:
- `replay/run-5.json` (the run)
- `replay/index.json` (the list of runs)
- `results.json` (the numbers page: the three-setup comparison and the load test, from `eval_results.json`, `race_results.json` and `load_test_results.json` if they are in the folder)

Open the screen once on your computer to check (`./run_all.sh --screen`, then http://localhost:8000). If it looks right, the `ui` folder is the whole website.

## Way 1, fastest: drag and drop (no GitHub needed, about 2 minutes)

1. Go to https://app.netlify.com/drop and sign up (free).
2. Drag the `foreman-code/ui` folder onto the page.
3. You get a link like `https://something-random.netlify.app`. Open it and click through the five pages.
4. Optional: in Site settings, rename the site to something readable like `foreman-graph`.

## Way 2: Render static site (if you prefer Render)

1. Push the Foreman folder to a public GitHub repo. Check first that `.env` is NOT in it (see "Before you push").
2. Go to https://render.com, choose New, then Static Site, and pick the repo.
3. Build command: leave empty. Publish directory: `foreman-code/ui`.
4. Create the site. You get a link like `https://foreman-graph.onrender.com`.

## Before you push to GitHub (one minute)

- `.env` must not be in the repo. Check: `git status` should not list it, and `.gitignore` should contain a line `.env`.
- Add these lines to `.gitignore` too: `.venv/`, `work/`, `__pycache__/`, `*.pyc`.
- Keep `foreman-code/ui/replay/` and `foreman-code/ui/results.json` in the repo (do not ignore them).
- Never put the NVIDIA key or any database password in any file you push.

## If the page says "waiting" or shows no run

- Open `your-link/replay/index.json` in the browser. If it says "not found", the replay files were not in the `ui` folder you uploaded. Run the export step again and upload again.

## Optional: a cloud database (not needed for the public page)

The public page does not need one. If you want to show the crew writing into a cloud graph, FalkorDB Cloud's free plan works, with these two details:
- It does not use TLS, so use `FALKORDB_SSL=false`.
- The username is the one you chose when you created the database (not `default`).

Put the host, port, username and password in `foreman-code/.env` only. Do not paste the password into chat or a file you share. The README's local setup works without any of this.

## Notes

- Nothing on the public page can change data. It only plays a saved file.
- Netlify and Render static sites stay awake, so there is no "first visit takes a minute" delay.
