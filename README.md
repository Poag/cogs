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

## Privileged intents

`gamelog` needs the **Presence Intent** and **Server Members Intent** enabled for the bot, both in the [Discord developer portal](https://discord.com/developers/applications) (under your application's Bot settings) and in Red's own intents config (`redbot-setup` lets you toggle these, or edit the instance's intents). Without both, the cog will never see game activity changes and will log nothing.

## Contact

Open an issue on this repository for bugs or feature requests.

## Credits

Built following the [Red-DiscordBot cog creation guide](https://docs.discord.red/en/stable/guide_cog_creation.html) and [publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html).
