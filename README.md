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

### Relationship data

`gamelog` and `voicelog` each log plain `(guild, user, ..., start, end, duration)` session rows to their own SQLite database - `gamelog`'s `sessions` table (one row per game session) and `voicelog`'s `voice_sessions` table (one row per voice-channel session). Neither cog computes "who was with whom" or "what game were they playing while in voice" itself; both are derivable later by joining:

- `voicelog`'s `voice_sessions` against itself on matching `channel_id` with overlapping `[start_time, end_time]` windows, to find who shared a voice channel and for how long, and
- `voicelog`'s `voice_sessions` against `gamelog`'s `sessions` on matching `user_id` with overlapping time windows, to find what game (if any) someone was playing during a given voice session.

Since the two cogs keep separate database files (each in its own cog data folder), that second join needs both files open at once (e.g. SQLite's `ATTACH DATABASE`). Those joins are the intended basis for building a relationship graph from this data.

## Privileged intents

`gamelog` needs the **Presence Intent** and **Server Members Intent** enabled for the bot, both in the [Discord developer portal](https://discord.com/developers/applications) (under your application's Bot settings) and in Red's own intents config (`redbot-setup` lets you toggle these, or edit the instance's intents). Without both, `gamelog` will never see game activity changes and will log nothing.

`voicelog` needs the **Voice States Intent**. It isn't a privileged intent, but Red's default intents config can still have it turned off - check the same intents settings as above.

## Contact

Open an issue on this repository for bugs or feature requests.

## Credits

Built following the [Red-DiscordBot cog creation guide](https://docs.discord.red/en/stable/guide_cog_creation.html) and [publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html).
