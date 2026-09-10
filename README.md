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
| `gamelog` | Logs what games members are playing and what voice channels they're in, per server, and how long each session lasts. Ignores non-game statuses (Spotify, custom statuses, etc). Requires the Presence, Server Members, and Voice States intents. |

### gamelog commands

| Command | Description |
| --- | --- |
| `[p]gamelog playtime [member]` | Total logged game time per game for a member (yourself by default). |
| `[p]gamelog top <game>` | Top 10 players of a specific game, by time played. |
| `[p]gamelog leaderboard` | Top 10 players across all games combined, by time played. |
| `[p]gamelog games` | Every game logged in the server, sorted by total time played. |
| `[p]gamelog voicetime [member]` | Total logged voice channel time per channel for a member (yourself by default). |

### Voice logging and relationship data

Every voice channel join/leave is logged as a `(guild, user, channel, start, end, duration)` row in `voice_sessions`, the same shape as the game log's `sessions` table. This cog doesn't compute "who was with whom" or "what game were they playing while in voice" itself - both are derivable later by joining:

- `voice_sessions` against itself on matching `channel_id` with overlapping `[start_time, end_time]` windows, to find who shared a voice channel and for how long, and
- `voice_sessions` against `sessions` on matching `user_id` with overlapping time windows, to find what game (if any) someone was playing during a given voice session.

Those joins are the basis for building a relationship graph from this data.

## Privileged intents

`gamelog` needs the **Presence Intent**, **Server Members Intent**, and **Voice States Intent** enabled for the bot, both in the [Discord developer portal](https://discord.com/developers/applications) (under your application's Bot settings) and in Red's own intents config (`redbot-setup` lets you toggle these, or edit the instance's intents). Without these, the cog will never see game or voice activity changes and will log nothing. Voice States is not a privileged intent, but Red's default intents can still have it turned off.

## Contact

Open an issue on this repository for bugs or feature requests.

## Credits

Built following the [Red-DiscordBot cog creation guide](https://docs.discord.red/en/stable/guide_cog_creation.html) and [publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html).
