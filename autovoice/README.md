# AutoVoice

This cog facilitates automatic voice channel creation. When a member joins an AutoVoice Source (voice channel), this cog will move them to a brand new voice channel that they have control over. Once everyone leaves it, it is automatically deleted.

This is a from-scratch reimplementation of PhasecoreX's `autoroom` cog, with its optional legacy companion text channel feature dropped - Discord's own voice channel text chat makes it obsolete. See "Migrating from AutoRoom" in the repo root README for how to move an existing AutoRoom setup over.

## For Members - `[p]autovoice`

Once you join an AutoVoice Source, you will be moved into a brand new voice channel. This is your room, you can do whatever you want with it. Use the `[p]autovoice` command (alias `[p]vc`) to check out all the different things you can do. Some examples include:

-   Check its current settings with `[p]autovoice settings`
-   Make it a public room with `[p]autovoice public` (everyone can see and join your room)
-   Make it a locked room with `[p]autovoice locked` (everyone can see, but nobody can join your room)
-   Make it a private room with `[p]autovoice private` (nobody can see and join your room)
-   Kick/ban users (or entire roles) from your room with `[p]autovoice deny` (useful for public rooms)
-   Allow users (or roles) into your room with `[p]autovoice allow` (useful for locked and private rooms)

When everyone leaves your room, it will automatically be deleted.

## For Server Admins - `[p]autovoiceset`

Start by having a voice channel, and a category (the voice channel does not need to be in the category, but it can be if you want). The voice channel will be the "AutoVoice Source", where your members will join and then be moved into their own room. These rooms will be created in the category that you choose.

```
[p]autovoiceset create <source_voice_channel> <dest_category>
```

This command will guide you through setting up an AutoVoice Source by asking some questions. If you get a warning about missing permissions, take a look at `[p]autovoiceset permissions`, grant the missing permissions, and then run the command again. Otherwise, answer the questions, and you'll have a new AutoVoice Source. Give it a try by joining it: if all goes well, you will be moved to a new room, where you can do all of the `[p]autovoice` commands.

There are some additional configuration options for AutoVoice Sources that you can set by using `[p]autovoiceset modify`. You can also check out `[p]autovoiceset access`, which controls whether admins (default yes) or moderators (default no) can see and join private rooms. For an overview of all of your settings, use `[p]autovoiceset settings`.

#### Member Roles and Hidden Sources

The AutoVoice Source will behave in a certain way, depending on what permissions it has. If the `@everyone` role is denied the connect permission (and optionally the view channel permission), the AutoVoice Source and its rooms will behave in a member role style. Any roles that are allowed to connect on the AutoVoice Source will be considered the "member roles". Only members of the server with one or more of these roles will be able to utilize these rooms and their Source.

For hidden AutoVoice Sources, you can deny the view channel permission for the `@everyone` role, but still allow the connect permission. The members won't be able to see the AutoVoice Source, but any rooms it creates they will be able to see (depending on if it isn't a private room). Ideally you would have a role that is allowed to see the AutoVoice Source, that role is allowed to create rooms, but then can invite anyone to their rooms.

You can of course do both of these, where the `@everyone` role is denied view channel and connect, and your member role is denied the view channel permission, but is allowed the connect permission. Non-members will never see the AutoVoice Source and its rooms, and the members will not see the AutoVoice Source, but will see the rooms.

#### Templates

The default room name format is based on the room owner's username. Using `[p]autovoiceset modify name`, you can choose a default format, or you can set a custom format. For custom formats, you have a couple of variables you can use in your template:

-   `{{username}}` - The room owner's username
-   `{{game}}` - The room owner's game they were playing when the room was created, or blank if they were not playing a game (requires the Presence intent, see the repo root README's "Privileged intents" section)
-   `{{dupenum}}` - A number, starting at 1, that will increment when the generated room name would be a duplicate of an already existing room.

You are also able to use `if`/`elif`/`else`/`endif` statements in order to conditionally show or hide parts of your template. Here are the templates for the two default formats included:

-   `username` - `{{username}}'s Room{% if dupenum > 1 %} ({{dupenum}}){% endif %}`
-   `game` - `{{game}}{% if not game %}{{username}}'s Room{% endif %}{% if dupenum > 1 %} ({{dupenum}}){% endif %}`

The username format is pretty self-explanatory: put the username, along with "'s Room" after it. For the game format, we put the game name, and then if there isn't a game, show `{{username}}'s Room` instead. Remember, if no game is being played, `{{game}}` won't return anything.

The last bit of both of these is `{% if dupenum > 1 %} ({{dupenum}}){% endif %}`. With this, we are checking if `dupenum` is greater than 1. If it is, we display ` ({{dupenum}})` at the end of our room name. This way, only duplicate named rooms will ever get a ` (2)`, ` (3)`, etc. appended to them, no ` (1)` will be shown.

Finally, you can use filters in order to format your variables. They are specified by adding a pipe, and then the name of the filter. The following are the currently implemented filters:

-   `{{username | lower}}` - Will lowercase the variable, the username in this example
-   `{{game | upper}}` - Will uppercase the variable, the game name in this example

This template format can also be used for the "channel hint" message sent into a newly created room's own chat (`[p]autovoiceset modify hint set`). For that, you can also use this variable:

-   `{{mention}}` - The room owner's mention

## Migrating from AutoRoom

If you're switching over from PhasecoreX's `autoroom` cog, see the "Migrating from AutoRoom" section in this repo's root README for the `[p]autovoiceset migrate` workflow. In short: keep `autoroom` loaded once so its own migrations have run, `[p]unload autoroom`, `[p]load autovoice`, then run `[p]autovoiceset migrate` in each server that used it. It's safe to run more than once.
