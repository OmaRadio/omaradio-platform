package main

import (
	"testing"

	"github.com/charmbracelet/bubbles/list"
	tea "github.com/charmbracelet/bubbletea"
)

// TestMusicScreenAdjustWeightWhileFiltered is a regression test for a real
// bug: adjusting rotation weight while the list was filtered emptied the
// visible list and it never came back. Root cause: list.Model.SetItems()
// returns a tea.Cmd that must be run by the bubbletea runtime (and its
// result msg fed back into Update) to repopulate the filtered view --
// reload() was discarding that command. Confirmed for real via manual
// testing (+/- on a filtered track worked but left the list empty).
func TestMusicScreenAdjustWeightWhileFiltered(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	tracks, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks: %v", err)
	}
	// Pick a track with real tags rather than tracks[0] blindly -- a
	// handful of backfilled tracks have blank ID3 artist/title (confirmed
	// for real against the vault), and FilterValue() only includes
	// artist/title/genre, not the filename label() falls back to, so a
	// blank-tagged track's own label() wouldn't even match its own filter.
	var target *Track
	for i := range tracks {
		if tracks[i].Artist != "" && tracks[i].Title != "" {
			target = &tracks[i]
			break
		}
	}
	if target == nil {
		t.Fatalf("expected at least one backfilled track with real artist/title tags")
	}

	m := newMusicModel(db)
	m.list.SetFilterText(target.Artist)

	if m.list.FilterState() == list.Unfiltered {
		t.Fatalf("expected SetFilterText to leave the list in a filtered state")
	}
	if len(m.list.VisibleItems()) == 0 {
		t.Fatalf("expected at least one visible item before adjusting weight")
	}

	// Whichever item the filter actually selected first -- not
	// necessarily `target`, if more than one track shares that artist --
	// is the one "+" is about to mutate, so track its original weight for
	// cleanup from here, not from the pre-filter listTracks() call.
	selected, ok := m.list.SelectedItem().(musicItem)
	if !ok {
		t.Fatalf("expected a selected musicItem after filtering")
	}
	original := selected.t.RotationWeight
	defer func() {
		if err := setRotationWeight(db, selected.t.MediaID, original); err != nil {
			t.Fatalf("cleanup: failed to restore original rotation_weight: %v", err)
		}
	}()

	updated, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("+")})
	m = updated

	if cmd == nil {
		t.Fatalf("expected a non-nil re-filter command after editing while filtered -- without it the list stays empty forever")
	}

	// This is the step the bug skipped: run the command the bubbletea
	// runtime would normally run, and feed its result back into Update.
	msg := cmd()
	updated, _ = m.Update(msg)
	m = updated

	if len(m.list.VisibleItems()) == 0 {
		t.Fatalf("bug reproduced: filtered list is empty after the reload command resolves")
	}
}

// TestMusicScreenEnterClearsAppliedFilter confirms pressing enter while
// browsing an already-applied filter (not actively typing it) drops back
// to the full, unfiltered list -- there's nothing else for enter to do on
// this screen (no per-row "select" action), so leaving it as a no-op felt
// like a dead end.
func TestMusicScreenEnterClearsAppliedFilter(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	tracks, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks: %v", err)
	}
	if len(tracks) < 2 {
		t.Skip("need at least 2 tracks to distinguish filtered from unfiltered counts")
	}

	m := newMusicModel(db)
	fullCount := len(m.list.VisibleItems())

	m.list.SetFilterText(tracks[0].Genre)
	if m.list.FilterState() != list.FilterApplied {
		t.Fatalf("expected FilterApplied after SetFilterText, got %v", m.list.FilterState())
	}
	filteredCount := len(m.list.VisibleItems())
	if filteredCount == 0 {
		t.Fatalf("expected at least one match filtering by genre %q", tracks[0].Genre)
	}

	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = updated

	if m.list.FilterState() != list.Unfiltered {
		t.Errorf("expected enter to clear the applied filter, got state %v", m.list.FilterState())
	}
	if got := len(m.list.VisibleItems()); got != fullCount {
		t.Errorf("expected enter to restore all %d tracks, got %d", fullCount, got)
	}
}

func TestUpdateTrackTags(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	tracks, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks: %v", err)
	}
	if len(tracks) == 0 {
		t.Fatalf("expected at least one backfilled track")
	}
	target := tracks[0]
	defer func() {
		if err := updateTrackTags(db, target.MediaID, target.Artist, target.Title, target.Genre); err != nil {
			t.Fatalf("cleanup: failed to restore original tags: %v", err)
		}
	}()

	if err := updateTrackTags(db, target.MediaID, "Test Artist", "Test Title", target.Genre); err != nil {
		t.Fatalf("updateTrackTags: %v", err)
	}

	after, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks (after): %v", err)
	}
	var found *Track
	for i := range after {
		if after[i].MediaID == target.MediaID {
			found = &after[i]
		}
	}
	if found == nil {
		t.Fatalf("track %d not found after updateTrackTags", target.MediaID)
	}
	if found.Artist != "Test Artist" || found.Title != "Test Title" {
		t.Errorf("expected artist/title to be updated, got %q / %q", found.Artist, found.Title)
	}
}

// TestMusicScreenEditFormPrefill confirms "e" opens the edit form
// pre-populated with the selected track's own current tags, not blank
// fields -- this is a correction tool, not a from-scratch add-form.
func TestMusicScreenEditFormPrefill(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newMusicModel(db)
	selected, ok := m.list.SelectedItem().(musicItem)
	if !ok {
		t.Fatalf("expected a selected musicItem")
	}

	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("e")})
	m = updated

	if !m.editing {
		t.Fatalf("expected 'e' to open the edit form")
	}
	if m.form.artistInput.Value() != selected.t.Artist {
		t.Errorf("expected artist field prefilled with %q, got %q", selected.t.Artist, m.form.artistInput.Value())
	}
	if m.form.titleInput.Value() != selected.t.Title {
		t.Errorf("expected title field prefilled with %q, got %q", selected.t.Title, m.form.titleInput.Value())
	}
	if m.form.genreChoices[m.form.genreIdx] != selected.t.Genre {
		t.Errorf("expected genre picker prefilled with %q, got %q", selected.t.Genre, m.form.genreChoices[m.form.genreIdx])
	}
}

// TestMusicScreenEditCancelDiscardsChanges confirms esc leaves the
// database untouched, even after the in-memory form was already edited.
func TestMusicScreenEditCancelDiscardsChanges(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	tracks, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks: %v", err)
	}
	if len(tracks) == 0 {
		t.Fatalf("expected at least one backfilled track")
	}
	originalArtist := tracks[0].Artist

	m := newMusicModel(db)
	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("e")})
	m = updated
	m.form.artistInput.SetValue("Should Never Be Saved")

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyEsc})
	m = updated

	if m.editing {
		t.Errorf("expected esc to close the edit form")
	}

	after, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks (after): %v", err)
	}
	if after[0].Artist != originalArtist {
		t.Errorf("esc should have discarded the edit, but artist changed to %q", after[0].Artist)
	}
}

// TestMusicScreenEditSubmitUpdatesTrack drives the full form flow end to
// end: open with "e", type new artist/title, cycle genre, save with
// enter, then confirm the change actually reached the database (not just
// the in-memory form) and the list reloaded to reflect it.
func TestMusicScreenEditSubmitUpdatesTrack(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	tracks, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks: %v", err)
	}
	if len(tracks) == 0 {
		t.Fatalf("expected at least one backfilled track")
	}
	target := tracks[0]
	defer func() {
		if err := updateTrackTags(db, target.MediaID, target.Artist, target.Title, target.Genre); err != nil {
			t.Fatalf("cleanup: failed to restore original tags: %v", err)
		}
	}()

	m := newMusicModel(db)
	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("e")})
	m = updated
	if !m.editing {
		t.Fatalf("expected 'e' to open the edit form")
	}

	m.form.artistInput.SetValue("Edited Artist")
	m.form.titleInput.SetValue("Edited Title")

	// Cycle the genre picker once, right then back left, to exercise
	// left/right without depending on there being more than one genre --
	// this leaves genreIdx unchanged if only one choice exists.
	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated
	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated
	if m.form.focus != editFieldGenre {
		t.Fatalf("expected two tabs to land on the genre field, got focus %v", m.form.focus)
	}
	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyRight})
	m = updated
	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyLeft})
	m = updated

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = updated

	if m.editing {
		t.Errorf("expected enter to close the edit form after saving")
	}

	after, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks (after): %v", err)
	}
	var found *Track
	for i := range after {
		if after[i].MediaID == target.MediaID {
			found = &after[i]
		}
	}
	if found == nil {
		t.Fatalf("track %d not found after submitting the edit form", target.MediaID)
	}
	if found.Artist != "Edited Artist" || found.Title != "Edited Title" {
		t.Errorf("expected saved artist/title, got %q / %q", found.Artist, found.Title)
	}
	if found.Genre != target.Genre {
		t.Errorf("expected genre unchanged after a right-then-left cycle, got %q, want %q", found.Genre, target.Genre)
	}
}

// TestMusicScreenEditStatusMessageNoTrailingDash is a regression test for
// a real bug found via a full scripted walkthrough: submitEdit() built its
// "updated ..." status message by hand-concatenating "artist + ' - ' +
// title" instead of reusing Track.label(), so it still showed a trailing
// "Artist - " when only one of the two tags was set -- the exact bug
// label() itself was already fixed for, just duplicated in a second spot.
func TestMusicScreenEditStatusMessageNoTrailingDash(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	tracks, err := listTracks(db)
	if err != nil {
		t.Fatalf("listTracks: %v", err)
	}
	if len(tracks) == 0 {
		t.Fatalf("expected at least one backfilled track")
	}
	target := tracks[0]
	defer func() {
		if err := updateTrackTags(db, target.MediaID, target.Artist, target.Title, target.Genre); err != nil {
			t.Fatalf("cleanup: failed to restore original tags: %v", err)
		}
	}()

	m := newMusicModel(db)
	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("e")})
	m = updated

	m.form.artistInput.SetValue("Solo Artist")
	m.form.titleInput.SetValue("")

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = updated

	if m.status != "updated Solo Artist" {
		t.Errorf("status = %q, want %q (no trailing dash)", m.status, "updated Solo Artist")
	}
}
