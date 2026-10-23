"""Continuous Album Shuffle for Rhythmbox 3.5.x (experimental)."""
import logging
import random
from collections import defaultdict, deque

from gi.repository import GObject, Gio, GLib, Gtk, Peas, RB

LOG = logging.getLogger('rhythmbox-album-shuffle')
PLAYLIST_NAME = 'Continuous Album Shuffle'
BATCH_ALBUMS = 12


def group_albums(entries):
    """Group by album artist + album, order tracks by disc/track numbers."""
    grouped = defaultdict(list)
    for entry in entries:
        album = (entry.get_string(RB.RhythmDBPropType.ALBUM) or '').strip()
        if not album or album.lower() in ('unknown', 'unknown album'):
            continue
        artist = (entry.get_string(RB.RhythmDBPropType.ALBUM_ARTIST) or
                  entry.get_string(RB.RhythmDBPropType.ARTIST) or '').strip()
        # Album-artist + title avoids merging identically named albums of different artists.
        key = (artist.casefold(), album.casefold())
        grouped[key].append(entry)
    for key, tracks in grouped.items():
        tracks.sort(key=lambda e: (
            int(e.get_ulong(RB.RhythmDBPropType.DISC_NUMBER)),
            int(e.get_ulong(RB.RhythmDBPropType.TRACK_NUMBER)),
            (e.get_string(RB.RhythmDBPropType.LOCATION) or '')))
    return grouped


class ContinuousAlbumShuffle(GObject.Object, Peas.Activatable):
    __gtype_name__ = 'ContinuousAlbumShufflePlugin'
    object = GObject.Property(type=GObject.Object)

    def __init__(self):
        GObject.Object.__init__(self)
        self.action = None
        self.app = None
        self.player = None
        self.handler = None
        self.playlist = None
        self.deck = deque()
        self.albums = {}
        self.album_ends = set()
        self.replenishing = False
        self.scheduled_count = 0
        self.completed_count = 0
        self.active = False
        self.button = None
        self.button_container = None
        self.last_source = None
        self.pending_follow_id = None

    def do_activate(self):
        self.app = Gio.Application.get_default()
        self.player = self.object.props.shell_player
        self.action = Gio.SimpleAction.new('album-shuffle-start', None)
        self.action.connect('activate', self.on_start)
        self.app.add_action(self.action)
        menu = Gio.MenuItem.new('Start / Reshuffle Albums', 'app.album-shuffle-start')
        self.app.add_plugin_menu_item('tools', 'album-shuffle-start', menu)
        self.handler = self.player.connect('playing-song-changed', self.on_song_changed)
        self._install_button()
        LOG.warning('Album Shuffle enabled: select Tools → Start / Reshuffle Albums')

    def do_deactivate(self):
        self.active = False
        if self.pending_follow_id is not None:
            GLib.source_remove(self.pending_follow_id)
            self.pending_follow_id = None
        self._remove_button()
        if self.handler is not None:
            self.player.disconnect(self.handler)
            self.handler = None
        if self.app:
            self.app.remove_plugin_menu_item('tools', 'album-shuffle-start')
            self.app.remove_action('album-shuffle-start')
        self.action = None

    def _install_button(self):
        """Prefer a persistent one-click button in the main window titlebar."""
        try:
            window = self.object.props.window
            button = Gtk.Button.new_with_label('Shuffle Albums')
            button.set_tooltip_text('Shuffle albums in the currently displayed filtered selection')
            button.connect('clicked', self.on_start)
            titlebar = window.get_titlebar()
            if isinstance(titlebar, Gtk.HeaderBar):
                titlebar.pack_end(button)
                self.button_container = titlebar
            else:
                # Some Rhythmbox themes/layouts have no GtkHeaderBar.  Use
                # a compact row in the existing main GtkBox when possible.
                child = window.get_child()
                if isinstance(child, Gtk.Box):
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    row.set_margin_start(8)
                    row.set_margin_end(8)
                    row.pack_end(button, False, False, 0)
                    child.pack_start(row, False, False, 0)
                    child.reorder_child(row, 0)
                    self.button_container = row
                else:
                    LOG.warning('Album Shuffle: cannot add button to this window layout; use Tools menu')
                    button.destroy()
                    return
            button.show_all()
            self.button = button
            if self.button_container is not titlebar:
                self.button_container.show_all()
        except Exception:
            LOG.exception('Album Shuffle: unable to attach interface button; Tools menu remains available')

    def _remove_button(self):
        if self.button is not None:
            try:
                container = self.button_container
                if isinstance(container, Gtk.HeaderBar):
                    container.remove(self.button)
                elif container is not None:
                    container.destroy()
            except Exception:
                LOG.exception('Album Shuffle: removing button')
            self.button = None
            self.button_container = None

    def _current_filtered_source(self):
        page = self.object.props.selected_page
        if page is self.playlist and self.last_source is not None:
            # If the user explicitly navigated to the generated playlist,
            # reuse the last filtered source for reshuffling.
            page = self.last_source
        if page is None or not isinstance(page, RB.Source):
            return None
        if page is self.playlist:
            return None
        return page

    def on_start(self, *_args):
        try:
            shell = self.object
            source = self._current_filtered_source()
            if source is None:
                LOG.warning('Album Shuffle: select Music or a playlist first')
                return
            # Take the model from the actual onscreen song list.  A source's
            # query-model may differ from the EntryView model after browser or
            # search filtering, depending on Rhythmbox's source implementation.
            entry_view = source.get_entry_view()
            model = entry_view.props.model if entry_view is not None else None
            if model is None:
                LOG.warning('Album Shuffle: no visible song list for source %s',
                            source.props.name)
                return
            entries = [row[0] for row in model]
            source_model = source.props.query_model
            try:
                source_count = sum(1 for _ in source_model) if source_model else -1
                base_model = source.props.base_query_model
                base_count = sum(1 for _ in base_model) if base_model else -1
            except Exception:
                source_count = base_count = -1
            LOG.warning('Album Shuffle filter snapshot: page=%s displayed=%d source=%d base=%d',
                        source.props.name, len(entries), source_count, base_count)
            grouped = group_albums(entries)
            if not grouped:
                LOG.warning('Album Shuffle: no tagged albums in this filtered view')
                return
            self.last_source = source
            self.albums = grouped
            keys = list(grouped)
            random.SystemRandom().shuffle(keys)
            self.deck = deque(keys)
            self.album_ends = set()
            self.scheduled_count = 0
            self.completed_count = 0
            self.active = False

            manager = shell.props.playlist_manager
            self.playlist = manager.new_playlist(PLAYLIST_NAME, False)
            LOG.warning('Album Shuffle selection after playlist creation: %s',
                        getattr(shell.props.selected_page.props, 'name', '?')
                        if shell.props.selected_page is not None else '(none)')
            first_entry = None
            for _ in range(min(BATCH_ALBUMS, len(self.deck))):
                first = self._append_album()
                if first_entry is None:
                    first_entry = first
            if first_entry is None:
                return
            self.player.set_playback_state(False, False)
            # Keep the selected Music/genre/artist view unchanged.
            # play_entry accepts an independent source for playback.
            self.active = True
            self.player.play_entry(first_entry, self.playlist)
            # Creating a playlist can change the selected display page.
            # Restore the Music browser AFTER starting playback, leaving the
            # generated playlist as the playing source.
            GLib.idle_add(self._restore_browser_view, source, first_entry)
            LOG.warning('Album Shuffle: %d albums from current filter, %d tracks; first album: %s',
                        len(keys), len(entries), first_entry.get_string(RB.RhythmDBPropType.ALBUM))
        except Exception:
            self.active = False
            LOG.exception('Could not start album shuffle')

    def _restore_browser_view(self, source, first_entry):
        try:
            current = self.object.props.selected_page
            LOG.warning('Album Shuffle page before restore: %s; desired: %s',
                        current.props.name if current else '(none)', source.props.name)
            if current is not source:
                self.object.props.display_page_tree.select(source)
            LOG.warning('Album Shuffle page after restore: %s',
                        self.object.props.selected_page.props.name
                        if self.object.props.selected_page else '(none)')
            self._queue_follow(first_entry)
        except Exception:
            LOG.exception('Album Shuffle could not restore the filtered browser view')
        return GLib.SOURCE_REMOVE

    def _append_album(self):
        if not self.deck:
            keys = list(self.albums)
            random.SystemRandom().shuffle(keys)
            self.deck = deque(keys)
        key = self.deck.popleft()
        self.scheduled_count += 1
        tracks = self.albums[key]
        for entry in tracks:
            self.playlist.add_entry(entry, -1)
        last = tracks[-1].get_string(RB.RhythmDBPropType.LOCATION)
        self.album_ends.add(last)
        return tracks[0]

    def _queue_follow(self, entry):
        """Schedule scrolling after the entry view has finished updating."""
        if self.pending_follow_id is not None:
            GLib.source_remove(self.pending_follow_id)
            self.pending_follow_id = None
        self.pending_follow_id = GLib.idle_add(self._follow_entry, entry)

    def _follow_entry(self, entry):
        self.pending_follow_id = None
        try:
            source = self.last_source
            # Do not interfere if the user is viewing another source.
            if not self.active or source is None or self.object.props.selected_page is not source:
                return GLib.SOURCE_REMOVE
            view = source.get_entry_view()
            if view is None or not view.get_entry_contained(entry):
                # User may have changed the filter while playback continues.
                return GLib.SOURCE_REMOVE
            view.select_none()
            view.select_entry(entry)
            view.scroll_to_entry(entry)
        except Exception:
            LOG.exception('Album Shuffle: could not follow the playing track')
        return GLib.SOURCE_REMOVE

    def on_song_changed(self, _player, entry):
        if not self.active or entry is None or self.replenishing:
            return
        try:
            if self.player.get_playing_source() != self.playlist:
                self.active = False
                return
            self._queue_follow(entry)
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if uri not in self.album_ends:
                return
            # When the last track of an album starts, add another album.
            # This keeps playback continuous without handling end-of-stream
            # ourselves or skipping the first track of the next album.
            self.album_ends.discard(uri)
            self.completed_count += 1
            # Do not begin a new cycle until all previously scheduled
            # albums have actually finished, even if the deck is empty.
            if not self.deck and self.completed_count < self.scheduled_count:
                return
            self.replenishing = True
            GLib.idle_add(self._append_next_idle)
        except Exception:
            LOG.exception('Album Shuffle continuation failed')

    def _append_next_idle(self):
        try:
            if self.active and self.playlist is not None:
                self._append_album()
        except Exception:
            LOG.exception('Unable to append next random album')
        finally:
            self.replenishing = False
        return GLib.SOURCE_REMOVE
