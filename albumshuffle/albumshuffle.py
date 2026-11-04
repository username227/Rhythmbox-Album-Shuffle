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
        button.set_tooltip_text('Shuffle Albums — start a fresh random album sequence')
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
        model = playlist.get_query_model()
        if model is None:
            return
        for entry in [row[0] for row in model]:
            playlist.remove_entry(entry)

    def on_start(self, *_args):
        self._cancel_full_playlist()
        try:
            shell = self.object
            source = self._current_filtered_source()
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
            self.player.play_entry(first_entry, self.playlist)
            GLib.idle_add(self._restore_browser_view, source, first_entry)
            self._schedule_full_playlist()
            LOG.warning('Album Shuffle: %d albums, %d eligible tracks from %d displayed '
                        '(%d excluded); first album: %s',
                        len(keys), self._full_playlist_expected_tracks, len(entries),
                        len(entries) - self._full_playlist_expected_tracks,
                        first_entry.get_string(RB.RhythmDBPropType.ALBUM))
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
            actual = sum(1 for _row in self.playlist.get_query_model())
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
                self.active = False
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


def _ss_progress(plugin, mode):
    if getattr(plugin, '_ss_generation', None) is None:
        return
    try:
        keys = list(plugin.deck)
        ends = getattr(plugin, 'album_ends' if mode == 'album' else 'folder_ends')
        playlist = getattr(plugin, 'playlist', None)
        last_source = getattr(plugin, 'last_source', None)
        state = {'version': 1, 'generation': plugin._ss_generation,
                 'active': bool(plugin.active),
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
    if getattr(plugin, '_ss_restore_timer', None) is not None:
        try:
            GLib.source_remove(plugin._ss_restore_timer)
        except Exception:
            pass
        plugin._ss_restore_timer = None
    plugin._ss_restored = True
    if not plugin.active:
        _ss_progress(plugin, mode)
        return
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
    except Exception:
        LOG.exception('%s Shuffle: failed to save initial shuffle order', mode.title())


def _ss_ready(plugin, mode):
    try:
        _ss_atomic(_ss_paths(mode)[2], {'pid': _ss_os.getpid(),
                   'generation': plugin._ss_generation, 'active': bool(plugin.active)})
    except Exception:
        LOG.exception('%s Shuffle: could not publish readiness', mode.title())


def _ss_restore(plugin, mode):
    if getattr(plugin, '_ss_restored', False):
        plugin._ss_restore_timer = None
        return GLib.SOURCE_REMOVE
    manifest = _ss_read(_ss_paths(mode)[0])
    progress = _ss_read(_ss_paths(mode)[1])
    if manifest is None or progress is None or not progress.get('active'):
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
        if target is None or target.get_query_model() is None:
            return GLib.SOURCE_CONTINUE
        database = plugin.object.props.db
        groups = {}
        for group in manifest['groups']:
            key = _ss_decode(group['key'])
            entries = []
            for uri in group['uris']:
                entry = database.entry_lookup_by_location(uri)
                if entry is None:
                    return GLib.SOURCE_CONTINUE  # NAS or DB still loading
                entries.append(entry)
            if entries:
                groups[key] = entries
        if not groups:
            return GLib.SOURCE_CONTINUE
        # Avoid restoring stale progress over a newer shuffle started by user.
        if getattr(plugin, '_ss_restored', False):
            return GLib.SOURCE_REMOVE
        plugin.playlist = target
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
        # Finish any unbuilt portion of an earlier rolling or interrupted queue.
        plugin._full_playlist_expected_tracks = sum(len(v) for v in groups.values())
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



# Follow the visible song row when playback originates from OUR generated
# shuffle playlist, even if the shuffle was resumed manually after playing
# something else (which leaves the scheduling engine's active flag False).
# This must not change playback, selected sources, filters, or playlist content.
def _ss_follow_music_view(plugin, mode):
    plugin._ss_music_follow_id = None
    try:
        expected_name = ('Continuous Album Shuffle' if mode == 'album'
                         else 'Continuous Folder Shuffle')
        playing_source = plugin.player.get_playing_source()
        if playing_source is None or playing_source.props.name != expected_name:
            return GLib.SOURCE_REMOVE

        music = plugin.object.props.library_source
        page = plugin.object.props.selected_page
        # Only the Music page and the exact active shuffle playlist qualify.
        # Do not interfere with any other playlist, or with manual browsing.
        if music is None or (page != music and page != playing_source):
            return GLib.SOURCE_REMOVE
        entry = plugin.player.get_playing_entry()
        if entry is None:
            return GLib.SOURCE_REMOVE
        view = page.get_entry_view()
        if view is None or not view.get_entry_contained(entry):
            return GLib.SOURCE_REMOVE  # The user's current filter hides it.

        selected = view.get_selected_entries() or []
        if not (len(selected) == 1 and selected[0] == entry):
            view.select_none()
            view.select_entry(entry)
        view.scroll_to_entry(entry)
    except Exception:
        LOG.exception('%s Shuffle: could not follow playing song in current view',
                      mode.title())
    return GLib.SOURCE_REMOVE


def _ss_schedule_music_follow(plugin, mode, page_changed=False):
    # Switching notebook pages can precede GTK's final layout.  Let the new
    # page settle before scrolling; ordinary song changes stay on the idle path.
    existing = getattr(plugin, '_ss_music_follow_id', None)
    if existing is not None:
        if not page_changed:
            return
        GLib.source_remove(existing)
        plugin._ss_music_follow_id = None
    if page_changed:
        plugin._ss_music_follow_id = GLib.timeout_add(
            75, _ss_follow_music_view, plugin, mode)
    else:
        plugin._ss_music_follow_id = GLib.idle_add(
            _ss_follow_music_view, plugin, mode)

def _ss_wrap(cls, mode):
    original_activate = cls.do_activate
    original_deactivate = cls.do_deactivate
    original_start = cls.on_start
    original_song = cls.on_song_changed
    original_append = cls._append_next_idle if mode == 'album' else cls._append_idle

    def activate(self):
        original_activate(self)
        self._ss_restored = False
        self._ss_generation = None
        self._ss_deadline = _ss_time.monotonic() + 600
        self._ss_restore_timer = GLib.timeout_add_seconds(3, _ss_restore, self, mode)
        GLib.idle_add(lambda: (_ss_restore(self, mode), GLib.SOURCE_REMOVE)[1])
        # Follow the playing row when the user returns to Music, too.
        self._ss_music_follow_id = None
        self._ss_music_page_handler = self.object.connect(
            'notify::selected-page',
            lambda *_args: _ss_schedule_music_follow(self, mode, page_changed=True))
        self._ss_music_source_handler = self.player.connect(
            'playing-source-changed',
            lambda *_args: _ss_schedule_music_follow(self, mode))
        _ss_schedule_music_follow(self, mode)

    def deactivate(self):
        self._cancel_full_playlist()
        if getattr(self, '_ss_music_follow_id', None) is not None:
            GLib.source_remove(self._ss_music_follow_id)
            self._ss_music_follow_id = None
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
        original_deactivate(self)

    def start(self, *args):
        result = original_start(self, *args)
        _ss_start(self, mode)
        return result

    def song_changed(self, *args):
        result = original_song(self, *args)
        _ss_schedule_music_follow(self, mode)
        _ss_progress(self, mode)
        return result

    def append(self, *args):
        result = original_append(self, *args)
        _ss_progress(self, mode)
        return result

    cls.do_activate = activate
    cls.do_deactivate = deactivate
    cls.on_start = start
    cls.on_song_changed = song_changed
    if mode == 'album':
        cls._append_next_idle = append
    else:
        cls._append_idle = append

_ss_wrap(ContinuousAlbumShuffle, 'album')
