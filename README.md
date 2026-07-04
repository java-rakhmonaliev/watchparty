# Watch Party

**Movie nights with friends who are far away — actually together.**

Everyone opens the same movie file on their own computer, and Watch Party keeps
playback perfectly in sync: when anyone presses play, pause, or skips, it happens
for everyone at the same moment. Your friends' faces and voices are right there in
the same browser tab. No installs, no accounts — just a link.

The movie itself **never leaves anyone's computer**. Nothing is uploaded anywhere;
the app only synchronizes *when* to play, not the video itself.

## How a movie night works

1. **Start a party.** Open the site, click *Start a party* — you get a private
   link like `velvet-otter-x7k2m9`. It's random, so nobody can guess it.
2. **Send the link to your friends** (up to 5 people per room).
3. **Everyone gets the same file.** Each person needs their own copy of the
   exact same video file (same download — if it doesn't match, the app warns you).
4. **Everyone picks the file, types a name, and enters.** Cameras and voice are
   optional — you can watch and listen without turning yours on.
5. **Watch together.** Anyone can pause, skip, or resume. Faces can sit in the
   sidebar, float over the movie, or pop out into their own window for a second
   screen.

The first person in the room is the **host**: they can lock the room (new people
then knock and wait to be let in), remove or mute people, and close the room.

## Tips for the smoothest night

- **Chrome, Edge, or Arc work best.** Firefox-based browsers sometimes struggle
  with certain video files.
- If someone's picture is black, that file's format is too exotic for their
  browser — a standard MP4 (H.264/AAC) copy of the movie plays for everyone.
- Use headphones so your microphone doesn't echo the movie.

## Running it yourself

For local tinkering you need Python 3.12+:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py runserver
```

Then open http://127.0.0.1:8000 in two browser windows and play.

Tests (server must be running):

```bash
pip install websockets
python scripts/ws_smoke.py     # server protocol tests
node scripts/sync_sim.mjs      # playback-sync tests
```

Everything about servers, hosting, and deployment lives in **[DEVOPS.md](DEVOPS.md)**.
