# Hermes Raven Plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets you use [Raven](https://github.com/The-Commit-Company/raven), the Frappe chat app, as a Hermes channel.

- DM Hermes and it replies.
- In a channel, @mention Hermes and it replies with a quote of your message.
- Send it images and files. It can send images and files back.
- Cron jobs and `hermes send --to raven:<email or channel id>` deliver into Raven.

## How it works

Hermes signs in as a normal Raven user with an API key. It polls Raven's own unread counts every 2 seconds, reads the new messages, and marks them read. This works like Telegram's `getUpdates`:

- No public URL, tunnel, or webhook is needed. Hermes can run on your laptop while Raven runs on Frappe Cloud.
- Nothing is installed on the Frappe site.
- Hermes only sees the channels and DMs its user can see in Raven.

## Setup

1. On the Frappe site, create a user for the agent (for example `hermes@yourcompany.com`) and give it the **Raven User** role.
2. Add that user to a Raven workspace and to any channels it should listen in.
3. On the user's page, choose **Settings > API Access > Generate Keys**. Copy the key and secret.
4. Install the plugin and turn it on:

   ```bash
   git clone https://github.com/gajjug004/hermes-raven-plugin.git ~/.hermes/plugins/raven
   hermes plugins enable raven
   ```

5. Add the settings to `~/.hermes/.env`:

   ```bash
   RAVEN_URL=https://chat.yourcompany.com
   RAVEN_API_KEY=...
   RAVEN_API_SECRET=...
   RAVEN_ALLOWED_USERS=you@yourcompany.com,teammate@yourcompany.com
   ```

6. Restart the gateway: `hermes gateway restart` (or `hermes gateway run`).

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RAVEN_URL` | required | Site URL |
| `RAVEN_API_KEY`, `RAVEN_API_SECRET` | required | Keys of the agent's Frappe user |
| `RAVEN_ALLOWED_USERS` | none | Emails allowed to talk to Hermes. Other senders are ignored. |
| `RAVEN_ALLOW_ALL_USERS` | `false` | Let every Raven user talk to Hermes |
| `RAVEN_REQUIRE_MENTION` | `true` | In channels, reply only when @mentioned. DMs always get a reply. |
| `RAVEN_POLL_INTERVAL` | `2` | Seconds between checks for new messages |
| `RAVEN_HOME_CHANNEL` | none | Channel ID or email for cron and notification delivery |

## Not supported yet

- Replies inside Raven threads. Raven keeps threads out of its unread counts, so Hermes does not see them.
- The typing indicator. Raven only exposes it over socket.io.
- Editing a message in place while Hermes streams. Hermes sends the final reply as a new message.

## Check

```bash
PYTHONPATH=<hermes-agent checkout>:~/.hermes/plugins <hermes python> -m raven.test_adapter
```
