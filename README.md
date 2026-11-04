# Rhythmbox-Album-Shuffle

This plugin adds **Continuous Album Shuffle** to Rhythmbox, playing complete albums in randomized order while preserving track order within each album.

## Features

- Adds a **Shuffle Albums** button next to Rhythmbox's shuffle button. Album shuffle can also be started using **Tools → Start / Reshuffle Albums**.
- Each time an album shuffle is started or restarted, the album order is randomized.
- Playback begins at the first track of a randomly selected album. Each album plays in disc and track order before the next album begins, regardless of the order displayed in the music library.
- Albums do not repeat until all eligible groups have played; then another randomized sequence begins.
- **Every eligible track from the current filtered selection is included in the generated playlist.** There is no arbitrary album or track-count limit.
- For large music libraries, the playlist is populated incrementally to maintain Rhythmbox responsiveness. Playback can begin before playlist generation is complete.
- The plugin respects the currently displayed library filters. For example, selecting Country includes only eligible tracks from that genre.
- The current library view and filters are preserved while playing. The playing track is highlighted and scrolled into view when present in the selected view.
- Starting a new shuffle replaces the previously generated playlist. **To preserve an existing playlist, rename it before reshuffling.**
- The plugin saves shuffle-session information to support recovery across Rhythmbox restarts. Exact playback-position restoration is handled separately by the Session Restore plugin.

## Shuffling Methodology

Album Shuffle groups tracks primarily by the **Album** tag and uses **Album Artist** to distinguish albums with identical titles, such as *Greatest Hits*.

If Album Artist is blank, tracks sharing the same Album title are grouped together regardless of their individual Artist tags or folder locations.

### Blank or Unknown Album Tags

Some collections deliberately leave the Album tag blank. Album Shuffle supports these tracks without modifying their metadata.

Open **Preferences → Plugins → Album Shuffle → Preferences** to choose whether they participate:

- **Include tracks with blank or Unknown Album tags (default):** Group these tracks by their containing folders and include them in the album shuffle. Each folder becomes a separate playback group.
- **Exclude tracks with blank or Unknown Album tags:** Omit those tracks from the generated playlist.

This preference also applies to Album tags reading `Unknown` or `Unknown Album`. The setting is saved between Rhythmbox sessions, and changes take effect on the **next reshuffle**, without changing the existing playlist.

## Installation

For a **user-level installation**, copy the `albumshuffle` plugin folder into:

`~/.local/share/rhythmbox/plugins/`

For a **system-wide installation**, the location depends on your distribution:

- **Arch Linux:** `/usr/lib/rhythmbox/plugins/`
- **Debian/Ubuntu/Mint (x86_64):** `/usr/lib/x86_64-linux-gnu/rhythmbox/plugins/`
- **Debian/Ubuntu/Mint (ARM64):** `/usr/lib/aarch64-linux-gnu/rhythmbox/plugins/`

For **Flatpak**, use:

`~/.var/app/org.gnome.Rhythmbox3/data/rhythmbox/plugins/`

Restart Rhythmbox, open **Preferences → Plugins**, and enable the plugin.

## Full Disclosure

I am not a developer and am not formally trained in computer programming. AI was used to assist in the writing of the code based on my specifications, testing, and feedback. I have tested various development versions on Arch Linux with Rhythmbox 3.5.1. I have not tested the plugin on other distributions. Reviews and improvements to the codebase, as well as additional feature requests, are welcome.

### Please Note

The plugin is Python-based. Issues affecting Python-based Rhythmbox plugins have broken them in the past for significant periods of time. Those upstream issues appear to have been resolved, but future changes to Rhythmbox or its dependencies, particularly the Python plugin loader, could break this plugin.
