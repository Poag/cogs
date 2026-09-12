# cogs

Custom cogs for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot).

## Installation

Add this repository to your Red instance, then install a cog from it:

```
[p]repo add cogs https://github.com/poag/cogs
[p]cog install cogs <cogname>
[p]load <cogname>
```

## Cogs

| Cog | Description |
| --- | --- |
| `gamelog` | Logs what games members are playing per server, who's playing what, and how long each session lasts. Ignores non-game statuses (Spotify, custom statuses, etc). Requires the Presence and Server Members privileged intents. |
| `voicelog` | Logs who's in voice channels per server, and how long each session lasts. Requires the Voice States intent. |
| `autovoice` | Automatic voice channel creation: join a configured "source" voice channel and get moved into your own brand new voice channel, which is deleted once everyone leaves it. Requires the Voice States intent. Does *not* require the Presence or Server Members privileged intents for its core functionality - only the optional `game`-based room name/hint templates need the Presence intent (see below). Can migrate an existing AutoRoom setup with `[p]autovoiceset migrate`. |

### gamelog commands

| Command | Description |
| --- | --- |
| `[p]gamelog playtime [member]` | Total logged time per game for a member (yourself by default). |
| `[p]gamelog top <game>` | Top 10 players of a specific game, by time played. |
| `[p]gamelog leaderboard` | Top 10 players across all games combined, by time played. |
| `[p]gamelog games` | Every game logged in the server, sorted by total time played. |

### voicelog commands

| Command | Description |
| --- | --- |
| `[p]voicelog voicetime [member]` | Total logged voice channel time per channel for a member (yourself by default). |

### autovoice commands

Member-facing, once you're in a room you own (alias `[p]av`):

| Command | Description |
| --- | --- |
| `[p]autovoice settings` (`about`, `info`) | Show your room's current settings. |
| `[p]autovoice name <name>` | Rename your room. |
| `[p]autovoice bitrate <kbps>` (`kbps`) | Change your room's bitrate. |
| `[p]autovoice users <user_limit>` (`userlimit`) | Change your room's user limit. |
| `[p]autovoice claim` | Claim ownership of a room whose owner has left. |
| `[p]autovoice public` | Make your room visible and joinable by everyone. |
| `[p]autovoice locked` | Make your room visible, but not joinable, by everyone. |
| `[p]autovoice private` | Hide your room from everyone. |
| `[p]autovoice allow <member_or_role>` (`add`) | Allow a member or role into your room. |
| `[p]autovoice deny <member_or_role>` (`ban`, `block`) | Deny (and disconnect) a member or role from your room. |

Admin-facing, for setting up sources (requires Manage Server):

| Command | Description |
| --- | --- |
| `[p]autovoiceset settings` | Show all AutoVoice Source settings for this server. |
| `[p]autovoiceset permissions` (`perms`) | Check the bot has the permissions each AutoVoice Source needs. |
| `[p]autovoiceset access admin` / `access mod` | Toggle whether admins/mods can join locked or private rooms. |
| `[p]autovoiceset access bot add/remove <role>` | Manage bot roles automatically allowed into every room. |
| `[p]autovoiceset create <source> <category>` (`enable`, `add`) | Set up a voice channel as an AutoVoice Source via a short setup wizard. |
| `[p]autovoiceset remove <source>` (`disable`, `delete`, `del`) | Remove an AutoVoice Source. |
| `[p]autovoiceset modify category <source> <category>` | Change a source's destination category. |
| `[p]autovoiceset modify type public/locked/private/server <source>` | Change the default room type a source creates. |
| `[p]autovoiceset modify name username/game/custom <source> [template]` | Change how created room names are generated. |
| `[p]autovoiceset modify hint set/disable <source> [text]` | Configure a welcome message sent into each new room's own chat. |
| `[p]autovoiceset modify specialperms ownermodify <source>` | Toggle whether room owners get Manage Channels on their room. |
| `[p]autovoiceset modify specialperms sendmessage <source>` | Toggle whether members can send messages in a room's own chat. |
| `[p]autovoiceset modify defaults` | Read how bitrate/user limit/permissions/member roles defaults work. |
| `[p]autovoiceset migrate` | One-time import of this server's AutoRoom setup - see [Migrating from AutoRoom](#migrating-from-autoroom) below. |

### Migrating from AutoRoom

`autovoice` is a from-scratch reimplementation of PhasecoreX's `autoroom` cog (automatic voice channel creation), with the optional legacy companion text channel feature dropped - it's made obsolete by Discord's own voice channel text chat. `[p]autovoiceset migrate` reads AutoRoom's own stored configuration directly and copies it into `autovoice`'s config for the current server, so a bot owner can switch cogs without disrupting anything already set up. Workflow:

1. Make sure `autoroom` is loaded (so it has run its own internal migrations at least once), then `[p]unload autoroom` so the two cogs aren't both moving members around at the same time.
2. `[p]load autovoice`.
3. In each server that used AutoRoom, run `[p]autovoiceset migrate`. This copies over the server's admin/mod/bot access settings, every AutoRoom Source (minus its legacy text channel setting, which gets reported back to you as dropped if it was in use), and every currently open AutoRoom-created voice channel (so people keep their room ownership and deny lists without having to wait for the room to empty out). It's safe to run more than once.
4. Confirm things work by joining a source channel. Once you're happy, `[p]unload autoroom` for good (or `[p]cog uninstall` it) - `migrate` never does this for you.

### Relationship data

`gamelog` and `voicelog` each log plain `(guild, user, ..., start, end, duration)` session rows to their own SQLite database - `gamelog`'s `sessions` table (one row per game session) and `voicelog`'s `voice_sessions` table (one row per voice-channel session). Neither cog computes "who was with whom" or "what game were they playing while in voice" itself; both are derivable later by joining:

- `voicelog`'s `voice_sessions` against itself on matching `channel_id` with overlapping `[start_time, end_time]` windows, to find who shared a voice channel and for how long, and
- `voicelog`'s `voice_sessions` against `gamelog`'s `sessions` on matching `user_id` with overlapping time windows, to find what game (if any) someone was playing during a given voice session.

Since the two cogs keep separate database files (each in its own cog data folder), that second join needs both files open at once (e.g. SQLite's `ATTACH DATABASE`). Those joins are the intended basis for building a relationship graph from this data. `autovoice` doesn't fit into this - it doesn't log session history, only current live state (room ownership and deny lists).

## Privileged intents

`gamelog` needs the **Presence Intent** and **Server Members Intent** enabled for the bot, both in the [Discord developer portal](https://discord.com/developers/applications) (under your application's Bot settings) and in Red's own intents config (`redbot-setup` lets you toggle these, or edit the instance's intents). Without both, `gamelog` will never see game activity changes and will log nothing.

`voicelog` needs the **Voice States Intent**. It isn't a privileged intent, but Red's default intents config can still have it turned off - check the same intents settings as above.

`autovoice` also needs the **Voice States Intent** to see members joining/leaving voice channels at all - without it, it won't create or clean up rooms. It does *not* need the Server Members intent (member objects arrive attached to voice state updates regardless). It also does *not* need the Presence intent for its core room creation/deletion/ownership behavior. The one exception is the `game` channel-name/hint template variable (mirroring `gamelog`'s reliance on presence data): `discord.Member.activities` is only ever populated by presence data from Discord's gateway, which the client only receives (and caches) when the Presence intent is enabled - regardless of whether anything is listening for presence update events. So an AutoVoice Source configured with `[p]autovoiceset modify name game` will work without the Presence intent, but `{{game}}` will always render empty, and named rooms will just fall back to the username format.

## Contact

Open an issue on this repository for bugs or feature requests.

## Credits

Built following the [Red-DiscordBot cog creation guide](https://docs.discord.red/en/stable/guide_cog_creation.html) and [publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html).
