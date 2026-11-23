# Run Finance Free on a cloud server

This puts paper trading on a small server that runs by itself. After each US
close, it records the day, writes the trade ideas for the next open, and sends
you a Telegram summary. It costs about €4–5 a month on Hetzner.

You do the parts that need your accounts (steps 1–3, about 15 minutes). One
script does everything else (step 4).

## 1. Make an SSH key on your Mac (skip if you have one)

```sh
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519      # press Enter at each question
pbcopy < ~/.ssh/id_ed25519.pub                          # copies the public key
```

## 2. Create the server on Hetzner

1. Sign up at https://console.hetzner.com. Hetzner may ask for ID or a card check the first time.
2. Create a project (any name), then click **Add server**:
   - **Location:** any EU site, such as Falkenstein, Nuremberg or Helsinki. The cheapest plans are there, and the location doesn't matter for end-of-day trading.
   - **Image:** Ubuntu 24.04.
   - **Type:** Shared vCPU, x86, the cheapest one (CX22 or the current equivalent: 2 vCPU, 4 GB).
   - **Networking:** keep **Public IPv4** on. GitHub can't be reached over IPv6 only.
   - **SSH keys:** click *Add SSH key* and paste the key you copied in step 1.
   - Leave everything else as it is, then click **Create & Buy now**.
3. Copy the server's IPv4 address. It's shown as `IP` below.

## 3. Create a Telegram bot (optional, but recommended)

1. In Telegram, message **@BotFather** and send `/newbot`. Pick a name. It replies with a **token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. Your **chat id** is the number after `"chat":{"id":`.

## 4. Install

```sh
ssh root@IP
curl -fsSL https://raw.githubusercontent.com/NadavAtias/Finance-Free/main/deploy/install.sh -o install.sh
bash install.sh
```

The script asks for the Telegram token and chat id, then sends you a test
message. It finishes in about 5 minutes. It also sets up the server:

- updates the system and turns on automatic security updates
- opens only SSH to the internet
- turns off password logins
- installs Docker and starts Finance Free

Finance Free starts again by itself after a crash or a reboot.

## 5. Open the dashboard

The dashboard has no login, so it isn't reachable from the internet. Open a
private tunnel from your Mac:

```sh
ssh -N -L 8000:localhost:8000 root@IP
```

Leave that window open, then go to http://localhost:8000. Press Ctrl+C to close the tunnel.

To make it a single word, add this to `~/.zshrc`:
`alias ff-dash='ssh -N -L 8000:localhost:8000 root@IP'`

## What happens each day

- **16:45 New York time (23:45 Israel time):** the run starts. If the day's prices aren't published yet, it tries again every 30 minutes, up to 4 more times.
- **Telegram:** you get a summary with the day's result and the trades to place at the next open.
- **Something goes wrong:** you get a Telegram message saying so, and the next run catches up on any missed days.
- **Silence:** no message by the next morning means the server itself is down. Check it in the Hetzner console.
- **Backups:** the ledger is backed up every night to `state/backups/`. The newest 30 are kept.

## Everyday commands (on the server)

```sh
cd /opt/finance-free
docker compose -f docker/compose.yaml logs -f paper   # follow the daily run
docker compose -f docker/compose.yaml ps              # is everything up?
bash deploy/update.sh                                 # install the latest version
```

## If Yahoo blocks the server

Yahoo sometimes refuses requests from data centers. If your alerts say the run
failed on `finance.yahoo.com`, switch to Tiingo, which has a free tier:

1. Get a key at https://www.tiingo.com.
2. Edit `/opt/finance-free/.env` and set `TIINGO_API_KEY=...` and `FF_PROVIDER=tiingo`.
3. Run `docker compose -f docker/compose.yaml up -d`.
