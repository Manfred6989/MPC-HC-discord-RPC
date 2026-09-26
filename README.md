# MPC-HC Discord Rich Presence

Shows what you're watching in [MPC-HC](https://github.com/clsid2/mpc-hc) as a Discord
"Watching …" status, with a progress bar while playing and "Paused at 12:34 / 45:00" when paused.

It's a single Python script. It polls MPC-HC's built-in web interface (`/variables.html`)
and sends the result to the Discord desktop app over its local IPC pipe
(via [pypresence](https://github.com/qwertyquerty/pypresence)).

## Setup (Windows)

1. **Enable MPC-HC's web interface**
   MPC-HC → *View → Options → Player → Web Interface* → tick **Listen on port** (default `13579`).
   Keep **Allow access from localhost only** ticked.
   Check it works: open <http://127.0.0.1:13579/variables.html> in a browser while a video plays.

2. **Create a Discord application** (free, takes a minute)
   - Go to <https://discord.com/developers/applications> → **New Application**.
     Name it `MPC-HC` (the name appears on your profile card).
   - Copy the **Application ID** from *General Information*.
   - Optional, for the icon: *Rich Presence → Art Assets* → upload an MPC-HC logo and name it `mpc-hc`.
     (Newly uploaded assets can take a few minutes to show up.)

3. **Install Python 3.10+** from <https://www.python.org/downloads/> (tick "Add python.exe to PATH"), then in this folder:
   ```
   pip install -r requirements.txt
   copy config.example.json config.json
   ```
   Edit `config.json` and set `discord_client_id` to your Application ID.

4. **Run it**
   ```
   python mpc_discord_rpc.py
   ```
   Or double-click `run_hidden.bat` to run it without a console window (logs go to `mpc_discord_rpc.log`).
   To start with Windows: press `Win+R`, type `shell:startup`, and put a shortcut to `run_hidden.bat` there.

Discord must be the **desktop app** (the browser version has no IPC pipe), and
*User Settings → Activity Privacy → Share your detected activities* must be on.

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `discord_client_id` | — | Your Discord Application ID (required). Can also be set with env var `MPC_DISCORD_CLIENT_ID`. |
| `mpc_host` / `mpc_port` | `127.0.0.1` / `13579` | Where MPC-HC's web interface listens. |
| `poll_interval` | `2.0` | Seconds between polls. |
| `large_image` | `mpc-hc` | Art asset key (or an `https://` image URL). |
| `large_text` | `MPC-HC` | Tooltip on the image. |
| `show_filename` | `true` | `false` shows "Watching a video" instead of the file name. |
| `strip_extension` | `true` | Drop `.mkv`, `.mp4`, … from the name. |
| `hide_when_paused` | `false` | Clear the status while paused. |
| `hide_when_stopped` | `true` | Clear the status while stopped. |

## Notes / limitations

- Discord rate-limits presence updates, so changes (pause, seek, next file) are pushed at most
  every 5 s. Normal playback doesn't need updates: Discord runs the progress bar itself.
- Playback speed is taken into account for the progress bar.
- Command line: `-c path\to\config.json`, `-v` for debug logging, `--log-file FILE`.
- Should also work with MPC-BE, which has a compatible web interface, but that's untested.

## Tests

```
python -m unittest tests/test_mpc_discord_rpc.py
```
