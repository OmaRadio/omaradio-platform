package main

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// Topic mirrors one row of items where category='topic', joined with djs
// for a human-readable scope label rather than a raw dj_scope_id.
type Topic struct {
	ID      int64
	Slug    string
	DJSlug  string // "" = any DJ
	DJName  string // "" = any DJ
	Title   string
	Summary string
	Status  string
}

// DJ mirrors one row of djs -- used to populate the scope picker when
// adding a topic.
type DJ struct {
	ID   int64
	Slug string
	Name string
}

func openDB(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec("PRAGMA foreign_keys = ON;"); err != nil {
		db.Close()
		return nil, fmt.Errorf("enabling foreign keys: %w", err)
	}
	return db, nil
}

func listTopics(db *sql.DB) ([]Topic, error) {
	rows, err := db.Query(`
		SELECT i.id, i.slug, COALESCE(d.slug, ''), COALESCE(d.name, ''), i.title, i.summary, i.status
		FROM items i
		LEFT JOIN djs d ON d.id = i.dj_scope_id
		WHERE i.category = 'topic'
		ORDER BY i.status, i.id
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var topics []Topic
	for rows.Next() {
		var t Topic
		var summary sql.NullString
		if err := rows.Scan(&t.ID, &t.Slug, &t.DJSlug, &t.DJName, &t.Title, &summary, &t.Status); err != nil {
			return nil, err
		}
		t.Summary = summary.String
		topics = append(topics, t)
	}
	return topics, rows.Err()
}

func listDJs(db *sql.DB) ([]DJ, error) {
	rows, err := db.Query(`SELECT id, slug, name FROM djs WHERE active = 1 ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var djs []DJ
	for rows.Next() {
		var d DJ
		if err := rows.Scan(&d.ID, &d.Slug, &d.Name); err != nil {
			return nil, err
		}
		djs = append(djs, d)
	}
	return djs, rows.Err()
}

var slugSanitizePattern = regexp.MustCompile(`[^a-z0-9]+`)

// slugify matches the Python slugify() helper already used throughout
// this pipeline (generate_outros.py etc.) -- lowercase, non-alphanumeric
// runs collapsed to a single hyphen, trimmed.
func slugify(text string) string {
	s := strings.ToLower(text)
	s = slugSanitizePattern.ReplaceAllString(s, "-")
	s = strings.Trim(s, "-")
	if s == "" {
		return "untitled"
	}
	return s
}

func slugExists(db *sql.DB, slug string) (bool, error) {
	var n int
	err := db.QueryRow(`SELECT COUNT(*) FROM items WHERE slug = ?`, slug).Scan(&n)
	return n > 0, err
}

// uniqueSlugFrom appends -2, -3, ... until the slug doesn't collide --
// same reasoning as any "add a new named thing" flow needs, since two
// topics with different DJ scopes could otherwise produce the same
// title-derived slug.
func uniqueSlugFrom(db *sql.DB, title string) (string, error) {
	base := slugify(title)
	slug := base
	for i := 2; ; i++ {
		exists, err := slugExists(db, slug)
		if err != nil {
			return "", err
		}
		if !exists {
			return slug, nil
		}
		slug = fmt.Sprintf("%s-%d", base, i)
	}
}

// addTopic inserts directly into items -- deliberately never touches
// topics.toml or scheduler/db/migrate_topics.py's sync. The two paths
// coexist safely because that sync only ever upserts slugs it finds in
// topics.toml and never touches anything else.
func addTopic(db *sql.DB, slug, title, summary string, djScopeID *int64) error {
	_, err := db.Exec(`
		INSERT INTO items (slug, category, dj_scope_id, title, summary, source, status, touch_policy, fetched_at)
		VALUES (?, 'topic', ?, ?, ?, 'tui', 'active', 'multi', ?)
	`, slug, djScopeID, title, summary, time.Now().UTC().Format(time.RFC3339))
	return err
}

func setTopicStatus(db *sql.DB, id int64, status string) error {
	_, err := db.Exec(`UPDATE items SET status = ? WHERE id = ?`, status, id)
	return err
}

// --- genre_windows ---------------------------------------------------------

// GenreWindow mirrors one row of genre_windows, joined for human-readable
// labels rather than raw station_id/dj_id.
type GenreWindow struct {
	ID        int64
	Station   string
	DJSlug    string // "" = applies regardless of who's on
	DJName    string // "" = applies regardless of who's on
	StartTime string
	EndTime   string
	Genre     string
	Active    bool
}

// defaultStationID returns the one station that exists today. Only one
// station has ever existed in this platform -- if a second one is ever
// added, this (and the genre-window add-form) would need a real picker,
// same shape as the DJ-scope picker already built for Topics.
func defaultStationID(db *sql.DB) (int64, string, error) {
	var id int64
	var slug string
	err := db.QueryRow(`SELECT id, slug FROM stations ORDER BY id LIMIT 1`).Scan(&id, &slug)
	return id, slug, err
}

func listGenreWindows(db *sql.DB) ([]GenreWindow, error) {
	rows, err := db.Query(`
		SELECT g.id, s.slug, COALESCE(d.slug, ''), COALESCE(d.name, ''), g.start_time, g.end_time, g.genre, g.active
		FROM genre_windows g
		JOIN stations s ON s.id = g.station_id
		LEFT JOIN djs d ON d.id = g.dj_id
		ORDER BY g.active DESC, g.start_time
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var windows []GenreWindow
	for rows.Next() {
		var w GenreWindow
		var active int
		if err := rows.Scan(&w.ID, &w.Station, &w.DJSlug, &w.DJName, &w.StartTime, &w.EndTime, &w.Genre, &active); err != nil {
			return nil, err
		}
		w.Active = active != 0
		windows = append(windows, w)
	}
	return windows, rows.Err()
}

// listDistinctGenres reads genres from real track data rather than free
// text, so an add-form can offer a picker instead of risking a typo that
// silently matches zero tracks.
func listDistinctGenres(db *sql.DB) ([]string, error) {
	rows, err := db.Query(`SELECT DISTINCT genre FROM tracks WHERE genre IS NOT NULL AND genre != '' ORDER BY genre`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var genres []string
	for rows.Next() {
		var g string
		if err := rows.Scan(&g); err != nil {
			return nil, err
		}
		genres = append(genres, g)
	}
	return genres, rows.Err()
}

func addGenreWindow(db *sql.DB, stationID int64, djID *int64, startTime, endTime, genre string) error {
	_, err := db.Exec(`
		INSERT INTO genre_windows (station_id, dj_id, start_time, end_time, genre, active)
		VALUES (?, ?, ?, ?, ?, 1)
	`, stationID, djID, startTime, endTime, genre)
	return err
}

func setGenreWindowActive(db *sql.DB, id int64, active bool) error {
	val := 0
	if active {
		val = 1
	}
	_, err := db.Exec(`UPDATE genre_windows SET active = ? WHERE id = ?`, val, id)
	return err
}

// --- tracks / rotation weight ----------------------------------------------

// Track mirrors one media row of kind='track', joined with its tracks
// row for the tags that actually matter to a human curating rotation --
// license/attribution live in the schema but aren't editable here, so
// they're left out.
type Track struct {
	MediaID        int64
	Path           string
	Artist         string
	Title          string
	Genre          string
	RotationWeight float64
	PlayCount      int
}

// label is what a human recognizes a track by -- most library files carry
// real ID3 tags, but a few (confirmed for real against the backfilled
// vault) don't, so this falls back to the filename rather than showing a
// blank "- " or a trailing "Artist - " / leading " - Title" when only one
// of the two tags is missing.
func (t Track) label() string {
	artist := strings.TrimSpace(t.Artist)
	title := strings.TrimSpace(t.Title)
	switch {
	case artist == "" && title == "":
		return filepath.Base(t.Path)
	case artist == "":
		return title
	case title == "":
		return artist
	default:
		return artist + " - " + title
	}
}

func listTracks(db *sql.DB) ([]Track, error) {
	rows, err := db.Query(`
		SELECT m.id, m.path, COALESCE(t.artist, ''), COALESCE(t.title, ''), COALESCE(t.genre, ''), m.rotation_weight,
			(SELECT COUNT(*) FROM plays p WHERE p.media_id = m.id)
		FROM media m
		LEFT JOIN tracks t ON t.media_id = m.id
		WHERE m.kind = 'track'
		ORDER BY t.artist, t.title, m.path
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var tracks []Track
	for rows.Next() {
		var tr Track
		if err := rows.Scan(&tr.MediaID, &tr.Path, &tr.Artist, &tr.Title, &tr.Genre, &tr.RotationWeight, &tr.PlayCount); err != nil {
			return nil, err
		}
		tracks = append(tracks, tr)
	}
	return tracks, rows.Err()
}

func setRotationWeight(db *sql.DB, mediaID int64, weight float64) error {
	_, err := db.Exec(`UPDATE media SET rotation_weight = ? WHERE id = ?`, weight, mediaID)
	return err
}

// updateTrackTags corrects artist/title/genre by hand -- for the tracks
// backfill_media.py found with missing or wrong ID3 tags. An upsert, not a
// plain UPDATE: every 'track'-kind media row has a matching tracks row
// today (confirmed against the real dev db), but tracks.media_id has no
// NOT NULL/foreign-key-enforced guarantee of that pairing, so a bare
// UPDATE could silently affect 0 rows if that ever weren't true.
func updateTrackTags(db *sql.DB, mediaID int64, artist, title, genre string) error {
	_, err := db.Exec(`
		INSERT INTO tracks (media_id, artist, title, genre)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(media_id) DO UPDATE SET artist = excluded.artist, title = excluded.title, genre = excluded.genre
	`, mediaID, artist, title, genre)
	return err
}

// --- plays (read-only air history) -----------------------------------------

// Play mirrors one row of plays, joined for a human-readable label rather
// than raw media_id/station_id. Read-only reporting -- there's no editable
// row here the way Track has, so no separate update function either.
type Play struct {
	ID      int64
	AirTime string
	Station string
	Kind    string
	Path    string
	DJSlug  string
	Artist  string
	Title   string
}

// label reuses Track's own real-tags-else-filename fallback -- it applies
// just as well to a played segment/outro/shoutout as to a track, since
// artist/title are simply empty for anything that isn't a track.
func (p Play) label() string {
	t := Track{Artist: p.Artist, Title: p.Title, Path: p.Path}
	return t.label()
}

func listRecentPlays(db *sql.DB, limit int) ([]Play, error) {
	rows, err := db.Query(`
		SELECT p.id, p.air_time, s.slug, m.kind, m.path, COALESCE(d.slug, ''), COALESCE(t.artist, ''), COALESCE(t.title, '')
		FROM plays p
		JOIN stations s ON s.id = p.station_id
		JOIN media m ON m.id = p.media_id
		LEFT JOIN djs d ON d.id = m.dj_id
		LEFT JOIN tracks t ON t.media_id = m.id
		ORDER BY p.air_time DESC
		LIMIT ?
	`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var plays []Play
	for rows.Next() {
		var p Play
		if err := rows.Scan(&p.ID, &p.AirTime, &p.Station, &p.Kind, &p.Path, &p.DJSlug, &p.Artist, &p.Title); err != nil {
			return nil, err
		}
		plays = append(plays, p)
	}
	return plays, rows.Err()
}
