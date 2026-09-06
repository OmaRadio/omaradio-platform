package main

import (
	"os"
	"testing"
)

func testDBPath(t *testing.T) string {
	path := os.Getenv("TUI_TEST_DB")
	if path == "" {
		t.Skip("set TUI_TEST_DB to a scratch sqlite db (migrate.py + seed_registries.py + migrate_topics.py already applied) to run this test")
	}
	return path
}

// TestTrackLabel is a regression test for a real cosmetic bug: a track
// with only one of artist/title tagged (confirmed for real against the
// backfilled vault -- several synthwave tracks have an artist but no
// title) rendered as a trailing "Nihilore - " or leading " - Song" rather
// than just the tag that actually exists. No db needed -- pure function.
func TestTrackLabel(t *testing.T) {
	cases := []struct {
		name   string
		track  Track
		expect string
	}{
		{"both tagged", Track{Artist: "Nihilore", Title: "The Woods", Path: "x/y.mp3"}, "Nihilore - The Woods"},
		{"artist only", Track{Artist: "Nihilore", Path: "library/music/synth/Climbers.mp3"}, "Nihilore"},
		{"title only", Track{Title: "Artifice", Path: "library/music/synth/Artifice.mp3"}, "Artifice"},
		{"neither tagged", Track{Path: "library/music/synth/Motion+Blur.mp3"}, "Motion+Blur.mp3"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.track.label(); got != c.expect {
				t.Errorf("label() = %q, want %q", got, c.expect)
			}
		})
	}
}

func TestListTopicsAndDJs(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	topics, err := listTopics(db)
	if err != nil {
		t.Fatalf("listTopics: %v", err)
	}
	// >= rather than == : this scratch db is a copy of the real dev db, and
	// topics added for real through the TUI (source='tui') accumulate on
	// top of the 14 that came from the original topics.toml migration.
	if len(topics) < 14 {
		t.Errorf("expected at least 14 topics from the real topics.toml migration, got %d", len(topics))
	}

	djs, err := listDJs(db)
	if err != nil {
		t.Fatalf("listDJs: %v", err)
	}
	if len(djs) != 4 {
		t.Errorf("expected 4 djs, got %d", len(djs))
	}
}

func TestAddTopicAndToggleStatus(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	before, _ := listTopics(db)

	slug, err := uniqueSlugFrom(db, "A Brand New Test Topic!!")
	if err != nil {
		t.Fatalf("uniqueSlugFrom: %v", err)
	}
	if slug != "a-brand-new-test-topic" {
		t.Errorf("expected slugified title, got %q", slug)
	}

	if err := addTopic(db, slug, "A Brand New Test Topic!!", "seed text", nil); err != nil {
		t.Fatalf("addTopic: %v", err)
	}

	// Adding again with the same title must not collide -- uniqueSlugFrom
	// should append -2.
	slug2, err := uniqueSlugFrom(db, "A Brand New Test Topic!!")
	if err != nil {
		t.Fatalf("uniqueSlugFrom (second): %v", err)
	}
	if slug2 != "a-brand-new-test-topic-2" {
		t.Errorf("expected collision-avoiding suffix, got %q", slug2)
	}

	after, _ := listTopics(db)
	if len(after) != len(before)+1 {
		t.Errorf("expected topic count to grow by 1, went from %d to %d", len(before), len(after))
	}

	var added *Topic
	for i := range after {
		if after[i].Slug == slug {
			added = &after[i]
		}
	}
	if added == nil {
		t.Fatalf("newly added topic %q not found in listTopics()", slug)
	}
	if added.Status != "active" {
		t.Errorf("expected new topic to be active, got %q", added.Status)
	}
	if added.DJSlug != "" {
		t.Errorf("expected nil dj scope (any dj), got %q", added.DJSlug)
	}

	if err := setTopicStatus(db, added.ID, "archived"); err != nil {
		t.Fatalf("setTopicStatus: %v", err)
	}
	after2, _ := listTopics(db)
	for i := range after2 {
		if after2[i].ID == added.ID && after2[i].Status != "archived" {
			t.Errorf("expected status archived after toggle, got %q", after2[i].Status)
		}
	}

	// Clean up so re-running this test suite against the same scratch db stays idempotent.
	if _, err := db.Exec("DELETE FROM items WHERE slug IN (?, ?)", slug, slug2); err != nil {
		t.Fatalf("cleanup: %v", err)
	}
}

func TestGenreWindowsCRUD(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	stationID, station, err := defaultStationID(db)
	if err != nil {
		t.Fatalf("defaultStationID: %v", err)
	}
	if station == "" {
		t.Fatalf("expected a non-empty station slug")
	}

	genres, err := listDistinctGenres(db)
	if err != nil {
		t.Fatalf("listDistinctGenres: %v", err)
	}
	if len(genres) == 0 {
		t.Fatalf("expected at least one genre from real backfilled track data")
	}

	before, err := listGenreWindows(db)
	if err != nil {
		t.Fatalf("listGenreWindows: %v", err)
	}

	if err := addGenreWindow(db, stationID, nil, "22:00", "02:00", genres[0]); err != nil {
		t.Fatalf("addGenreWindow: %v", err)
	}

	after, err := listGenreWindows(db)
	if err != nil {
		t.Fatalf("listGenreWindows (after add): %v", err)
	}
	if len(after) != len(before)+1 {
		t.Fatalf("expected genre window count to grow by 1, went from %d to %d", len(before), len(after))
	}

	var added *GenreWindow
	for i := range after {
		if after[i].Genre == genres[0] && after[i].StartTime == "22:00" && after[i].EndTime == "02:00" {
			added = &after[i]
		}
	}
	if added == nil {
		t.Fatalf("newly added genre window not found in listGenreWindows()")
	}
	if !added.Active {
		t.Errorf("expected new genre window to be active by default")
	}
	if added.DJSlug != "" {
		t.Errorf("expected nil dj scope (any dj), got %q", added.DJSlug)
	}
	if added.Station != station {
		t.Errorf("expected station %q, got %q", station, added.Station)
	}

	if err := setGenreWindowActive(db, added.ID, false); err != nil {
		t.Fatalf("setGenreWindowActive: %v", err)
	}
	after2, err := listGenreWindows(db)
	if err != nil {
		t.Fatalf("listGenreWindows (after toggle): %v", err)
	}
	for i := range after2 {
		if after2[i].ID == added.ID && after2[i].Active {
			t.Errorf("expected genre window to be inactive after toggle")
		}
	}

	// Clean up so re-running this test suite against the same scratch db stays idempotent.
	if _, err := db.Exec("DELETE FROM genre_windows WHERE id = ?", added.ID); err != nil {
		t.Fatalf("cleanup: %v", err)
	}
}

func TestListTracksAndSetRotationWeight(t *testing.T) {
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
	if target.label() == "" {
		t.Errorf("expected a non-empty label (artist/title or filename fallback)")
	}

	// This mutates a real, pre-existing row rather than one this test
	// created -- unlike addTopic/addGenreWindow's cleanup-by-delete, here
	// the only safe cleanup is restoring the original weight.
	original := target.RotationWeight
	defer func() {
		if err := setRotationWeight(db, target.MediaID, original); err != nil {
			t.Fatalf("cleanup: failed to restore original rotation_weight: %v", err)
		}
	}()

	if err := setRotationWeight(db, target.MediaID, 2.5); err != nil {
		t.Fatalf("setRotationWeight: %v", err)
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
		t.Fatalf("track %d not found after setRotationWeight", target.MediaID)
	}
	if found.RotationWeight != 2.5 {
		t.Errorf("expected rotation_weight 2.5, got %v", found.RotationWeight)
	}
}

func TestListRecentPlays(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	plays, err := listRecentPlays(db, 5)
	if err != nil {
		t.Fatalf("listRecentPlays: %v", err)
	}
	if len(plays) == 0 {
		t.Fatalf("expected at least one recorded play in the real dev db")
	}
	if len(plays) > 5 {
		t.Errorf("expected the limit to cap results at 5, got %d", len(plays))
	}

	for i := 1; i < len(plays); i++ {
		if plays[i].AirTime > plays[i-1].AirTime {
			t.Errorf("expected descending air_time order, got %q before %q", plays[i-1].AirTime, plays[i].AirTime)
		}
	}

	if plays[0].label() == "" {
		t.Errorf("expected a non-empty label (artist/title or filename fallback)")
	}
	if plays[0].Station == "" {
		t.Errorf("expected a non-empty station slug")
	}
}

// TestListTracksPlayCount cross-checks listTracks' per-track play_count
// subquery against a hand-written aggregate over the same table.
func TestListTracksPlayCount(t *testing.T) {
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

	sawAPlay := false
	for _, tr := range tracks {
		var want int
		if err := db.QueryRow(`SELECT COUNT(*) FROM plays WHERE media_id = ?`, tr.MediaID).Scan(&want); err != nil {
			t.Fatalf("aggregate query for media %d: %v", tr.MediaID, err)
		}
		if tr.PlayCount != want {
			t.Errorf("track %d (%s): PlayCount = %d, want %d", tr.MediaID, tr.label(), tr.PlayCount, want)
		}
		if want > 0 {
			sawAPlay = true
		}
	}
	if !sawAPlay {
		t.Skip("no track in this db has ever aired -- PlayCount cross-check ran, but only against all-zero values")
	}
}
