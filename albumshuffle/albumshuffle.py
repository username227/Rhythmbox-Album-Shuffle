"""Continuous Album Shuffle for Rhythmbox 3.5.x."""
import logging
import random
from collections import defaultdict, deque

from gi.repository import GObject, Gio, GLib, Gtk, Gdk, GdkPixbuf, Peas, RB

LOG = logging.getLogger('rhythmbox-album-shuffle')
PLAYLIST_NAME = 'Continuous Album Shuffle'
BATCH_ALBUMS = 12


def group_albums(entries):
    """Group by album title and optional album artist; sort by disc/track."""
    grouped = defaultdict(list)
    for entry in entries:
        album = (entry.get_string(RB.RhythmDBPropType.ALBUM) or '').strip()
        if not album or album.lower() in ('unknown', 'unknown album'):
            continue
        # Never fall back to the individual track artist. Compilation albums
        # commonly contain different artists while having no Album Artist tag.
        album_artist = (entry.get_string(RB.RhythmDBPropType.ALBUM_ARTIST) or '').strip()
        # Same title with distinct Album Artist values stays separate;
        # missing Album Artist groups all same-titled tracks together.
        key = (album_artist.casefold(), album.casefold())
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
        self.button_wrapper = None
        self.button_fallback_row = None
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

    def _find_shuffle_button(self, widget):
        """Look for Rhythmbox's actual GTK shuffle control, not a position."""
        try:
            if isinstance(widget, Gtk.Button):
                action_name = widget.get_action_name() if isinstance(widget, Gtk.Actionable) else None
                tooltip = (widget.get_tooltip_text() or '').lower()
                widget_name = (widget.get_name() or '').lower()
                # GTK may expose the GAction name, a tooltip or a widget name.
                if action_name in ('app.play-shuffle', 'app.play-shuffle(true)', 'app.play-shuffle(false)'):
                    return widget
                if 'shuffle' in tooltip or 'shuffle' in widget_name:
                    return widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    found = self._find_shuffle_button(child)
                    if found is not None:
                        return found
        except Exception:
            LOG.exception('Album Shuffle: inspecting GTK control hierarchy')
        return None

    def _shuffle_icon_size(self, shuffle_button):
        """Derive icon size from the actual Rhythmbox Shuffle image."""
        def find_image(widget):
            if isinstance(widget, Gtk.Image):
                return widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    result = find_image(child)
                    if result is not None:
                        return result
            return None

        icon = find_image(shuffle_button) if shuffle_button is not None else None
        if icon is not None:
            pixels = icon.get_pixel_size()
            if pixels > 0:
                return pixels
            # Resolved size of a named Gtk.IconSize (not the button's padding).
            try:
                ok, width, height = Gtk.icon_size_lookup(icon.get_icon_size())
                if ok and width > 0:
                    return width
            except Exception:
                pass
            width = icon.get_allocated_width()
            if width > 1:
                return width
        return 20

    def _draw_cd_overlay(self, area, cr):
        """A clear concentric-ring CD replacing the lower-right arrowhead."""
        import math
        alloc = area.get_allocation()
        size = min(alloc.width, alloc.height)
        if size <= 1:
            return False
        fg = area.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        # GTK sometimes returns transparent/black widget backgrounds. Derive
        # a contrasting light/dark face from the theme text color instead.
        lum = 0.2126 * fg.red + 0.7152 * fg.green + 0.0722 * fg.blue
        dark = (fg.red, fg.green, fg.blue)
        light = (0.96, 0.96, 0.96) if lum < 0.5 else (0.08, 0.08, 0.08)
        # Nudge the disc slightly left so its right edge stays round
        # instead of being clipped by the overlay allocation.
        cx, cy, radius = size * 0.71, size * 0.78, size * 0.285
        # Opaque knockout and disc body hide the original lower arrowhead.
        cr.set_source_rgb(*light)
        cr.arc(cx, cy, radius + size * 0.055, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(*dark)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
        # Broad, high contrast ring, followed by an unmistakable dark hub.
        cr.set_source_rgb(*light)
        cr.arc(cx, cy, radius * 0.52, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(*dark)
        cr.arc(cx, cy, radius * 0.21, 0, 2 * math.pi)
        cr.fill()
        return False

    def _draw_combined_icon(self, area, cr, icon_size):
        """Paint both theme shuffle icon and CD on one non-windowed widget."""
        import os
        theme = Gtk.IconTheme.get_default()
        pixbuf = None
        for name in ('media-playlist-shuffle-symbolic', 'media-playlist-shuffle'):
            if not theme.has_icon(name):
                continue
            try:
                pixbuf = theme.load_icon(name, icon_size, Gtk.IconLookupFlags.FORCE_SIZE)
                break
            except GLib.Error:
                continue
        if pixbuf is None:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    os.path.join(os.path.dirname(__file__), 'album-shuffle.svg'),
                    icon_size, icon_size, True)
            except GLib.Error:
                return False
        alloc = area.get_allocation()
        x = max(0, (alloc.width - pixbuf.get_width()) / 2)
        y = max(0, (alloc.height - pixbuf.get_height()) / 2)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, x, y)
        cr.paint()
        self._draw_cd_overlay(area, cr)
        return False

    def _make_icon_button(self, shuffle_button=None):
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_tooltip_text('Shuffle Albums — start a fresh random album sequence')
        icon_size = self._shuffle_icon_size(shuffle_button)
        canvas = Gtk.DrawingArea()
        canvas.set_size_request(icon_size, icon_size)
        canvas.connect('draw', self._draw_combined_icon, icon_size)
        canvas.connect('style-updated', lambda widget: widget.queue_draw())
        Gtk.IconTheme.get_default().connect('changed',
                lambda theme: canvas.queue_draw())
        button.add(canvas)
        button.connect('clicked', self.on_start)
        return button

    def _install_button(self):
        """Insert beside Shuffle when its GTK container is available.

        Otherwise use a small icon button in the original upper row; never
        relocate, remove, or alter the existing playback control itself.
        """
        try:
            window = self.object.props.window
            shuffle_button = self._find_shuffle_button(window)
            button = self._make_icon_button(shuffle_button)
            if shuffle_button is not None:
                parent = shuffle_button.get_parent()
                if isinstance(parent, Gtk.Box):
                    children = parent.get_children()
                    position = children.index(shuffle_button) + 1
                    parent.pack_start(button, False, False, 0)
                    parent.reorder_child(button, position)
                    self.button_container = parent
                    self.button = button
                    button.show_all()
                    LOG.warning('Album Shuffle: inserted immediately after Shuffle')
                    return
                if isinstance(parent, Gtk.Toolbar) and isinstance(shuffle_button, Gtk.ToolItem):
                    item = Gtk.ToolItem()
                    item.add(button)
                    parent.insert(item, parent.get_item_index(shuffle_button) + 1)
                    self.button_container = parent
                    self.button_wrapper = item
                    self.button = button
                    item.show_all()
                    LOG.warning('Album Shuffle: inserted GTK tool item after Shuffle')
                    return
                LOG.warning('Album Shuffle: shuffle control has unsupported parent %s', type(parent).__name__)
            else:
                LOG.warning('Album Shuffle: shuffle control not located; using fallback')
            titlebar = window.get_titlebar()
            if isinstance(titlebar, Gtk.HeaderBar):
                titlebar.pack_end(button)
                self.button_container = titlebar
            else:
                child = window.get_child()
                if not isinstance(child, Gtk.Box):
                    button.destroy()
                    LOG.warning('Album Shuffle: no suitable fallback location; Tools menu remains available')
                    return
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                row.set_margin_start(8)
                row.set_margin_end(8)
                row.pack_end(button, False, False, 0)
                child.pack_start(row, False, False, 0)
                child.reorder_child(row, 0)
                self.button_container = row
                self.button_fallback_row = row
            self.button = button
            button.show_all()
            if self.button_container is not titlebar:
                self.button_container.show_all()
        except Exception:
            LOG.exception('Album Shuffle: unable to attach button; Tools menu still available')

    def _remove_button(self):
        if self.button is None:
            return
        try:
            if self.button_wrapper is not None:
                self.button_wrapper.destroy()
            elif self.button_container is not None:
                parent = self.button.get_parent()
                if parent is not None:
                    parent.remove(self.button)
                # Only delete fallback rows created by the plugin.
                if getattr(self, 'button_fallback_row', None) is not None:
                    self.button_fallback_row.destroy()
                    self.button_fallback_row = None
        except Exception:
            LOG.exception('Album Shuffle: removing button')
        self.button = None
        self.button_container = None
        self.button_wrapper = None

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

    def _get_or_create_playlist(self, manager):
        """Reuse our generated playlist instead of creating another every run.

        Resolve the saved playlist by name after a Rhythmbox restart, too.
        Don't remove unrelated playlists or alter the music library.
        """
        if self.playlist is not None:
            return self.playlist
        for candidate in manager.get_playlists():
            if (isinstance(candidate, RB.StaticPlaylistSource) and
                    candidate.props.name == PLAYLIST_NAME):
                self.playlist = candidate
                return candidate
        self.playlist = manager.new_playlist(PLAYLIST_NAME, False)
        return self.playlist

    def _clear_generated_playlist(self, playlist):
        """Remove entries only from the plugin's own playlist."""
        model = playlist.get_query_model()
        if model is None:
            return
        # Snapshot first; removing entries modifies the query model.
        for entry in [row[0] for row in model]:
            playlist.remove_entry(entry)

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

            # Playback must stop before we replace tracks in the playlist
            # that might currently be feeding the player.
            self.player.set_playback_state(False, False)
            manager = shell.props.playlist_manager
            self.playlist = self._get_or_create_playlist(manager)
            self._clear_generated_playlist(self.playlist)
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
