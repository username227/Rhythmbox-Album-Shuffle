# Rhythmbox-Album-Shuffle
This is a plugin for Rhythmbox. It will add the option to shuffle by albums to Rhythmbox. 

# Features
- Adds a Shuffle Albums a button above the menu to initiate the album shuffle playback.
- Album shuffle can also be started by selecting Tools → Start / Reshuffle Albums.
- Each time album shuffle is started or restarted, the album order is randomized.
- Playback begins at the first track of a randomly selected album.
- Each album plays from beginning to end, in the correct disc and track order, before moving on to another randomly selected album.
- Albums are not repeated until all eligible albums have been played, at which point the sequence is reshuffled.
- Pressing the Shuffle Albums button again generates a new randomized sequence and starts playback over.
- Albums play in their proper track order regardless of how songs are sorted in Rhythmbox's library display. Changing the displayed sort order does not affect the playback sequence.
- The plugin respects the currently displayed library filters. For example, if you select a particular genre, only tracks matching that genre are included in the shuffle.
- The currently playing song is automatically highlighted and scrolled into view in the song list, provided it is included in the current filtered view.
- The plugin preserves the current library view and filters while playing.

# Installation
Install the <i>albumshuffle</i> folder to either <code> /usr/lib/rhythmbox/plugins</code> or <code> ~/.local/share/rhythmbox/plugins</code>. For the flatpak version, install into <code>~/.var/app/org.gnome.Rhythmbox3/data/rhythmbox/plugins</code>

# Full Disclosure
  I am not a developer and am not formally trained in computer programming. AI was used to assist in the writing of the code based on my specifications, testing, and feedback. I have tested various development versions on  Arch Linux with Rhythmbox 3.5.1. I have not tested the plugin on other distributions. Review and improving of the code base, as well as additional feature requests, are welcome.

## Please Note
  The plugin is python-based. The issues regarding the breaking of the python-based Rhythmbox plugins was well known, and they were broken in the past for a significant amount of time. Those upstream issues appear to have been rectified now. However, future changes of  Rhythmbox or its dependencies could cause changes that affect this plugin, particularly if those changes break python-loader. Hopefully this will not happen, but if it does, this plugin could break.
