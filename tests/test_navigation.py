"""Sidebar, top bar, tool drawer and log button — the redesigned shell."""


from PyQt6.QtWidgets import QListWidgetItem


def test_starts_on_songs(window):
    lib = window.page_library
    assert window.content_stack.currentWidget() is lib
    assert lib.current_section() == "songs"
    assert window.sidebar._buttons["songs"].isChecked()
    assert window.page_title.text() == "Songs"
    assert "3 songs" in window.page_subtitle.text()


def test_sidebar_counts(window):
    buttons = window.sidebar._buttons
    assert buttons["songs"].count_label.text() == "3"
    assert buttons["audiobooks"].count_label.text() == "5"


def test_attention_chip_opens_section(window):
    window.sidebar._buttons["unsorted"].click()
    lib = window.page_library
    assert lib.current_section() == "unsorted"
    assert window.page_title.text() == "Unsorted"
    assert not lib.search_row.isVisible()  # search is for Songs/Audiobooks only


def test_health_go_to_duplicates_keeps_sidebar_in_sync(window):
    window._on_navigate("health", None)
    window.page_library.health_goto_dupes_btn.click()
    assert window.page_library.current_section() == "duplicates"
    assert window.sidebar._buttons["duplicates"].isChecked()
    assert window.page_title.text() == "Duplicates"


def test_tool_button_opens_drawer_over_current_page(window):
    drawer = window.tool_drawer
    window._on_navigate("audiobooks", None)
    window.tool_buttons["merge"].click()
    assert drawer.isVisible()
    assert drawer.stack.currentWidget() is window.page_merge
    assert window.content_stack.currentWidget() is window.page_library  # page underneath unchanged
    assert window.sidebar._buttons["audiobooks"].isChecked()
    assert window.tool_buttons["merge"].isChecked()
    assert drawer.geometry().right() == window.body_widget.width() - 1
    assert drawer.MIN_WIDTH <= drawer.width() <= drawer.MAX_WIDTH


def test_tool_button_toggles_and_switches(window):
    drawer = window.tool_drawer
    window.tool_buttons["merge"].click()
    window.tool_buttons["youtube"].click()
    assert drawer.isVisible() and drawer.stack.currentWidget() is window.page_youtube
    assert window.tool_buttons["youtube"].isChecked() and not window.tool_buttons["merge"].isChecked()
    window.tool_buttons["youtube"].click()
    assert not drawer.isVisible()
    assert not any(b.isChecked() for b in window.tool_buttons.values())


def test_tags_button_opens_batch_apply_fresh(window):
    window.batch_tag_list.addItem("leftover.mp3")
    window.tool_buttons["tags"].click()
    assert window.tool_drawer.stack.currentWidget() is window.page_tags
    assert window.tag_mode_tabs.currentIndex() == 1
    assert window.batch_tag_list.count() == 0


def test_fix_tags_opens_single_file_tags(window, library):
    first = window.page_library.songs_list.item(0).data(256)
    window._fix_tags_for_path(str(library.root / first["library_path"]), first["id"])
    assert window.tool_drawer.isVisible()
    assert window.tool_drawer.stack.currentWidget() is window.page_tags
    assert window.tag_mode_tabs.currentIndex() == 0
    assert window._pending_library_refile == first["id"]


def test_youtube_button_shows_queue_count(window):
    button = window.tool_buttons["youtube"]
    window.youtube_queue_list.addItem(QListWidgetItem("a"))
    window.youtube_queue_list.addItem(QListWidgetItem("b"))
    assert button.text().endswith("2")
    window.youtube_queue_list.clear()
    assert button.text() == "YouTube"


def test_escape_closes_drawer(window, qtbot):
    from PyQt6.QtCore import Qt
    window.tool_buttons["split"].click()
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert not window.tool_drawer.isVisible()


def test_column_sort(window):
    lib = window.page_library
    lib.songs_header.sortRequested.emit("artist")
    assert lib._sort_field == "Artist" and lib._sort_ascending
    assert [lib.songs_list.item(i).data(256)["artist"] for i in range(3)] == ["Band", "Band", "Solo"]
    lib.songs_header.sortRequested.emit("artist")
    assert not lib._sort_ascending
    lib.songs_header.sortRequested.emit("added")
    assert lib._sort_field == "Date Added" and not lib._sort_ascending  # newest first


def test_search_filters(window):
    lib = window.page_library
    lib.search_edit.setText("solo")
    lib.refresh()
    assert lib.songs_list.count() == 1


def test_series_page_and_back(window):
    window._on_navigate("audiobook_series", "Saga")
    assert window.content_stack.currentWidget() is window.page_audiobook_series
    assert window.sidebar._buttons["audiobooks"].isChecked()
    assert window.page_audiobook_series.track_list.count() == 2
    window._return_to_audiobooks()
    assert window.page_library.current_section() == "audiobooks"
    assert window.page_library.audiobooks_stack.currentIndex() == 0  # Series view


def test_audiobooks_series_all_switch(window):
    lib = window.page_library
    window._on_navigate("audiobooks", None)
    lib.audiobooks_view_toggle.setCurrentText("All")
    assert lib.audiobooks_stack.currentIndex() == 1
    assert lib.audiobooks_list.count() == 5


def test_playlist_page(window, library):
    deck = window.sidebar.playlist_deck
    deck._on_playlist_clicked(deck.playlist_list.item(0))
    assert window.content_stack.currentWidget() is window.page_playlist
    assert not any(b.isChecked() for b in window.sidebar._buttons.values())
    rows = [window.page_playlist.track_list.item(i).data(256) for i in range(2)]
    assert [r["pos"] for r in rows] == ["1", "2"]
    assert [r["title"] for r in rows] == ["Three", "One"]


def test_log_button(window):
    log = window.console_strip
    log.set_task_text("Analyzing loudness (5/10)")
    assert "Analyzing" in log.text()
    log.set_task_text("")
    assert log.text() == "Log"
    window._toggle_console()
    overlay = window.console_overlay.geometry()
    assert window.console_overlay.isVisible() and log.isChecked()
    assert overlay.left() >= window.sidebar.width()
    window._toggle_console()
    assert not window.console_overlay.isVisible()
