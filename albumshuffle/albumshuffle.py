"""Continuous Album Shuffle for Rhythmbox 3.5.x."""
import json
import logging
import os
import random
import tempfile
from collections import defaultdict, deque

import cairo

from gi.repository import GObject, Gio, GLib, Gtk, Gdk, GdkPixbuf, Peas, PeasGtk, RB

LOG = logging.getLogger('rhythmbox-album-shuffle')
PLAYLIST_NAME = 'Continuous Album Shuffle'
# INITIAL_GROUPS is a startup prefill, NOT an eligibility or playlist limit.
# Remaining groups are inserted during GTK idle cycles until EVERY track is present.
INITIAL_GROUPS = 12
FULL_GROUPS_PER_IDLE = 16
FULL_TRACKS_PER_IDLE = 250
# Only Album Shuffle needs this preference; Folder Shuffle groups by file path.
CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), 'rhythmbox', 'albumshuffle.json')


def read_include_unknown():
    """Include unknown-album tracks by default; preserve explicit user choice."""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as stream:
            setting = json.load(stream)
        value = setting.get('include_unknown_albums', True)
        if isinstance(value, bool):
            return value
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return True


def save_include_unknown(value):
    """Persist the preference atomically so it survives Rhythmbox restarts."""
    directory = os.path.dirname(CONFIG_FILE)
    temp_path = None
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=directory,
                prefix='.albumshuffle-', delete=False) as stream:
            temp_path = stream.name
            json.dump({'include_unknown_albums': bool(value)}, stream, indent=2)
            stream.write('\n')
        os.replace(temp_path, CONFIG_FILE)
    except OSError:
        LOG.exception('Album Shuffle: unable to save unknown-album preference')
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except OSError:
                LOG.debug('Album Shuffle: could not remove temporary settings file',
                          exc_info=True)


def group_albums(entries, include_unknown=True):
    """Group tagged albums normally; optionally use folders for unknown albums.

    Keep the existing two-field keys for tagged albums, so saved shuffle
    sessions remain compatible. Unknown-album groups use a distinct three-field
    key, which cannot collide with any regular album.
    """
    grouped = defaultdict(list)
    for entry in entries:
        album = (entry.get_string(RB.RhythmDBPropType.ALBUM) or '').strip()
        if not album or album.casefold() in ('unknown', 'unknown album'):
            if not include_unknown:
                continue
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION) or ''
            if not uri:
                LOG.warning('Album Shuffle: skipping an unknown-album track with no location')
                continue
            try:
                file = Gio.File.new_for_uri(uri)
                parent = file.get_parent()
                folder_uri = parent.get_uri() if parent is not None else uri
            except (GLib.Error, ValueError, TypeError):
                # Retain the track as an individual group if its parent URI
                # could not be interpreted; never collapse unrelated tracks.
                folder_uri = uri
            key = ('__unknown_album_folder__', folder_uri, '')
        else:
            album_artist = (entry.get_string(RB.RhythmDBPropType.ALBUM_ARTIST) or '').strip()
            key = (album_artist.casefold(), album.casefold())
        grouped[key].append(entry)
    for tracks in grouped.values():
        tracks.sort(key=lambda e: (
            int(e.get_ulong(RB.RhythmDBPropType.DISC_NUMBER)),
            int(e.get_ulong(RB.RhythmDBPropType.TRACK_NUMBER)),
            (e.get_string(RB.RhythmDBPropType.LOCATION) or '')))
    return grouped


class ContinuousAlbumShuffle(GObject.Object, Peas.Activatable, PeasGtk.Configurable):
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
        self._full_playlist_idle = None
        self._full_playlist_expected_tracks = 0
        self._view_callback_epoch = 0
        self._bookmark = None
        self._resume_dialog = None
        self._resume_action = None
        self._shuffle_start_entry = None

    def do_create_configure_widget(self):
        """Displayed by Rhythmbox Preferences -> Plugins -> Configure."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        panel.set_border_width(12)

        heading = Gtk.Label(label='Tracks without an album name')
        heading.set_xalign(0)
        panel.pack_start(heading, False, False, 0)

        include = Gtk.CheckButton.new_with_label(
            'Include tracks with blank or Unknown Album tags')
        include.set_active(read_include_unknown())
        panel.pack_start(include, False, False, 0)

        explanation = Gtk.Label(label=(
            'Blank Album tags can be intentional. When enabled, tracks without a named album are grouped '
            'by their containing folder and included in the complete '
            'shuffled playlist. Tagged albums continue to be grouped '
            'normally. When disabled, those tracks are excluded.'))
        explanation.set_xalign(0)
        explanation.set_line_wrap(True)
        explanation.set_max_width_chars(62)
        panel.pack_start(explanation, False, False, 0)

        note = Gtk.Label(label=(
            'Saved immediately; the choice takes effect on the next '
            'Start / Reshuffle Albums, not in an already playing playlist.'))
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.set_max_width_chars(62)
        panel.pack_start(note, False, False, 4)

        include.connect('toggled', lambda button: save_include_unknown(button.get_active()))
        panel.show_all()
        return panel

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
        # This call must be inside the registered Peas.Activatable vfunc: changing
        # do_activate after GObject type registration does not update its vfunc.
        self._resume_activate()
        LOG.warning('Album Shuffle enabled: select Tools → Start / Reshuffle Albums')

    def do_deactivate(self):
        # Disconnect bookmark observers and checkpoint BEFORE original teardown.
        self._resume_deactivate()
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
        try:
            if isinstance(widget, Gtk.Button):
                action_name = widget.get_action_name() if isinstance(widget, Gtk.Actionable) else None
                tooltip = (widget.get_tooltip_text() or '').lower()
                widget_name = (widget.get_name() or '').lower()
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
        import math
        alloc = area.get_allocation()
        size = min(alloc.width, alloc.height)
        if size <= 1:
            return False
        fg = area.get_style_context().get_color(area.get_style_context().get_state())
        dark = (fg.red, fg.green, fg.blue)
        cx, cy, radius = size * 0.71, size * 0.78, size * 0.285
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.arc(cx, cy, radius + size * 0.055, 0, 2 * math.pi)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgb(*dark)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.arc(cx, cy, radius * 0.52, 0, 2 * math.pi)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgb(*dark)
        cr.arc(cx, cy, radius * 0.21, 0, 2 * math.pi)
        cr.fill()
        return False

    def _draw_combined_icon(self, area, cr, icon_size):
        import os
        theme = Gtk.IconTheme.get_default()
        pixbuf = None
        for name in ('media-playlist-shuffle-symbolic', 'media-playlist-shuffle'):
            if not theme.has_icon(name):
                continue
            try:
                info = theme.lookup_icon(name, icon_size, Gtk.IconLookupFlags.FORCE_SIZE)
                if info is None:
                    continue
                pixbuf, _symbolic = info.load_symbolic_for_context(area.get_style_context())
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
        cr.push_group()
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, x, y)
        cr.paint()
        self._draw_cd_overlay(area, cr)
        cr.pop_group_to_source()
        cr.paint()
        return False

    def _make_icon_button(self, shuffle_button=None):
        button = Gtk.Button()
        if isinstance(shuffle_button, Gtk.Button):
            button.set_relief(shuffle_button.get_relief())
        button.get_style_context().add_class('image-button')
        if isinstance(shuffle_button, Gtk.Button) and shuffle_button.get_style_context().has_class('raised'):
            button.get_style_context().add_class('raised')
        button.set_tooltip_text('Shuffle Albums — resume the existing shuffle or create a new one')
        icon_size = self._shuffle_icon_size(shuffle_button)
        canvas = Gtk.DrawingArea()
        canvas.set_size_request(icon_size, icon_size)
        canvas.connect('draw', self._draw_combined_icon, icon_size)
        canvas.connect('style-updated', lambda widget: widget.queue_draw())
        canvas.connect('state-flags-changed', lambda widget, old_flags: widget.queue_draw())
        Gtk.IconTheme.get_default().connect('changed', lambda theme: canvas.queue_draw())
        button.add(canvas)
        button.connect('clicked', self.on_start)
        return button

    def _install_button(self):
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
            page = self.last_source
        if page is None or not isinstance(page, RB.Source):
            return None
        if page is self.playlist:
            return None
        return page

    def _get_or_create_playlist(self, manager):
        if _ss_registered_playlist(self, self.playlist):
            _ss_bind_playlist(self, 'album', self.playlist)
            return self.playlist
        self.playlist = None
        for candidate in manager.get_playlists():
            if (isinstance(candidate, RB.StaticPlaylistSource) and
                    candidate.props.name == PLAYLIST_NAME):
                self.playlist = candidate
                _ss_bind_playlist(self, 'album', candidate)
                return candidate
        self.playlist = manager.new_playlist(PLAYLIST_NAME, False)
        # The manager always opens an inline rename editor at idle; close it
        # after its callback has run without changing the playlist name.
        GLib.idle_add(_ss_finish_generated_playlist_name, self, self.playlist)
        _ss_bind_playlist(self, 'album', self.playlist)
        return self.playlist

    def _clear_generated_playlist(self, playlist):
        _ss_clear_full_playlist(playlist)

    def on_start(self, *_args):
        try:
            shell = self.object
            source = (getattr(self, '_ss_start_source_override', None) or
                      self._current_filtered_source())
            if source is None:
                LOG.warning('Album Shuffle: select Music or a playlist first')
                return
            entry_view = source.get_entry_view()
            model = entry_view.props.model if entry_view is not None else None
            if model is None:
                LOG.warning('Album Shuffle: no visible song list for source %s', source.props.name)
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
            include_unknown = read_include_unknown()
            grouped = group_albums(entries, include_unknown=include_unknown)
            if not grouped:
                LOG.warning('Album Shuffle: no eligible albums/folders in this filtered view')
                return
            LOG.warning('Album Shuffle unknown-album policy: %s',
                        'include by folder' if include_unknown else 'exclude')
            # Commit to replacing the old shuffle only AFTER the candidate
            # Music/playlist view has produced a usable set of groups.
            self._cancel_full_playlist()
            self._view_callback_epoch += 1
            self.last_source = source
            self.albums = grouped
            self._full_playlist_expected_tracks = sum(len(v) for v in grouped.values())
            keys = list(grouped)
            random.SystemRandom().shuffle(keys)
            self.deck = deque(keys)
            self.album_ends = set()
            self.scheduled_count = 0
            self.completed_count = 0
            self.active = False
            self.player.set_playback_state(False, False)
            manager = shell.props.playlist_manager
            self.playlist = self._get_or_create_playlist(manager)
            self._clear_generated_playlist(self.playlist)
            LOG.warning('Album Shuffle selection after playlist creation: %s',
                        getattr(shell.props.selected_page.props, 'name', '?')
                        if shell.props.selected_page is not None else '(none)')
            first_entry = None
            for _ in range(min(INITIAL_GROUPS, len(self.deck))):
                first = self._append_album()
                if first_entry is None:
                    first_entry = first
            if first_entry is None:
                return
            self.player.set_playback_state(False, False)
            self.active = True
            self._shuffle_start_entry = first_entry
            self.player.play_entry(first_entry, self.playlist)
            GLib.idle_add(self._restore_browser_view, source, first_entry,
                          self._view_callback_epoch)
            self._schedule_full_playlist()
            LOG.warning('Album Shuffle: %d albums, %d eligible tracks from %d displayed '
                        '(%d excluded); first album: %s',
                        len(keys), self._full_playlist_expected_tracks, len(entries),
                        len(entries) - self._full_playlist_expected_tracks,
                        first_entry.get_string(RB.RhythmDBPropType.ALBUM))
            return True  # Only a completed new-shuffle setup updates the bookmark.
        except Exception:
            self.active = False
            LOG.exception('Could not start album shuffle')

    def _restore_browser_view(self, source, first_entry, epoch):
        # The generated playlist drives playback, but Music remains the view.
        _ss_show_music(self, 'album', epoch, source)
        return GLib.SOURCE_REMOVE

    def _cancel_full_playlist(self):
        if self._full_playlist_idle is not None:
            GLib.source_remove(self._full_playlist_idle)
            self._full_playlist_idle = None

    def _schedule_full_playlist(self):
        if self._full_playlist_idle is None and self.deck and self.playlist is not None:
            self._full_playlist_idle = GLib.idle_add(self._fill_playlist_step)

    def _fill_playlist_step(self):
        # Populate the complete shuffled cycle without a long blocking GTK call.
        # The limited batch is a time slice; there is NO album or track limit.
        if self.playlist is None or not self.albums:
            self._full_playlist_idle = None
            return GLib.SOURCE_REMOVE
        try:
            groups_added = tracks_added = 0
            while (self.deck and groups_added < FULL_GROUPS_PER_IDLE and
                   (tracks_added < FULL_TRACKS_PER_IDLE or groups_added == 0)):
                key = self.deck[0]
                tracks_added += len(self.albums[key])
                self._append_album()
                groups_added += 1
            if getattr(self, '_ss_generation', None) is not None:
                _ss_progress(self, 'album')
            if self.deck:
                return GLib.SOURCE_CONTINUE
            self._full_playlist_idle = None
            actual = sum(1 for _row in _ss_full_playlist_model(self.playlist))
            expected = self._full_playlist_expected_tracks or sum(len(v) for v in self.albums.values())
            if actual != expected:
                LOG.error('Album Shuffle INCOMPLETE: playlist contains %d of %d eligible tracks',
                          actual, expected)
                self._report_incomplete(actual, expected)
            else:
                LOG.warning('Album Shuffle: COMPLETE playlist: %d albums, %d/%d tracks',
                            len(self.albums), actual, expected)
            return GLib.SOURCE_REMOVE
        except Exception:
            self._full_playlist_idle = None
            LOG.exception('Album Shuffle: failed to finish generated playlist')
            self._report_incomplete(-1, self._full_playlist_expected_tracks)
            return GLib.SOURCE_REMOVE

    def _report_incomplete(self, actual, expected):
        # Do not leave a truncated playlist pretending to be the full selection.
        try:
            detail = ('Only %d of %d eligible tracks were added.' % (actual, expected)
                      if actual >= 0 else 'An error interrupted playlist generation.')
            dialog = Gtk.MessageDialog(
                transient_for=self.object.props.window,
                flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text='Album Shuffle playlist is incomplete')
            dialog.format_secondary_text(detail + ' Please check the Rhythmbox log.')
            dialog.connect('response', lambda dlg, _response: dlg.destroy())
            dialog.show()
        except Exception:
            LOG.exception('Album Shuffle: could not show incomplete-playlist warning')

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
        if self.pending_follow_id is not None:
            GLib.source_remove(self.pending_follow_id)
            self.pending_follow_id = None
        self.pending_follow_id = GLib.idle_add(self._follow_entry, entry)

    def _follow_entry(self, entry):
        self.pending_follow_id = None
        try:
            # An idle callback queued before switching shuffles must not
            # highlight an entry from the previous playback source.
            if (self.player.get_playing_source() != self.playlist or
                    self.player.get_playing_entry() != entry):
                return GLib.SOURCE_REMOVE
            # Preserve the original follow behavior for the filtered Music view,
            # but also highlight the saved song in the generated shuffle playlist.
            source = self.object.props.selected_page
            if not self.active or source not in (self.last_source, self.playlist):
                return GLib.SOURCE_REMOVE
            view = source.get_entry_view()
            if view is None or not view.get_entry_contained(entry):
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
                # Temporary diversion is not the end of this shuffle session.
                # Keep its deck, playlist, and state file available for Resume.
                return
            self._queue_follow(entry)
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if uri not in self.album_ends:
                return
            self.album_ends.discard(uri)
            self.completed_count += 1
            if not self.deck and self.completed_count < self.scheduled_count:
                return
            self.replenishing = True
            GLib.idle_add(self._append_next_idle, self._view_callback_epoch)
        except Exception:
            LOG.exception('Album Shuffle continuation failed')

    def _append_next_idle(self, epoch=None):
        # Do not append a truncated replacement group while saved entries
        # are offline.  Existing Rhythmbox playlist playback still works.
        if getattr(self, '_ss_incomplete_restore', False):
            self.replenishing = False
            return GLib.SOURCE_REMOVE
        if epoch is not None and epoch != self._view_callback_epoch:
            self.replenishing = False
            return GLib.SOURCE_REMOVE  # Obsolete callback from an older shuffle.
        try:
            if (self.active and self.playlist is not None and
                    self.player.get_playing_source() == self.playlist):
                self._append_album()
        except Exception:
            LOG.exception('Unable to append next random album')
        finally:
            self.replenishing = False
        return GLib.SOURCE_REMOVE


# === Rhythmbox persistent shuffle session integration (v1) ===

# This file's session extension is embedded, so this shuffle plugin remains
# independently installable with or without Session Restore.
import json as _ss_json
import os as _ss_os
import tempfile as _ss_tempfile
import time as _ss_time
import uuid as _ss_uuid
from pathlib import Path as _SSPath


def _ss_root():
    return _SSPath(_ss_os.environ.get('XDG_STATE_HOME',
                    _ss_os.path.expanduser('~/.local/state'))) / 'rhythmbox-shuffle-state'


def _ss_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with _ss_tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(path.parent),
                                             prefix='.' + path.name + '.', delete=False) as fp:
            name = fp.name
            _ss_json.dump(data, fp, ensure_ascii=False, separators=(',', ':'))
            fp.write('\n')
            fp.flush()
            _ss_os.fsync(fp.fileno())
        _ss_os.replace(name, path)
    finally:
        if name and _ss_os.path.exists(name):
            _ss_os.unlink(name)


def _ss_read(path):
    try:
        with path.open(encoding='utf-8') as fp:
            value = _ss_json.load(fp)
        return value if isinstance(value, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        LOG.exception('Shuffle session: failed to read %s', path)
        return None


def _ss_key(key):
    return list(key) if isinstance(key, tuple) else key


def _ss_decode(key):
    return tuple(key) if isinstance(key, list) else key


def _ss_paths(mode):
    root = _ss_root()
    return root / (mode + '-groups.json'), root / (mode + '-progress.json'), root / (mode + '-ready.json')



# --- Generated-playlist lifecycle (test9) ---
# RB.DisplayPage emits 'deleted' for an explicit Delete Playlist request.
# Source existence is always checked against Rhythmbox's playlist manager;
# holding a Python reference is NOT proof the playlist still exists.
def _ss_registered_playlist(plugin, candidate):
    if candidate is None:
        return False
    try:
        return any(p == candidate for p in
                   plugin.object.props.playlist_manager.get_playlists())
    except Exception:
        LOG.exception('Shuffle: could not check playlist registration')
        return False


def _ss_unbind_playlist(plugin):
    old = getattr(plugin, '_ss_watched_playlist', None)
    handler = getattr(plugin, '_ss_watched_playlist_handler', None)
    plugin._ss_watched_playlist = None
    plugin._ss_watched_playlist_handler = None
    if old is not None and handler is not None:
        try:
            old.disconnect(handler)
        except (TypeError, RuntimeError):
            pass


def _ss_bind_playlist(plugin, mode, playlist):
    if not _ss_registered_playlist(plugin, playlist):
        return False
    if (getattr(plugin, '_ss_watched_playlist', None) == playlist and
            getattr(plugin, '_ss_watched_playlist_handler', None) is not None):
        return True
    _ss_unbind_playlist(plugin)
    try:
        handler = playlist.connect('deleted',
            lambda *_args: _ss_playlist_deleted(plugin, mode, playlist))
    except Exception:
        LOG.exception('%s Shuffle: cannot monitor playlist deletion', mode.title())
        return False
    plugin._ss_watched_playlist = playlist
    plugin._ss_watched_playlist_handler = handler
    return True


def _ss_watch_existing_playlist(plugin, mode):
    # Rhythmbox can load playlists before the saved session is restored.
    # Subscribe as soon as one appears, without treating a missing playlist
    # during startup as a deletion (it may not have loaded yet).
    try:
        for candidate in plugin.object.props.playlist_manager.get_playlists():
            if (isinstance(candidate, RB.StaticPlaylistSource) and
                    candidate.props.name == PLAYLIST_NAME):
                _ss_bind_playlist(plugin, mode, candidate)
                break
    except Exception:
        LOG.exception('%s Shuffle: cannot watch existing playlist', mode.title())



def _ss_playlist_announced(plugin, mode, candidate):
    if (isinstance(candidate, RB.StaticPlaylistSource) and
            candidate.props.name == PLAYLIST_NAME):
        if not _ss_bind_playlist(plugin, mode, candidate):
            GLib.idle_add(lambda: (_ss_watch_existing_playlist(plugin, mode),
                                   GLib.SOURCE_REMOVE)[1])



def _ss_full_playlist_model(playlist):
    """The static playlist's stored order, never its browser-filtered rows.

    Rhythmbox saves static playlists from base-query-model.  Using the
    current get_query_model() here loses hidden rows when Genre/Artist/Album
    or a third-party browser filter is active.
    """
    if playlist is None:
        return None
    return playlist.props.base_query_model


def _ss_full_playlist_entries(playlist):
    model = _ss_full_playlist_model(playlist)
    if model is None:
        raise RuntimeError('generated playlist base model is unavailable')
    return [row[0] for row in model]


def _ss_clear_full_playlist(playlist):
    """Remove every stored entry without triggering a filtered-model assert.

    Rhythmbox's C remove_entry checks its *current* query model, although
    actual playlist membership lives in the base model.  Clear the native
    browser first, then refuse to mutate if a custom/third-party filter still
    leaves hidden entries.  This protects the old shuffle from partial loss.
    """
    rows = _ss_full_playlist_entries(playlist)
    base = _ss_full_playlist_model(playlist)
    # RB.Source's reset_filters is a C class virtual function, not a public
    # GI method.  Calling playlist.reset_filters() raised AttributeError and
    # aborted every New Shuffle before it could start playing.
    #
    # RB.PlaylistSource.set_query_model IS public and updates both the source
    # playback model and the internal model consulted by remove_entry().
    # Never remove a hidden entry while the latter still points at the
    # browser-filtered query model: Rhythmbox asserts it must contain the row.
    if playlist.get_query_model() is not base:
        playlist.set_query_model(base)
    if playlist.get_query_model() is not base:
        raise RuntimeError('cannot expose complete playlist for safe removal')
    for entry in rows:
        # Native browser or third-party filter callbacks can replace the
        # current query model during row removal.  Rebind to the base model
        # only when necessary, before the next C remove_entry() call.
        if playlist.get_query_model() is not base:
            playlist.set_query_model(base)
        if playlist.get_query_model() is not base:
            raise RuntimeError('playlist filtering changed during cleanup')
        playlist.remove_entry(entry)
    if _ss_full_playlist_entries(playlist):
        raise RuntimeError('generated playlist did not clear completely')


def _ss_snapshot_before_start(plugin, mode):
    # A failed start must not strand the previous saved generation pointing to
    # a replaced playlist. Keep an in-memory rollback snapshot until verified.
    playlist = plugin.playlist if _ss_registered_playlist(plugin, plugin.playlist) else None
    if playlist is None:
        for candidate in plugin.object.props.playlist_manager.get_playlists():
            if (isinstance(candidate, RB.StaticPlaylistSource) and
                    candidate.props.name == PLAYLIST_NAME):
                playlist = candidate
                break
    rows = []
    if playlist is not None:
        model = _ss_full_playlist_model(playlist)
        if model is not None:
            rows = [row[0] for row in model]
    group_name = 'albums' if mode == 'album' else 'folders'
    ends_name = 'album_ends' if mode == 'album' else 'folder_ends'
    return {'playlist': playlist, 'rows': rows,
            'disk_state': [(path, _ss_read(path)) for path in
                           (*_ss_paths(mode), _ss_root() / (mode + '-bookmark.json'))],
            'epoch': plugin._view_callback_epoch,
            'groups': getattr(plugin, group_name).copy(),
            'ends': set(getattr(plugin, ends_name)),
            'deck': deque(plugin.deck), 'active': plugin.active,
            'generation': getattr(plugin, '_ss_generation', None),
            'last_source': plugin.last_source,
            'scheduled_count': plugin.scheduled_count,
            'completed_count': plugin.completed_count,
            'expected_tracks': plugin._full_playlist_expected_tracks,
            'saved': (plugin._bookmark.saved.copy()
                      if plugin._bookmark and plugin._bookmark.saved else None)}


def _ss_rollback_unverified_start(plugin, mode, pending):
    old = pending.get('before') if pending else None
    if not old:
        plugin.active = False
        return
    # The start routine validates its filters before touching the old shuffle.
    # If it exited early, nothing requires a potentially huge row rebuild.
    mutated = plugin._view_callback_epoch != old['epoch']
    if mutated:
        plugin._cancel_full_playlist()
    if pending.get('swap'):
        pending['swap_was_used'] = True
        _ss_rollback_playlist_swap(plugin, mode, pending)
    playlist = old['playlist']
    if playlist is not None and _ss_registered_playlist(plugin, playlist):
        if mutated and not pending.get('swap_rollback_skips_rebuild') and not pending.get('swap_was_used'):
            try:
                _ss_clear_full_playlist(playlist)
                for entry in old['rows']:
                    playlist.add_entry(entry, -1)
            except Exception:
                LOG.exception('%s Shuffle: failed to roll back playlist rows', mode.title())
        plugin.playlist = playlist
        _ss_bind_playlist(plugin, mode, playlist)
    else:
        # A deletion during an attempted start is definitive, never recover
        # the prior generation into a newly created playlist.
        if old['generation']:
            plugin.active = False
            return
    plugin.deck = old['deck']
    if mode == 'album':
        plugin.albums = old['groups']
        plugin.album_ends = old['ends']
    else:
        plugin.folders = old['groups']
        plugin.folder_ends = old['ends']
    plugin.active = old['active']
    plugin._ss_generation = old['generation']
    plugin.last_source = old['last_source']
    plugin.scheduled_count = old['scheduled_count']
    plugin.completed_count = old['completed_count']
    plugin._full_playlist_expected_tracks = old['expected_tracks']
    if plugin._bookmark is not None:
        plugin._bookmark.saved = old['saved']
        plugin._bookmark._observed_source = None
        plugin._bookmark._observed_uri = None
    # Initial session persistence uses several individually atomic files.
    # If a write fails midway, restore their previous generation together.
    if pending.get('state_write_attempted'):
        for path, previous in old['disk_state']:
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _ss_atomic(path, previous)
            except OSError:
                LOG.exception('%s Shuffle: unable to restore old session file %s',
                              mode.title(), path)
    if mutated and plugin.active and plugin.deck:
        plugin._schedule_full_playlist()
    LOG.warning('%s Shuffle: rolled back unverified New Shuffle; previous bookmark preserved',
                mode.title())

def _ss_playlist_deleted(plugin, mode, deleted):
    if (getattr(plugin, '_ss_watched_playlist', None) != deleted or
            getattr(plugin, '_ss_discarding', False)):
        return
    _ss_cancel_missing_recovery(plugin)
    plugin._ss_discarding = True
    try:
        try:
            was_playing = plugin.player.get_playing_source() == deleted
        except Exception:
            was_playing = False
        # Invalidate the session BEFORE stopping the player: stop fires song/
        # source signals which must not recreate a deleted bookmark.
        _ss_cancel_pending_start(plugin)
        plugin._view_callback_epoch += 1
        plugin.active = False
        plugin.replenishing = False
        plugin._cancel_full_playlist()
        for field in ('pending_follow_id', 'pending_follow', '_ss_music_follow_id'):
            timer = getattr(plugin, field, None)
            if timer is not None:
                try:
                    GLib.source_remove(timer)
                except Exception:
                    pass
                setattr(plugin, field, None)
        if getattr(plugin, '_ss_restore_timer', None) is not None:
            GLib.source_remove(plugin._ss_restore_timer)
            plugin._ss_restore_timer = None
        plugin._ss_restored = True
        plugin._ss_generation = None
        plugin.playlist = None
        plugin._shuffle_start_entry = None
        plugin.deck.clear()
        if mode == 'album':
            plugin.albums = {}
            plugin.album_ends.clear()
        else:
            plugin.folders = {}
            plugin.folder_ends.clear()
        plugin.scheduled_count = plugin.completed_count = 0
        plugin._full_playlist_expected_tracks = 0
        bookmark = getattr(plugin, '_bookmark', None)
        if bookmark is not None:
            bookmark.reset()
            bookmark._observed_source = None
            bookmark._observed_uri = None
        if getattr(plugin, '_resume_dialog', None) is not None:
            plugin._resume_dialog.destroy()
            plugin._resume_dialog = None
        _ss_unbind_playlist(plugin)
        for path in (*_ss_paths(mode), _ss_root() / (mode + '-bookmark.json')):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOG.exception('%s Shuffle: could not remove discarded state file %s',
                              mode.title(), path)
        if was_playing:
            # Only stop the deleted shuffle, never unrelated Music or another
            # plugin's playback. RBShellPlayer.stop closes the old stream.
            try:
                plugin.player.stop()
            except Exception:
                LOG.exception('%s Shuffle: could not stop deleted playlist', mode.title())
        LOG.warning('%s Shuffle: playlist deleted; discarded saved shuffle and bookmark',
                    mode.title())
    finally:
        plugin._ss_discarding = False


def _ss_cancel_pending_start(plugin):
    pending = getattr(plugin, '_ss_pending_start', None)
    plugin._ss_pending_start = None
    if not pending:
        return
    timer = pending.get('timer')
    if timer is not None:
        try:
            GLib.source_remove(timer)
        except (TypeError, RuntimeError):
            pass
    for handler in pending.get('handlers', []):
        try:
            plugin.player.disconnect(handler)
        except (TypeError, RuntimeError):
            pass


def _ss_verify_new_playback(plugin, mode):
    pending = getattr(plugin, '_ss_pending_start', None)
    if not pending or not pending.get('armed'):
        return GLib.SOURCE_REMOVE
    if not _ss_registered_playlist(plugin, pending['playlist']):
        LOG.error('%s Shuffle: generated playlist disappeared before playback confirmation',
                  mode.title())
        _ss_cancel_pending_start(plugin)
        _ss_rollback_unverified_start(plugin, mode, pending)
        if getattr(plugin, '_bookmark', None) is not None:
            plugin._bookmark.suspended = False
        return GLib.SOURCE_REMOVE
    try:
        current = plugin.player.get_playing_entry()
        source = plugin.player.get_playing_source()
        status = plugin.player.get_playing()
        playing = bool(status[-1] if isinstance(status, tuple) else status)
        matched = (playing and source == pending['playlist'] and current is not None and
                   current.get_string(RB.RhythmDBPropType.LOCATION) == pending['uri'])
    except Exception:
        matched = False
    if matched:
        _ss_cancel_pending_start(plugin)
        pending['state_write_attempted'] = True
        # A confirmation of the actual playback source and entry is required
        # before replacing the on-disk session generation or its bookmark.
        try:
            if not _ss_start(plugin, mode):
                raise RuntimeError('session generation could not be saved')
            bookmark = getattr(plugin, '_bookmark', None)
            if bookmark is not None:
                bookmark.start_new()
                saved = _ss_read(bookmark.path)
                if (not saved or saved.get('generation') != plugin._ss_generation or
                        saved.get('uri') != pending['uri']):
                    raise RuntimeError('new shuffle bookmark was not persisted')
                bookmark.suspended = False
            plugin._schedule_full_playlist()
            _ss_schedule_music_follow(plugin, mode)
            _ss_commit_playlist_swap(plugin, mode, pending)
            LOG.warning('%s Shuffle: verified new playback; committed session', mode.title())
        except Exception:
            LOG.exception('%s Shuffle: verified playback but could not commit session',
                          mode.title())
            _ss_rollback_unverified_start(plugin, mode, pending)
            bookmark = getattr(plugin, '_bookmark', None)
            if bookmark is not None:
                bookmark.suspended = False
        return GLib.SOURCE_REMOVE
    if _ss_time.monotonic() >= pending['deadline']:
        LOG.error('%s Shuffle: new playback not confirmed; bookmark not replaced',
                  mode.title())
        _ss_cancel_pending_start(plugin)
        _ss_rollback_unverified_start(plugin, mode, pending)
        bookmark = getattr(plugin, '_bookmark', None)
        if bookmark is not None:
            bookmark.suspended = False
        return GLib.SOURCE_REMOVE
    return GLib.SOURCE_CONTINUE

def _ss_cancel_missing_recovery(plugin):
    timer = getattr(plugin, '_ss_missing_restore_timer', None)
    plugin._ss_missing_restore_timer = None
    if timer is not None:
        try:
            GLib.source_remove(timer)
        except (TypeError, RuntimeError):
            pass
    plugin._ss_pending_manifest = None
    plugin._ss_pending_deck = None
    plugin._ss_missing_count = 0
    plugin._ss_incomplete_restore = False


def _ss_reconcile_missing(plugin, mode):
    """Finish a partial startup recovery without rewriting the saved order.

    The existing native playlist can be resumed while an old URI is offline,
    but *no* further groups may be appended until the complete manifest is
    available.  Recheck only in memory; never alter the saved group manifest.
    """
    if not getattr(plugin, '_ss_incomplete_restore', False):
        plugin._ss_missing_restore_timer = None
        return GLib.SOURCE_REMOVE
    if getattr(plugin, '_ss_pending_start', None) is not None:
        return GLib.SOURCE_CONTINUE
    manifest = getattr(plugin, '_ss_pending_manifest', None)
    if not manifest or manifest.get('generation') != plugin._ss_generation:
        _ss_cancel_missing_recovery(plugin)
        return GLib.SOURCE_REMOVE
    try:
        db = plugin.object.props.db
        groups = {}
        missing = 0
        for group in manifest['groups']:
            key = _ss_decode(group['key'])
            entries = []
            for uri in group['uris']:
                entry = db.entry_lookup_by_location(uri)
                if entry is None:
                    missing += 1
                else:
                    entries.append(entry)
            if entries:
                groups[key] = entries
        if missing:
            if missing != plugin._ss_missing_count:
                LOG.warning('%s Shuffle: %d unavailable saved tracks; keeping the original order on disk',
                            mode.title(), missing)
                plugin._ss_missing_count = missing
            return GLib.SOURCE_CONTINUE
        # Database availability can precede playlist XML row insertion.  The
        # pending queue cannot be made active before native membership returns.
        base = _ss_full_playlist_model(plugin.playlist)
        current_uris = ({row[0].get_string(RB.RhythmDBPropType.LOCATION)
                         for row in base} if base is not None else set())
        # Groups still in the saved deck have not been written to the
        # playlist yet.  They must NOT block recovery of an interrupted fill.
        pending_keys = {_ss_decode(raw) for raw in plugin._ss_pending_deck}
        if any(uri not in current_uris for group in manifest['groups']
               if _ss_decode(group['key']) not in pending_keys
               for uri in group['uris']):
            return GLib.SOURCE_CONTINUE
        plugin.albums = groups
        plugin.deck = deque(k for raw in plugin._ss_pending_deck
                            if (k := _ss_decode(raw)) in groups)
        plugin._full_playlist_expected_tracks = sum(len(v) for v in groups.values())
        # GLib removes this currently executing timeout on SOURCE_REMOVE.
        plugin._ss_missing_restore_timer = None
        _ss_cancel_missing_recovery(plugin)
        plugin._schedule_full_playlist()
        _ss_progress(plugin, mode)
        LOG.warning('%s Shuffle: all saved tracks became available; full continuous shuffle restored',
                    mode.title())
        return GLib.SOURCE_REMOVE
    except Exception:
        LOG.exception('%s Shuffle: failed to reconcile pending saved tracks', mode.title())
        return GLib.SOURCE_CONTINUE


def _ss_progress(plugin, mode):
    # A partial resume must not overwrite the complete saved deck or group
    # progress while some library entries are offline.
    if (getattr(plugin, '_ss_generation', None) is None or
            getattr(plugin, '_ss_incomplete_restore', False) or
            getattr(plugin, '_ss_pending_start', None) is not None):
        return
    try:
        keys = list(plugin.deck)
        ends = getattr(plugin, 'album_ends' if mode == 'album' else 'folder_ends')
        playlist = getattr(plugin, 'playlist', None)
        last_source = getattr(plugin, 'last_source', None)
        state = {'version': 1, 'generation': plugin._ss_generation,
                 'active': bool(plugin.active),
                 'resumable': bool(playlist is not None and plugin._ss_generation),
                 'playlist': playlist.props.name if playlist else '',
                 'source': last_source.props.name if last_source else 'Music',
                 'deck': [_ss_key(k) for k in keys],
                 'ends': sorted(ends),
                 'scheduled': int(plugin.scheduled_count),
                 'completed': int(plugin.completed_count),
                 'timestamp': int(_ss_time.time())}
        _ss_atomic(_ss_paths(mode)[1], state)
    except Exception:
        LOG.exception('%s Shuffle: unable to checkpoint progress', mode.title())


def _ss_start(plugin, mode):
    # Called only after a New Shuffle has been confirmed.  An incomplete
    # previous generation must never reconcile over this replacement.
    _ss_cancel_missing_recovery(plugin)
    if getattr(plugin, '_ss_restore_timer', None) is not None:
        try:
            GLib.source_remove(plugin._ss_restore_timer)
        except Exception:
            pass
        plugin._ss_restore_timer = None
    plugin._ss_restored = True
    if not plugin.active:
        _ss_progress(plugin, mode)
        return False
    try:
        plugin._ss_generation = _ss_uuid.uuid4().hex
        groups = plugin.albums if mode == 'album' else plugin.folders
        payload = {'version': 1, 'generation': plugin._ss_generation,
                   'groups': [{'key': _ss_key(k),
                               'uris': [entry.get_string(RB.RhythmDBPropType.LOCATION)
                                        for entry in entries]}
                              for k, entries in groups.items()]}
        _ss_atomic(_ss_paths(mode)[0], payload)
        _ss_progress(plugin, mode)
        _ss_ready(plugin, mode)
        confirmed_groups = _ss_read(_ss_paths(mode)[0])
        confirmed_progress = _ss_read(_ss_paths(mode)[1])
        return bool(confirmed_groups and confirmed_progress and
                    confirmed_groups.get('generation') == plugin._ss_generation and
                    confirmed_progress.get('generation') == plugin._ss_generation)
    except Exception:
        LOG.exception('%s Shuffle: failed to save initial shuffle order', mode.title())
        return False


def _ss_ready(plugin, mode):
    try:
        _ss_atomic(_ss_paths(mode)[2], {'pid': _ss_os.getpid(),
                   'generation': plugin._ss_generation, 'active': bool(plugin.active)})
    except Exception:
        LOG.exception('%s Shuffle: could not publish readiness', mode.title())


def _ss_enable_pending_resume_button(plugin, mode):
    """Enable Resume in an already-open dialog only after verified restoration.

    The toolbar can be clicked while Rhythmbox is still loading a long saved
    playlist. Unlike test15, do not freeze the Resume choice at click time.
    UI errors must never abort the underlying successful session restoration.
    """
    try:
        dialog = getattr(plugin, '_resume_dialog', None)
        bookmark = getattr(plugin, '_bookmark', None)
        if dialog is None or bookmark is None or not bookmark.valid():
            return
        resume_button = dialog.get_widget_for_response(Gtk.ResponseType.YES)
        if resume_button is None:
            return
        resume_button.set_sensitive(True)
        dialog.format_secondary_text(
            'Resume returns to the bookmarked track at its saved playback time, '
            'keeping the existing playlist order. New Shuffle replaces that '
            'playlist using the currently selected library filters.')
        dialog.set_default_response(Gtk.ResponseType.YES)
        LOG.info('%s Shuffle: enabled Resume in already-open startup dialog',
                 mode.title())
    except Exception:
        LOG.exception('%s Shuffle: could not update pending Resume dialog',
                      mode.title())


def _ss_restore(plugin, mode):
    # An explicit New Shuffle is being prepared; don't let startup recovery
    # replace its candidate deck before playback verification finishes.
    if getattr(plugin, '_ss_pending_start', None) is not None:
        return GLib.SOURCE_CONTINUE
    if getattr(plugin, '_ss_restored', False):
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    manifest = _ss_read(_ss_paths(mode)[0])
    progress = _ss_read(_ss_paths(mode)[1])
    if manifest is None or progress is None or not (
            progress.get('active') or progress.get('resumable')):
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    if manifest.get('generation') != progress.get('generation'):
        LOG.warning('%s Shuffle: inconsistent state generations; not restoring', mode.title())
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    if _ss_time.monotonic() > plugin._ss_deadline:
        LOG.warning('%s Shuffle: library not ready; session state remains on disk', mode.title())
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    try:
        target = None
        for playlist in plugin.object.props.playlist_manager.get_playlists():
            if playlist.props.name == progress.get('playlist'):
                target = playlist
                break
        if target is None or _ss_full_playlist_model(target) is None:
            return GLib.SOURCE_CONTINUE
        database = plugin.object.props.db
        groups = {}
        missing = 0
        available_uris = set()
        for group in manifest['groups']:
            key = _ss_decode(group['key'])
            entries = []
            for uri in group['uris']:
                entry = database.entry_lookup_by_location(uri)
                if entry is None:
                    missing += 1
                else:
                    entries.append(entry)
                    available_uris.add(uri)
            if entries:
                groups[key] = entries
        if not groups:
            return GLib.SOURCE_CONTINUE
        # The playlist manager can advertise a playlist before its rows have
        # been loaded from XML.  Previously, DB lookups alone were considered
        # enough, so restoration could complete prematurely and Resume stayed
        # disabled forever.  Wait for every currently resolvable saved URI to
        # appear in the actual base model before marking restoration complete.
        base_uris = {row[0].get_string(RB.RhythmDBPropType.LOCATION)
                     for row in _ss_full_playlist_model(target)}
        pending_keys = {_ss_decode(raw) for raw in progress.get('deck', [])}
        # Only groups already scheduled into the playlist must have rows.
        # Pending groups were never inserted on interrupted/early shutdown.
        scheduled_uris = {uri for group in manifest['groups']
                          if _ss_decode(group['key']) not in pending_keys
                          for uri in group['uris'] if uri in available_uris}
        unloaded = scheduled_uris - base_uris
        # Even if the saved deck still contains the bookmarked album, Resume
        # must never become ready before that exact track is in the playlist.
        saved_bookmark = getattr(getattr(plugin, '_bookmark', None), 'saved', None)
        if (isinstance(saved_bookmark, dict) and
                saved_bookmark.get('generation') == progress['generation']):
            bookmarked_uri = saved_bookmark.get('uri')
            if (not bookmarked_uri or bookmarked_uri not in available_uris or
                    bookmarked_uri not in base_uris):
                return GLib.SOURCE_CONTINUE
        if unloaded:
            previous = getattr(plugin, '_ss_last_waiting_rows', None)
            if previous != len(unloaded):
                plugin._ss_last_waiting_rows = len(unloaded)
                LOG.warning('%s Shuffle: waiting for %d saved playlist rows to load',
                            mode.title(), len(unloaded))
            return GLib.SOURCE_CONTINUE
        if missing:
            # Allow partial recovery ONLY when the saved bookmark is available
            # and belongs to this actual playlist.  The old manifest/deck are
            # never rewritten until the remaining URI(s) can be reconciled.
            bookmark = getattr(plugin, '_bookmark', None)
            saved = bookmark.saved if bookmark is not None else None
            saved_uri = saved.get('uri') if saved else None
            if (not saved_uri or saved.get('generation') != progress['generation'] or
                    saved_uri not in available_uris):
                return GLib.SOURCE_CONTINUE
        # Avoid restoring stale progress over a newer shuffle started by user.
        if getattr(plugin, '_ss_restored', False):
            return GLib.SOURCE_REMOVE
        plugin.playlist = target
        _ss_bind_playlist(plugin, mode, target)
        if mode == 'album':
            plugin.albums = groups
            plugin.album_ends = set(progress.get('ends', []))
        else:
            plugin.folders = groups
            plugin.folder_ends = set(progress.get('ends', []))
        plugin.deck = deque(k for raw in progress.get('deck', [])
                            if (k := _ss_decode(raw)) in groups)
        plugin.scheduled_count = int(progress.get('scheduled', 0))
        plugin.completed_count = int(progress.get('completed', 0))
        wanted = progress.get('source') or 'Music'
        plugin.last_source = plugin.object.props.library_source
        for source in plugin.object.props.playlist_manager.get_playlists():
            if source.props.name == wanted:
                plugin.last_source = source
                break
        plugin.active = True
        plugin.replenishing = False
        plugin._ss_generation = progress['generation']
        plugin._ss_restored = True
        plugin._ss_restore_timer = None
        _ss_ready(plugin, mode)
        _ss_enable_pending_resume_button(plugin, mode)
        # Preserve the saved manifest and pending queue if any URI is offline.
        # The native playlist itself is never rewritten by startup recovery.
        plugin._full_playlist_expected_tracks = sum(len(g['uris']) for g in manifest['groups'])
        if missing:
            plugin._ss_incomplete_restore = True
            plugin._ss_pending_manifest = manifest
            plugin._ss_pending_deck = list(progress.get('deck', []))
            plugin._ss_missing_count = missing
            plugin._ss_missing_restore_timer = GLib.timeout_add_seconds(
                10, _ss_reconcile_missing, plugin, mode)
            LOG.warning('%s Shuffle: resumed saved playlist with %d temporarily unavailable tracks; playlist and saved order unchanged',
                        mode.title(), missing)
        else:
            plugin._schedule_full_playlist()
        LOG.warning('%s Shuffle: recovered %d groups, %d pending groups, %d group ends',
                    mode.title(), len(groups), len(plugin.deck), len(progress.get('ends', [])))
        entry = plugin.player.get_playing_entry()
        if entry and plugin.player.get_playing_source() == target:
            if mode == 'album':
                plugin._queue_follow(entry)
            else:
                plugin._follow_later(entry)
        _ss_schedule_music_follow(plugin, mode)
        return GLib.SOURCE_REMOVE
    except Exception:
        LOG.exception('%s Shuffle: restore attempt encountered error', mode.title())
        return GLib.SOURCE_CONTINUE



import hashlib as _ss_diag_hashlib
import pathlib as _ss_diag_pathlib
import time as _ss_diag_time


# Diagnostic-only event recorder: no changes to playback, selection, or timing.
def _ss_actual_viewport_visibility(view, entry):
    """Is the entry's *selected row* actually inside GtkTreeView's viewport?

    RB.EntryView.get_entry_visible() is unsuitable: its implementation only
    checks whether a cell rectangle has nonzero coordinates and height; it
    does not compare the row with the scrolled viewport.

    Returns None if Gtk has not mapped the tree view / its viewport is unknown.
    """
    if view is None or entry is None:
        return None
    try:
        pending = [view]
        tree = None
        while pending:
            widget = pending.pop()
            if isinstance(widget, Gtk.TreeView):
                tree = widget
                break
            if isinstance(widget, Gtk.Container):
                pending.extend(widget.get_children())
        if tree is None or not tree.get_mapped():
            return None
        selection = tree.get_selection()
        _model, paths = selection.get_selected_rows()
        if len(paths) != 1:
            return False
        visible = tree.get_visible_range()
        if len(visible) == 3:
            success, first, last = visible
            if not success:
                return None
        else:
            first, last = visible
        if first is None or last is None:
            return None
        path = paths[0]
        return first.compare(path) <= 0 and path.compare(last) <= 0
    except Exception:
        LOG.exception('Shuffle: cannot inspect GTK tree viewport')
        return None


def _ss_highlight_diagnostic(plugin, mode, event):
    try:
        player = plugin.player
        source = player.get_playing_source()
        expected = 'Continuous %s Shuffle' % mode.title()
        if source is None or source.props.name != expected:
            return
        music = plugin.object.props.library_source
        page = plugin.object.props.selected_page
        entry = player.get_playing_entry()
        view = music.get_entry_view() if music is not None else None
        if entry is None:
            reason = 'no-playing-entry'
            ident = '-'
        else:
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION) or ''
            ident = _ss_diag_hashlib.sha256(uri.encode('utf-8')).hexdigest()[:10]
            reason = 'entry-present'
        contained = bool(view is not None and entry is not None and
                         view.get_entry_contained(entry))
        viewport_visible = _ss_actual_viewport_visibility(view, entry)
        rb_visible = bool(contained and view.get_entry_visible(entry))
        selected = view.get_selected_entries() if view is not None else []
        selected = selected or []
        selected_match = bool(entry is not None and len(selected) == 1 and
                              selected[0] == entry)
        page_name = page.props.name if page is not None else '(none)'
        if page != music and not event.startswith('page'):
            return
        data = ('%s %s event=%s entry=%s contained=%s selected=%s '
                'viewport_visible=%s rb_visible=%s '
                'selection_count=%d page=%r source=%r reason=%s\n' % (
                    _ss_diag_time.strftime('%Y-%m-%dT%H:%M:%S'), mode, event, ident,
                    contained, selected_match, viewport_visible, rb_visible,
                    len(selected), page_name, source.props.name, reason))
        path = _ss_diag_pathlib.Path(GLib.get_user_cache_dir()) / 'rhythmbox' / 'shuffle-highlight-diagnostic.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 500_000:
            path.write_text('# Log rotated because it exceeded 500 KB\n', encoding='utf-8')
        with path.open('a', encoding='utf-8') as f:
            f.write(data)
    except Exception:
        LOG.exception('%s Shuffle: diagnostic trace failed', mode.title())

# Show Music for explicit New/Resume actions without changing the playback
# source or taking the user away from unrelated manual browsing.
def _ss_show_music(plugin, mode, epoch, previous_page):
    if epoch != plugin._view_callback_epoch:
        return GLib.SOURCE_REMOVE
    try:
        music = plugin.object.props.library_source
        if music is None:
            return GLib.SOURCE_REMOVE
        page = plugin.object.props.selected_page
        # The user may have clicked elsewhere while the scheduled callback was
        # pending.  Do not undo that navigation.
        if page != music and page != plugin.playlist and page != previous_page:
            return GLib.SOURCE_REMOVE
        if page != music:
            plugin.object.props.display_page_tree.select(music)
        _ss_schedule_music_follow(plugin, mode, page_changed=True)
    except Exception:
        LOG.exception('%s Shuffle: could not show Music', mode.title())
    return GLib.SOURCE_REMOVE


# Bring the playing Music row into view, then release control so the user can
# scroll freely.  Retry while the view is not ready, and rearm only for a new
# song, page entry, or query-model replacement.  These callbacks never change
# playback, filters or sidebar page.
def _ss_follow_music_view(plugin, mode, attempt=0):
    plugin._ss_music_follow_id = None
    if attempt in (0, 20):
        _ss_highlight_diagnostic(plugin, mode, 'follow-%d' % attempt)
    try:
        expected_name = ('Continuous Album Shuffle' if mode == 'album'
                         else 'Continuous Folder Shuffle')
        playing_source = plugin.player.get_playing_source()
        bookmark = getattr(plugin, '_bookmark', None)
        page = plugin.object.props.selected_page
        if (bookmark is not None and plugin.playlist is not None and
                page is not None and page.props.name == plugin.playlist.props.name and
                (playing_source is None or playing_source.props.name != expected_name)):
            bookmark.highlight()  # Selection only; never starts other music.
            return GLib.SOURCE_REMOVE
        if playing_source is None or playing_source.props.name != expected_name:
            return GLib.SOURCE_REMOVE

        music = plugin.object.props.library_source
        # Never interfere with manual navigation or an unrelated playlist.
        if music is None or (page != music and page != playing_source):
            return GLib.SOURCE_REMOVE
        entry = plugin.player.get_playing_entry()
        view = page.get_entry_view() if page is not None else None
        if entry is not None and view is not None and view.get_entry_contained(entry):
            selected = view.get_selected_entries() or []
            correct = len(selected) == 1 and selected[0] == entry
            if not correct:
                view.select_none()
                view.select_entry(entry)
            # Check the real GtkTreeView scroll range, NOT Rhythmbox's
            # get_entry_visible(), which can return True for offscreen rows.
            # Never move Music's scroll position unless a playback transition
            # or model/page update has explicitly initiated this follow pass.
            in_viewport = _ss_actual_viewport_visibility(view, entry)
            if not correct or in_viewport is not True:
                # RB.EntryView.scroll_to_entry selects the row as well as
                # asking GtkTreeView to scroll to its location.  When the
                # view is unmapped it returns without scrolling; a subsequent
                # pass when mapped will reissue the request.
                view.scroll_to_entry(entry)
            if attempt == 20 and ((view.get_selected_entries() or []) != [entry] or
                                  _ss_actual_viewport_visibility(view, entry) is not True):
                LOG.warning('%s Shuffle: Music row not selected and in viewport',
                            mode.title())
        elif attempt == 20:
            # A Music filter may intentionally hide the playing entry.  Log it
            # once for troubleshooting, but never modify the current filter.
            LOG.debug('%s Shuffle: playing song not visible in Music view after retry',
                      mode.title())

        # Follow is complete once the playing row is BOTH selected and inside
        # the real GtkTreeView viewport.  Stop immediately: continuing to
        # enforce viewport visibility fights manual scrolling for 4 seconds.
        # A new song, page entry, or entries-replaced event schedules a fresh
        # follow pass if Rhythmbox subsequently changes the model/selection.
        if (entry is not None and view is not None and
                (view.get_selected_entries() or []) == [entry] and
                _ss_actual_viewport_visibility(view, entry) is True):
            return GLib.SOURCE_REMOVE

        # The row is not ready yet.  Retry only while it is missing,
        # unselected or outside the viewport, never after a confirmed success.
        if attempt < 20:
            plugin._ss_music_follow_id = GLib.timeout_add(
                200, _ss_follow_music_view, plugin, mode, attempt + 1)
    except Exception:
        LOG.exception('%s Shuffle: could not follow playing song in current view',
                      mode.title())
        if attempt < 20:
            plugin._ss_music_follow_id = GLib.timeout_add(
                200, _ss_follow_music_view, plugin, mode, attempt + 1)
    return GLib.SOURCE_REMOVE


def _ss_attach_music_view(plugin, mode):
    """Rearm highlighting when Music replaces its song-list query model."""
    try:
        music = plugin.object.props.library_source
        view = music.get_entry_view() if music is not None else None
        old_view = getattr(plugin, '_ss_music_model_view', None)
        if view == old_view:
            return
        old_id = getattr(plugin, '_ss_music_model_handler', None)
        if old_view is not None and old_id is not None:
            old_view.disconnect(old_id)
        diag_old = getattr(plugin, '_ss_diag_sel_handler', None)
        if old_view is not None and diag_old is not None:
            old_view.disconnect(diag_old)
        plugin._ss_music_model_view = view
        plugin._ss_music_model_handler = None
        plugin._ss_diag_sel_handler = None
        plugin._ss_diag_sel_view = view
        if view is not None:
            plugin._ss_music_model_handler = view.connect(
                'entries-replaced',
                lambda *_args: (_ss_highlight_diagnostic(plugin, mode, 'entries-replaced'),
                                _ss_schedule_music_follow(plugin, mode, page_changed=True)))
            plugin._ss_diag_sel_handler = view.connect(
                'selection-changed',
                lambda *_args: _ss_highlight_diagnostic(plugin, mode, 'selection-changed'))
    except Exception:
        LOG.exception('%s Shuffle: could not monitor Music view model', mode.title())


def _ss_schedule_music_follow(plugin, mode, page_changed=False):
    # A new track or sidebar page invalidates pending attempts.  The callback
    # always reads the *current* playing entry, not a stale resume bookmark.
    _ss_attach_music_view(plugin, mode)
    existing = getattr(plugin, '_ss_music_follow_id', None)
    if existing is not None:
        GLib.source_remove(existing)
        plugin._ss_music_follow_id = None
    if page_changed:
        plugin._ss_music_follow_id = GLib.timeout_add(
            75, _ss_follow_music_view, plugin, mode)
    else:
        plugin._ss_music_follow_id = GLib.idle_add(
            _ss_follow_music_view, plugin, mode)

# A deliberately small adaptation of Rhythmbox Session Restore's
# atomic checkpoints, playback-time sampling, and verified deferred seek.
# Embedded in each independently installable shuffle plugin.
import time as _bookmark_time


class _ShuffleBookmark:
    """Per-playlist bookmark. Never edits the generated playlist or its order."""

    TICK_SECONDS = 2
    SEEK_RETRIES = 40

    def __init__(self, plugin, mode):
        self.plugin = plugin
        self.mode = mode
        self.path = _ss_root() / ('%s-bookmark.json' % mode)
        self.saved = _ss_read(self.path)
        if not isinstance(self.saved, dict) or self.saved.get('version') != 1:
            self.saved = None
        self.seek_timer = None
        self.seek_uri = None
        self.seek_seconds = 0
        self.seek_attempts = 0
        self.seek_generation = None
        self.skip_resume_arrival = False
        self.last_saved_at = 0
        self.suspended = False
        # The last source we actually observed playing, not the source now
        # reported by Rhythmbox after it starts switching to another track.
        self._observed_source = None
        self._observed_uri = None
        self.timer = GLib.timeout_add_seconds(self.TICK_SECONDS, self._tick)
        self.source_handler = plugin.player.connect('playing-source-changed',
                                                    self._on_source_changed)
        # Rhythmbox publishes the old stream's position at subsecond
        # resolution.  Keep it in memory until a playback transition occurs;
        # never write to disk at the high-frequency signal rate.
        self.nano_handler = plugin.player.connect('elapsed-nano-changed',
                                                  self._on_elapsed_nano_changed)
        self.playing_handler = plugin.player.connect('playing-changed',
                                                     self._on_playing_changed)
        # Rhythmbox deliberately does NOT emit playing-source-changed when
        # switching from a normal source into the play queue.  It does notify
        # playing-from-queue before the new stream opens, so catch that too.
        self.queue_handler = plugin.player.connect('notify::playing-from-queue',
                                                   self._on_queue_changed)
        # Last-resort flush for transitions lacking either early notification.
        self.song_handler = plugin.player.connect('playing-song-changed',
                                                  self._on_song_changed)
        self.window = plugin.object.props.window
        self.hide_handler = (self.window.connect('hide', self._on_window_hide)
                             if self.window is not None else None)

    @staticmethod
    def _time(player):
        try:
            result = player.get_playing_time()
            return max(0, int(result[-1] if isinstance(result, tuple) else result))
        except Exception:
            return 0

    @staticmethod
    def _backend_time_ns(player):
        """Read the audio backend directly (the shell clock rounds to seconds).

        Return None if the backend no longer has a seekable/current stream;
        most importantly, never turn an unavailable position into zero.
        """
        try:
            backend = player.props.player
            ns = backend.get_time()
            if isinstance(ns, int) and 0 <= ns < 10**17:
                return ns
        except Exception:
            pass
        return None

    @classmethod
    def _precise_time_ns(cls, player):
        ns = cls._backend_time_ns(player)
        if ns is not None:
            return ns
        try:
            result = player.get_playing_time()
            if isinstance(result, tuple):
                if result and result[0] is False:
                    return None
                result = result[-1]
            if result is None:
                return None
            seconds = int(result)
            return seconds * 1_000_000_000 if seconds >= 0 else None
        except Exception:
            return None

    def _record(self, uri, ns, force=False, advance=False):
        """Save only a proven track/position in this shuffle generation."""
        plugin = self.plugin
        generation = getattr(plugin, '_ss_generation', None)
        if not generation or not uri or not isinstance(ns, int) or ns < 0:
            return False
        if (self.saved and self.saved.get('generation') == generation and
                self.saved.get('uri') == uri and not advance):
            index = self.saved.get('index', -1)
        else:
            index = self._playlist_index(uri, advance=advance)
        if not isinstance(index, int) or index < 0:
            return False
        # Rhythmbox's set_playing_time API only accepts whole seconds.  Keep
        # the precise snapshot for auditability and round for the next seek.
        seconds = (ns + 500_000_000) // 1_000_000_000
        self.saved = {'version': 1, 'generation': generation, 'uri': uri,
                      'index': index, 'position': seconds,
                      'position_ns': ns, 'saved_at': int(_bookmark_time.time())}
        if force or _bookmark_time.monotonic() - self.last_saved_at >= 10:
            self._persist()
        return True

    def _persist(self):
        if self.saved is None:
            return
        try:
            _ss_atomic(self.path, self.saved)
            self.last_saved_at = _bookmark_time.monotonic()
        except Exception:
            LOG.exception('%s Shuffle: unable to save bookmark', self.mode.title())

    def reset(self):
        self.cancel_seek()
        self.saved = None

    def start_new(self):
        """Called only after the original new-shuffle operation succeeds."""
        self.cancel_seek()
        player = self.plugin.player
        playlist = self.plugin.playlist
        if playlist is None:
            return
        # The first row is the known new-shuffle start; the backend might not
        # have published it yet, or might still report the old playlist entry.
        entry = getattr(self.plugin, '_shuffle_start_entry', None)
        if entry is None:
            model = _ss_full_playlist_model(playlist)
            entry = next((row[0] for row in model), None) if model is not None else None
        if entry is None:
            LOG.error('%s Shuffle: could not bookmark new shuffle: first entry unavailable',
                      self.mode.title())
            return
        generation = getattr(self.plugin, '_ss_generation', None)
        if not generation:
            return
        uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
        self.saved = {'version': 1, 'generation': generation, 'uri': uri,
                      'index': 0, 'position': 0, 'position_ns': 0,
                      'saved_at': int(_bookmark_time.time())}
        self._observed_source = playlist
        self._observed_uri = uri
        self._persist()

    def _playlist_index(self, uri, advance=False):
        model = _ss_full_playlist_model(self.plugin.playlist)
        if model is None:
            return -1
        old = self.saved or {}
        old_index = old.get('index', -1)
        matches = []
        for i, row in enumerate(model):
            if row[0].get_string(RB.RhythmDBPropType.LOCATION) == uri:
                matches.append(i)
        if not matches:
            return -1
        if old.get('uri') == uri and old_index in matches:
            if not advance:
                return old_index
            # A new playing-song-changed event with the same URI can mean the
            # next occurrence in a second continuous shuffle cycle.
            later = [i for i in matches if i > old_index]
            return later[0] if later else matches[0]
        if isinstance(old_index, int) and old_index >= 0:
            # Favor the next occurrence when a continuous cycle repeats.
            return min(matches, key=lambda i: (abs(i - (old_index + 1)), i))
        return matches[0]

    def observe(self, entry=None, force=False, advance=False, position_ns=None):
        """Record a live position only while THIS shuffle owns playback."""
        if self.suspended or self.seek_uri is not None:
            return
        plugin = self.plugin
        if not plugin.active or plugin.playlist is None:
            return
        player = plugin.player
        if player.get_playing_source() != plugin.playlist:
            return
        if entry is None:
            entry = player.get_playing_entry()
        if entry is None:
            return
        uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
        if not uri:
            return
        ns = position_ns if position_ns is not None else self._precise_time_ns(player)
        if ns is None:
            return   # Do not replace a valid bookmark with a fabricated 0.
        if self._record(uri, ns, force=force, advance=advance):
            self._observed_source = plugin.playlist
            self._observed_uri = uri

    def _tick(self):
        # Two-second fallback/checkpoint; normal playback updates the memory
        # snapshot much more frequently through elapsed-nano-changed.
        self.observe()
        return GLib.SOURCE_CONTINUE

    def _on_elapsed_nano_changed(self, _player, elapsed_ns):
        if not isinstance(elapsed_ns, int) or elapsed_ns < 0:
            return
        # No disk write per tick. _record still checkpoints every 10 seconds.
        self.observe(position_ns=elapsed_ns)

    def _on_playing_changed(self, _player, playing):
        # Capture a deliberate pause immediately, too.  This is independent
        # of whether the user ever switches away from the shuffle.
        if not playing:
            self.observe(force=True)

    def _capture_departure(self, new_source, backend_still_old=True):
        """Freeze outgoing bookmark, never reading a replacement stream's clock."""
        plugin = self.plugin
        if self.seek_uri is not None and new_source != plugin.playlist:
            # A superseded Resume must not overwrite the original bookmark.
            self.cancel_seek()
        if (new_source == plugin.playlist or self.suspended or
                self._observed_source != plugin.playlist):
            return
        saved = self.saved
        if (saved and saved.get('generation') == getattr(plugin, '_ss_generation', None)
                and saved.get('uri') == self._observed_uri):
            if backend_still_old:
                entry = plugin.player.get_playing_entry()
                old_uri = (entry.get_string(RB.RhythmDBPropType.LOCATION)
                           if entry is not None else None)
                if old_uri == self._observed_uri:
                    # Early source notification: the old audio stream is still
                    # open, even though get_playing_source() is already new.
                    ns = self._backend_time_ns(plugin.player)
                    if ns is not None:
                        self._record(old_uri, ns, force=True)
            # The newest elapsed-nano snapshot is the safe fallback when the
            # backend or old entry has already disappeared (e.g. Stop).
            self._persist()
        self._observed_source = None
        self._observed_uri = None

    def _on_source_changed(self, _player, new_source):
        # On ordinary playback switches Rhythmbox emits this before opening
        # the new song: backend.get_time() still reports the outgoing position.
        self._capture_departure(new_source)

    def _on_queue_changed(self, *_args):
        # Queue switches can omit playing-source-changed entirely.  This
        # notification also arrives before the new stream replaces the old.
        self._capture_departure(self.plugin.player.get_playing_source())

    def _on_song_changed(self, _player, _entry):
        # Last resort if a backend/source switch lacks an early notification.
        # Here the new stream may already be active: flush only the previously
        # sampled position rather than accidentally reading the NEW clock.
        self._capture_departure(self.plugin.player.get_playing_source(),
                                backend_still_old=False)

    def _on_window_hide(self, *_args):
        # Session Restore captures its final checkpoint here as well; doing so
        # BEFORE Rhythmbox stops audio avoids losing the last playback position.
        self.observe(force=True)

    def _playlist_for_saved(self):
        plugin = self.plugin
        if _ss_registered_playlist(plugin, plugin.playlist):
            _ss_bind_playlist(plugin, self.mode, plugin.playlist)
            return plugin.playlist
        name = PLAYLIST_NAME
        for playlist in plugin.object.props.playlist_manager.get_playlists():
            if isinstance(playlist, RB.StaticPlaylistSource) and playlist.props.name == name:
                _ss_bind_playlist(plugin, self.mode, playlist)
                return playlist
        return None

    def valid(self):
        """Prevent bookmarks from resuming the wrong shuffle generation."""
        saved = self.saved
        if not isinstance(saved, dict):
            return False
        plugin = self.plugin
        generation = getattr(plugin, '_ss_generation', None)
        if not generation or generation != saved.get('generation'):
            return False
        playlist = self._playlist_for_saved()
        if playlist is None:
            return False
        entry = plugin.object.props.db.entry_lookup_by_location(saved.get('uri', ''))
        if entry is None:
            return False
        model = _ss_full_playlist_model(playlist)
        if model is None:
            return False
        # The shuffled source can be long, but validating it occurs only when
        # opening the dialog / resuming, not on every playback clock tick.
        return any(row[0].get_string(RB.RhythmDBPropType.LOCATION) == saved['uri']
                   for row in model)

    def may_still_load(self):
        # A saved shuffle may exist before Rhythmbox reloads its long playlist.
        # Never silently replace it merely because valid() is temporarily false.
        saved = self.saved
        if not saved or not saved.get('generation'):
            return False
        progress = _ss_read(_ss_paths(self.mode)[1])
        generation = getattr(self.plugin, '_ss_generation', None)
        return bool(progress and (progress.get('active') or progress.get('resumable')) and
                    progress.get('generation') == saved.get('generation') and
                    generation in (None, saved['generation']))

    def cancel_seek(self):
        if self.seek_timer is not None:
            GLib.source_remove(self.seek_timer)
            self.seek_timer = None
        self.seek_uri = None
        self.skip_resume_arrival = False


    def is_resume_arrival(self, entry):
        if entry is None or not self.skip_resume_arrival or self.seek_uri is None:
            return False
        if self.plugin.player.get_playing_source() != self.plugin.playlist:
            return False
        if entry.get_string(RB.RhythmDBPropType.LOCATION) != self.seek_uri:
            return False
        self.skip_resume_arrival = False
        return True

    def _finish_seek(self):
        self.seek_attempts += 1
        player = self.plugin.player
        try:
            if self.seek_generation != getattr(self.plugin, '_ss_generation', None):
                raise RuntimeError('shuffle generation changed')
            entry = player.get_playing_entry()
            if entry is None or player.get_playing_source() != self.plugin.playlist:
                raise RuntimeError('waiting for bookmarked shuffle playback')
            uri = entry.get_string(RB.RhythmDBPropType.LOCATION)
            if uri != self.seek_uri:
                raise RuntimeError('current track is not bookmarked track')
            state = player.get_playing()
            playing = bool(state[-1] if isinstance(state, tuple) else state)
            if not playing:
                raise RuntimeError('waiting for stream to start')
            if self.seek_seconds:
                accepted = player.set_playing_time(self.seek_seconds)
                if accepted is False:
                    raise RuntimeError('backend not yet ready to seek')
                if abs(self._time(player) - self.seek_seconds) > 3:
                    raise RuntimeError('seek not yet established')
            self.seek_timer = None
            self.seek_uri = None

            self.observe(force=True)
            return GLib.SOURCE_REMOVE
        except Exception as exc:
            if self.seek_attempts in (1, 5, 15):
                LOG.warning('%s Shuffle: waiting to resume at %ss (%s)',
                            self.mode.title(), self.seek_seconds, exc)
            if self.seek_attempts >= self.SEEK_RETRIES:
                LOG.warning('%s Shuffle: resume seek unavailable; keeping current playback',
                            self.mode.title())
                self.cancel_seek()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

    def resume(self):
        if not self.valid():
            LOG.warning('%s Shuffle: no valid playlist bookmark to resume', self.mode.title())
            return False
        self.cancel_seek()
        plugin = self.plugin
        saved = self.saved.copy()
        playlist = self._playlist_for_saved()
        entry = plugin.object.props.db.entry_lookup_by_location(saved['uri'])
        self.seek_uri = saved['uri']
        self.seek_seconds = max(0, int(saved.get('position', 0)))
        self.seek_generation = saved['generation']
        self.seek_attempts = 0
        self.skip_resume_arrival = True
        plugin.playlist = playlist
        plugin.active = True
        plugin.replenishing = False
        plugin._view_callback_epoch += 1  # Invalidate superseded shuffle UI work.

        try:
            previous_page = plugin.object.props.selected_page
            _ss_cancel_pending_session_restore(plugin)
            plugin.player.play_entry(entry, playlist)
            _ss_highlight_diagnostic(plugin, self.mode, 'resume-play-entry')
            if self.seek_timer is None:
                self.seek_timer = GLib.timeout_add(200, self._finish_seek)
            # Playback stays on the generated playlist.  The selected view is
            # Music, including on Resume from another source or after startup.
            _ss_show_music(plugin, self.mode, plugin._view_callback_epoch, previous_page)
            GLib.idle_add(_ss_show_music, plugin, self.mode,
                          plugin._view_callback_epoch, previous_page)
            LOG.warning('%s Shuffle: resuming %s at %ds (index %d)',
                        self.mode.title(), saved['uri'], self.seek_seconds,
                        saved.get('index', -1))
            return True
        except Exception:
            self.cancel_seek()
            LOG.exception('%s Shuffle: could not resume bookmarked track', self.mode.title())
            return False

    def highlight(self):
        """Scroll to bookmark without interrupting unrelated playback."""
        if not self.valid():
            return
        plugin = self.plugin
        playlist = self._playlist_for_saved()
        page = plugin.object.props.selected_page
        if page is None or page.props.name != playlist.props.name:
            return
        entry = plugin.object.props.db.entry_lookup_by_location(self.saved['uri'])
        view = page.get_entry_view()
        if view is None or not view.get_entry_contained(entry):
            return
        view.select_none()
        view.select_entry(entry)
        view.scroll_to_entry(entry)

    def close(self):
        # Capture the last sampled state even if another source is now playing.
        self.observe(force=True)
        self._persist()
        self.cancel_seek()
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        for name in ('source_handler', 'nano_handler', 'playing_handler',
                     'queue_handler', 'song_handler'):
            handler = getattr(self, name, None)
            if handler is not None:
                self.plugin.player.disconnect(handler)
                setattr(self, name, None)
        if self.hide_handler is not None and self.window is not None:
            self.window.disconnect(self.hide_handler)
            self.hide_handler = None


def _ss_cancel_pending_session_restore(plugin):
    """Explicit shuffle choice supersedes Session Restore's pending first Play.

    The action is optional: both shuffle plugins remain standalone when Session
    Restore is disabled or an older version is installed.  Activation is
    synchronous, before play_entry(), so no delayed correction can steal it.
    """
    try:
        app = plugin.app or Gio.Application.get_default()
        action = app.lookup_action('session-restore-abandon-pending') if app else None
        if action is not None:
            action.activate(None)
    except Exception:
        LOG.exception('Shuffle: could not signal Session Restore before playback')



# --- Constant-time generated playlist replacement (test14) ---
# The previous method deleted every old track through StaticPlaylistSource,
# which is impractical for a 20k-40k track playlist and can be invalidated by
# browser filters.  Keep the old source available for rollback; build the new
# shuffle in an empty source, then delete the old source only after playback
# and the on-disk bookmark have been verified.

# --- End Rhythmbox's automatic inline renaming of generated playlists. ---
# RBPlaylistManager.new_playlist() schedules edit_new_playlist_name() in a
# GTK idle callback even when a descriptive name was supplied.  This is
# appropriate for user-created playlists, but not for plugin-generated ones.
def _ss_finish_generated_playlist_name(plugin, playlist):
    if (getattr(plugin, 'playlist', None) is not playlist or
            playlist is None or playlist.props.name != PLAYLIST_NAME):
        return GLib.SOURCE_REMOVE
    try:
        def find_tree(widget):
            if isinstance(widget, Gtk.TreeView):
                return widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    found = find_tree(child)
                    if found is not None:
                        return found
            return None

        tree = find_tree(plugin.object.props.display_page_tree)
        if tree is None:
            return GLib.SOURCE_REMOVE
        # Only cancel the playlist manager's immediately initiated edit;
        # never interfere with subsequent explicit renaming by the user.
        for col in tree.get_columns():
            for cell in col.get_cells():
                if isinstance(cell, Gtk.CellRendererText) and cell.get_property('editable'):
                    cell.stop_editing(True)
                    cell.set_property('editable', False)
                    cursor, _col = tree.get_cursor()
                    if cursor is not None:
                        tree.set_cursor(cursor, None, False)
                    return GLib.SOURCE_REMOVE
    except Exception:
        LOG.exception('Could not finish generated playlist name editor')
    return GLib.SOURCE_REMOVE

def _ss_stage_playlist_swap(plugin, mode, pending):
    previous = pending['before'].get('playlist')
    if not _ss_registered_playlist(plugin, previous):
        return
    if previous.props.name != PLAYLIST_NAME:
        raise RuntimeError('previous generated playlist was renamed externally')
    nonce = _ss_uuid.uuid4().hex[:10]
    temporary_name = '%s (previous %s)' % (PLAYLIST_NAME, nonce)
    previous.props.name = temporary_name
    if previous.props.name != temporary_name:
        raise RuntimeError('could not temporarily rename previous shuffle playlist')
    # The managed source remains intact; unlike clearing its rows, this has
    # no effect on its saved membership or any native filtered model.
    pending['swap'] = {'previous': previous, 'new': None,
                       'previous_name': PLAYLIST_NAME}
    _ss_unbind_playlist(plugin)
    plugin.playlist = None


def _ss_rollback_playlist_swap(plugin, mode, pending):
    swap = pending.get('swap')
    if not swap:
        return
    previous = swap['previous']
    candidate = plugin.playlist
    plugin._cancel_full_playlist()
    # Suppress our explicit-deletion handler while removing an uncommitted
    # candidate. A user's Delete Playlist operation is handled separately.
    if candidate is not None and candidate != previous and _ss_registered_playlist(plugin, candidate):
        _ss_unbind_playlist(plugin)
        try:
            if plugin.player.get_playing_source() == candidate:
                plugin.player.stop()
            candidate.delete_thyself()
        except Exception:
            LOG.exception('%s Shuffle: could not remove failed new playlist', mode.title())
    if _ss_registered_playlist(plugin, previous):
        previous.props.name = swap['previous_name']
        plugin.playlist = previous
        _ss_bind_playlist(plugin, mode, previous)
    else:
        plugin.playlist = None
    pending['swap'] = None


def _ss_commit_playlist_swap(plugin, mode, pending):
    swap = pending.get('swap')
    if not swap:
        return
    previous = swap['previous']
    pending['swap'] = None
    # Old source was unbound before creating the candidate; this intentional
    # cleanup must never trigger the explicit-user-deletion state reset.
    try:
        if _ss_registered_playlist(plugin, previous):
            previous.delete_thyself()
    except Exception:
        # New shuffle and bookmark have already committed successfully;
        # leaving a clearly labeled previous source is safer than rolling
        # back verified playback merely because removal was refused.
        LOG.exception('%s Shuffle: could not remove prior playlist after replacement',
                      mode.title())

def _ss_wrap(cls, mode):
    original_start = cls.on_start
    original_song = cls.on_song_changed
    original_append = cls._append_next_idle if mode == 'album' else cls._append_idle

    def activate(self):
        self._ss_incomplete_restore = False
        self._ss_missing_restore_timer = None
        self._ss_pending_manifest = None
        self._ss_pending_deck = None
        self._ss_missing_count = 0
        self._ss_pending_start = None
        self._ss_watched_playlist = None
        self._ss_watched_playlist_handler = None
        self._ss_discarding = False
        manager = self.object.props.playlist_manager
        self._ss_playlist_manager = manager
        self._ss_playlist_added_handler = manager.connect(
            'playlist-added', lambda _mgr, source: _ss_playlist_announced(self, mode, source))
        _ss_watch_existing_playlist(self, mode)
        self._bookmark = _ShuffleBookmark(self, mode)
        LOG.warning('%s Shuffle: resume support initialized (bookmark=%s)',
                    mode.title(), bool(self._bookmark.saved))
        self._resume_action = Gio.SimpleAction.new('%s-shuffle-resume' % mode, None)
        self._resume_action.connect('activate', lambda *_args: self._bookmark.resume())
        self.app.add_action(self._resume_action)
        self.app.add_plugin_menu_item('tools', '%s-shuffle-resume' % mode,
            Gio.MenuItem.new('Resume %s Shuffle' % mode.title(),
                             'app.%s-shuffle-resume' % mode))
        self._ss_restored = False
        self._ss_generation = None
        self._ss_deadline = _ss_time.monotonic() + 600
        self._ss_restore_timer = GLib.timeout_add_seconds(3, _ss_restore, self, mode)
        GLib.idle_add(lambda: (_ss_restore(self, mode), GLib.SOURCE_REMOVE)[1])
        # Follow the playing row when the user returns to Music, too.
        self._ss_music_follow_id = None
        self._ss_music_model_view = None
        self._ss_music_model_handler = None
        self._ss_diag_sel_handler = None
        self._ss_diag_sel_view = None
        self._ss_music_page_handler = self.object.connect(
            'notify::selected-page',
            lambda *_args: (_ss_highlight_diagnostic(self, mode, 'page-change'),
                            _ss_schedule_music_follow(self, mode, page_changed=True)))
        self._ss_music_source_handler = self.player.connect(
            'playing-source-changed',
            lambda *_args: _ss_schedule_music_follow(self, mode))
        _ss_schedule_music_follow(self, mode)

    def deactivate(self):
        _ss_cancel_missing_recovery(self)
        pending = getattr(self, '_ss_pending_start', None)
        if pending is not None:
            _ss_cancel_pending_start(self)
            _ss_rollback_unverified_start(self, mode, pending)
            if self._bookmark is not None:
                self._bookmark.suspended = False
        if getattr(self, '_ss_playlist_added_handler', None) is not None:
            self._ss_playlist_manager.disconnect(self._ss_playlist_added_handler)
            self._ss_playlist_added_handler = None
        _ss_unbind_playlist(self)
        if self._resume_dialog is not None:
            self._resume_dialog.destroy()
            self._resume_dialog = None
        if self._bookmark is not None:
            self._bookmark.close()
            self._bookmark = None
        if self._resume_action is not None:
            self.app.remove_plugin_menu_item('tools', '%s-shuffle-resume' % mode)
            self.app.remove_action('%s-shuffle-resume' % mode)
            self._resume_action = None
        self._cancel_full_playlist()
        if getattr(self, '_ss_music_follow_id', None) is not None:
            GLib.source_remove(self._ss_music_follow_id)
            self._ss_music_follow_id = None
        if (getattr(self, '_ss_music_model_view', None) is not None and
                getattr(self, '_ss_music_model_handler', None) is not None):
            self._ss_music_model_view.disconnect(self._ss_music_model_handler)
            self._ss_music_model_view = None
            self._ss_music_model_handler = None
        if (getattr(self, '_ss_diag_sel_view', None) is not None and
                getattr(self, '_ss_diag_sel_handler', None) is not None):
            self._ss_diag_sel_view.disconnect(self._ss_diag_sel_handler)
            self._ss_diag_sel_handler = None
            self._ss_diag_sel_view = None
        if getattr(self, '_ss_music_page_handler', None) is not None:
            self.object.disconnect(self._ss_music_page_handler)
            self._ss_music_page_handler = None
        if getattr(self, '_ss_music_source_handler', None) is not None:
            self.player.disconnect(self._ss_music_source_handler)
            self._ss_music_source_handler = None
        _ss_progress(self, mode)  # Must occur BEFORE original sets active=False.
        if getattr(self, '_ss_restore_timer', None) is not None:
            try:
                GLib.source_remove(self._ss_restore_timer)
            except Exception:
                pass
            self._ss_restore_timer = None
        try:
            _ss_paths(mode)[2].unlink(missing_ok=True)
        except OSError:
            LOG.exception('%s Shuffle: unable to clear readiness file', mode.title())
        # The registered do_deactivate() performs its original cleanup next.

    def _start_fresh_shuffle(self, *args):
        bookmark = self._bookmark
        if bookmark is not None:
            bookmark.suspended = True
            bookmark.cancel_seek()
        # The handlers are installed BEFORE play_entry: a source change can be
        # emitted synchronously, but a stream start may arrive later.
        _ss_cancel_pending_start(self)
        before = _ss_snapshot_before_start(self, mode)
        pending = {'armed': False, 'timer': None, 'handlers': [],
                   'before': before, 'deadline': _ss_time.monotonic() + 20}
        self._ss_pending_start = pending
        pending['handlers'].append(self.player.connect(
            'playing-song-changed',
            lambda *_args: GLib.idle_add(_ss_verify_new_playback, self, mode)))
        pending['handlers'].append(self.player.connect(
            'playing-changed',
            lambda *_args: GLib.idle_add(_ss_verify_new_playback, self, mode)))
        try:
            # Explicit New Shuffle supersedes any Session Restore first-Play
            # correction.  Deliver this before original_start calls play_entry.
            _ss_cancel_pending_session_restore(self)
            old_source = before['playlist']
            source = (self._current_filtered_source() if mode == 'album'
                      else self._source())
            if old_source is not None and source == old_source:
                source = self.last_source or self.object.props.library_source
            self._ss_start_source_override = source
            if source is None or source.get_entry_view() is None:
                raise RuntimeError('no valid source for New Shuffle')
            model = source.get_entry_view().props.model
            if model is None or not any(True for _ in model):
                raise RuntimeError('selected source contains no visible tracks')
            _ss_stage_playlist_swap(self, mode, pending)
            try:
                result = original_start(self, *args)
            finally:
                self._ss_start_source_override = None
            if result is not True or self.playlist is None:
                _ss_cancel_pending_start(self)
                _ss_rollback_unverified_start(self, mode, pending)
                if bookmark is not None:
                    bookmark.suspended = False
                return result
            first = getattr(self, '_shuffle_start_entry', None)
            if first is None:
                _ss_cancel_pending_start(self)
                _ss_rollback_unverified_start(self, mode, pending)
                if bookmark is not None:
                    bookmark.suspended = False
                return False
            pending['playlist'] = self.playlist
            pending['uri'] = first.get_string(RB.RhythmDBPropType.LOCATION)
            pending['armed'] = True
            # Do not fill more rows or write a bookmark until playback is real.
            self._cancel_full_playlist()
            if _ss_verify_new_playback(self, mode) == GLib.SOURCE_CONTINUE:
                pending['timer'] = GLib.timeout_add(
                    150, _ss_verify_new_playback, self, mode)
            return True
        except Exception:
            _ss_cancel_pending_start(self)
            _ss_rollback_unverified_start(self, mode, pending)
            if bookmark is not None:
                bookmark.suspended = False
            raise

    def _dialog_response(self, dialog, response):
        self._resume_dialog = None
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            if self._bookmark is not None and self._bookmark.valid():
                self._bookmark.resume()
        elif response == Gtk.ResponseType.NO:
            _start_fresh_shuffle(self)

    def _existing_generated_playlist(self):
        """Never silently erase a generated playlist if its bookmark is missing."""
        playlist = self.playlist if _ss_registered_playlist(self, self.playlist) else None
        if playlist is None:
            for candidate in self.object.props.playlist_manager.get_playlists():
                if candidate.props.name == PLAYLIST_NAME:
                    playlist = candidate
                    break
        if playlist is None:
            return False
        model = _ss_full_playlist_model(playlist)
        return model is None or any(True for _ in model)

    def start(self, *args):
        """The toolbar and Tools entry share a resume-or-reshuffle dialog."""
        if self._resume_dialog is not None:
            self._resume_dialog.present()
            return
        bookmark = self._bookmark
        ready = bookmark is not None and bookmark.valid()
        loading = bookmark is not None and bookmark.may_still_load()
        if not ready and not loading and not self._ss_restored:
            pending = _ss_read(_ss_paths(mode)[1])
            if pending and (pending.get('active') or pending.get('resumable')):
                manifest = _ss_read(_ss_paths(mode)[0])
                loading = bool(manifest and
                               pending.get('generation') == manifest.get('generation'))
        existing = _existing_generated_playlist(self)
        if not ready and not loading and not existing:
            LOG.warning('%s Shuffle: no existing playlist/bookmark, starting new shuffle',
                        mode.title())
            return _start_fresh_shuffle(self, *args)
        LOG.warning('%s Shuffle: prompting: ready=%s, loading=%s, existing=%s',
                    mode.title(), ready, loading, existing)
        dialog = Gtk.MessageDialog(
            transient_for=self.object.props.window,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text='%s Shuffle: resume or start fresh?' % mode.title())
        if ready:
            dialog.format_secondary_text(
                'Resume returns to the bookmarked track at its saved playback time, '
                'keeping the existing playlist order. New Shuffle replaces that '
                'playlist using the currently selected library filters.')
        elif loading:
            dialog.format_secondary_text(
                'Rhythmbox is still loading this saved shuffle. Resume will become '
                'available automatically as soon as the playlist and bookmark are '
                'ready. You can also Cancel or explicitly start a New Shuffle.')
        else:
            dialog.format_secondary_text(
                'The generated shuffle playlist still exists, but it has no valid '
                'bookmark. Cancel to preserve it, or explicitly generate a new '
                'shuffle using the current filters.')
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.add_button('New Shuffle', Gtk.ResponseType.NO)
        if ready or loading:
            resume_button = dialog.add_button('Resume', Gtk.ResponseType.YES)
            resume_button.set_sensitive(bool(ready))
            if ready:
                dialog.set_default_response(Gtk.ResponseType.YES)
        dialog.connect('response', lambda dlg, response: _dialog_response(self, dlg, response))
        self._resume_dialog = dialog
        dialog.show()

    def song_changed(self, *args):
        if getattr(self, '_ss_pending_start', None) is not None:
            return None
        bookmark = self._bookmark
        entry = args[-1] if args else None
        if (bookmark is not None and bookmark.seek_uri is not None and
                entry is not None and self.player.get_playing_source() == self.playlist and
                entry.get_string(RB.RhythmDBPropType.LOCATION) != bookmark.seek_uri):
            # A manual track change supersedes a pending Resume seek.
            bookmark.cancel_seek()
        if bookmark is not None and bookmark.is_resume_arrival(entry):
            # Resuming *onto* a group's last track is not completing that group.
            if mode == 'album':
                self._queue_follow(entry)
            else:
                self._follow_later(entry)
            result = None
        else:
            result = original_song(self, *args)
        if bookmark is not None:
            bookmark.observe(entry, force=True, advance=True)
        _ss_schedule_music_follow(self, mode)
        _ss_progress(self, mode)
        return result

    def append(self, *args):
        result = original_append(self, *args)
        _ss_progress(self, mode)
        return result

    cls._resume_activate = activate
    cls._resume_deactivate = deactivate
    cls.on_start = start
    cls.on_song_changed = song_changed
    if mode == 'album':
        cls._append_next_idle = append
    else:
        cls._append_idle = append

_ss_wrap(ContinuousAlbumShuffle, 'album')
