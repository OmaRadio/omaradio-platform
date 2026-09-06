package main

import (
	"database/sql"
	"fmt"
	"math"
	"strings"

	"github.com/charmbracelet/bubbles/list"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
)

const (
	rotationWeightStep = 0.25
	rotationWeightMin  = 0.1
	rotationWeightMax  = 5.0
	rotationWeightDef  = 1.0
)

// --- list.Item adapter -------------------------------------------------

type musicItem struct{ t Track }

func (i musicItem) Title() string {
	return fmt.Sprintf("%5.2f  %4dx  %s", i.t.RotationWeight, i.t.PlayCount, i.t.label())
}

func (i musicItem) Description() string { return i.t.Genre }
func (i musicItem) FilterValue() string { return i.t.Artist + " " + i.t.Title + " " + i.t.Genre }

// --- edit-form state ---------------------------------------------------

type trackEditField int

const (
	editFieldArtist trackEditField = iota
	editFieldTitle
	editFieldGenre
	editFieldCount
)

type trackEditForm struct {
	mediaID      int64
	originalPath string // for the status message's filename fallback if both tags end up blank
	artistInput  textinput.Model
	titleInput   textinput.Model
	genreChoices []string
	genreIdx     int
	focus        trackEditField
}

// newTrackEditForm prefills from the track being corrected. Genre is a
// picker over already-observed genre values, same "avoid a typo that
// silently matches zero tracks" reasoning as Genre windows' own add-form
// -- but unlike that screen, the value being edited is guaranteed to
// already be in that list (it came from the same tracks table), except
// the corner case where this track is the *only* one with its genre, so
// that value is prepended defensively rather than assumed present.
func newTrackEditForm(t Track, genres []string) trackEditForm {
	artist := textinput.New()
	artist.SetValue(t.Artist)
	artist.Focus()
	title := textinput.New()
	title.SetValue(t.Title)

	choices := genres
	idx := -1
	for i, g := range choices {
		if g == t.Genre {
			idx = i
			break
		}
	}
	if idx == -1 {
		choices = append([]string{t.Genre}, choices...)
		idx = 0
	}

	return trackEditForm{
		mediaID:      t.MediaID,
		originalPath: t.Path,
		artistInput:  artist,
		titleInput:   title,
		genreChoices: choices,
		genreIdx:     idx,
		focus:        editFieldArtist,
	}
}

// --- main model --------------------------------------------------------

// musicModel has no add-form -- tracks arrive from backfill_media.py
// scanning the vault, never by hand here. Rotation weight and artist/
// title/genre tag corrections are the only things a human curates
// directly, so this screen edits an existing row in place ("e") rather
// than mirroring Topics/Genre windows' list+add-form shape.
type musicModel struct {
	db      *sql.DB
	list    list.Model
	editing bool
	form    trackEditForm
	genres  []string
	status  string
}

func newMusicModel(db *sql.DB) musicModel {
	tracks, err := listTracks(db)
	status := ""
	if err != nil {
		status = fmt.Sprintf("error loading tracks: %v", err)
	}
	items := make([]list.Item, len(tracks))
	for i, t := range tracks {
		items[i] = musicItem{t}
	}
	l := list.New(items, list.NewDefaultDelegate(), 0, 0)
	l.Title = "Music (weight, play count)"
	l.SetShowStatusBar(false)
	genres, _ := listDistinctGenres(db)
	return musicModel{db: db, list: l, genres: genres, status: status}
}

func (m musicModel) Init() tea.Cmd { return nil }

// reload returns the tea.Cmd from list.SetItems and callers must return it
// onward through Update -- while a filter is active, SetItems clears the
// filtered view to nil and relies on that command (run by the bubbletea
// runtime, feeding its result back into a later Update call) to
// repopulate it. Dropping the command left the list empty after any edit
// made while filtered -- confirmed for real via manual testing.
func (m *musicModel) reload() tea.Cmd {
	tracks, err := listTracks(m.db)
	if err != nil {
		m.status = fmt.Sprintf("error reloading: %v", err)
		return nil
	}
	items := make([]list.Item, len(tracks))
	for i, t := range tracks {
		items[i] = musicItem{t}
	}
	return m.list.SetItems(items)
}

func (m musicModel) Update(msg tea.Msg) (musicModel, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.list.SetSize(msg.Width, msg.Height-nonListChromeHeight)
		return m, nil

	case tea.KeyMsg:
		if m.editing {
			return m.updateForm(msg)
		}

		if m.list.FilterState() == list.Filtering {
			var cmd tea.Cmd
			m.list, cmd = m.list.Update(msg)
			return m, cmd
		}

		switch msg.String() {
		case "+", "=":
			return m.adjustWeight(rotationWeightStep)
		case "-":
			return m.adjustWeight(-rotationWeightStep)
		case "0":
			return m.setWeight(rotationWeightDef)
		case "e":
			if item, ok := m.list.SelectedItem().(musicItem); ok {
				m.editing = true
				m.form = newTrackEditForm(item.t, m.genres)
				m.status = ""
			}
			return m, nil
		case "enter":
			// Enter has no meaning of its own once browsing a filtered
			// view (there's nothing to "select into" here) -- treat it as
			// "done narrowing, show me everything again" instead of a
			// no-op.
			if m.list.FilterState() == list.FilterApplied {
				m.list.ResetFilter()
				return m, nil
			}
		}
	}

	var cmd tea.Cmd
	m.list, cmd = m.list.Update(msg)
	return m, cmd
}

func (m musicModel) updateForm(msg tea.KeyMsg) (musicModel, tea.Cmd) {
	switch msg.String() {
	case "esc":
		m.editing = false
		m.status = "cancelled"
		return m, nil

	case "tab", "shift+tab", "down", "up":
		if msg.String() == "shift+tab" || msg.String() == "up" {
			m.form.focus = (m.form.focus - 1 + editFieldCount) % editFieldCount
		} else {
			m.form.focus = (m.form.focus + 1) % editFieldCount
		}
		m.form.artistInput.Blur()
		m.form.titleInput.Blur()
		switch m.form.focus {
		case editFieldArtist:
			m.form.artistInput.Focus()
		case editFieldTitle:
			m.form.titleInput.Focus()
		}
		return m, nil

	case "left", "right":
		if m.form.focus == editFieldGenre && len(m.form.genreChoices) > 0 {
			n := len(m.form.genreChoices)
			if msg.String() == "right" {
				m.form.genreIdx = (m.form.genreIdx + 1) % n
			} else {
				m.form.genreIdx = (m.form.genreIdx - 1 + n) % n
			}
			return m, nil
		}

	case "enter":
		return m.submitEdit()
	}

	var cmd tea.Cmd
	switch m.form.focus {
	case editFieldArtist:
		m.form.artistInput, cmd = m.form.artistInput.Update(msg)
	case editFieldTitle:
		m.form.titleInput, cmd = m.form.titleInput.Update(msg)
	}
	return m, cmd
}

func (m musicModel) submitEdit() (musicModel, tea.Cmd) {
	artist := strings.TrimSpace(m.form.artistInput.Value())
	title := strings.TrimSpace(m.form.titleInput.Value())
	genre := ""
	if len(m.form.genreChoices) > 0 {
		genre = m.form.genreChoices[m.form.genreIdx]
	}
	if err := updateTrackTags(m.db, m.form.mediaID, artist, title, genre); err != nil {
		m.status = fmt.Sprintf("error updating track: %v", err)
		return m, nil
	}
	// Reuse Track.label() rather than reconstructing "Artist - Title" by
	// hand -- an earlier ad hoc version here had the exact trailing-"- "
	// bug that label() was fixed for, since it never went through that fix.
	m.status = fmt.Sprintf("updated %s", Track{Artist: artist, Title: title, Path: m.form.originalPath}.label())
	m.editing = false
	cmd := m.reload()
	return m, cmd
}

func (m musicModel) adjustWeight(delta float64) (musicModel, tea.Cmd) {
	item, ok := m.list.SelectedItem().(musicItem)
	if !ok {
		return m, nil
	}
	next := item.t.RotationWeight + delta
	if next < rotationWeightMin {
		next = rotationWeightMin
	}
	if next > rotationWeightMax {
		next = rotationWeightMax
	}
	return m.setWeight(next)
}

func (m musicModel) setWeight(weight float64) (musicModel, tea.Cmd) {
	item, ok := m.list.SelectedItem().(musicItem)
	if !ok {
		return m, nil
	}
	weight = math.Round(weight*100) / 100
	if err := setRotationWeight(m.db, item.t.MediaID, weight); err != nil {
		m.status = fmt.Sprintf("error updating weight: %v", err)
		return m, nil
	}
	m.status = fmt.Sprintf("%s -> %.2f", item.t.label(), weight)
	cmd := m.reload()
	return m, cmd
}

func (m musicModel) View() string {
	if m.editing {
		return m.viewForm()
	}
	return m.viewList()
}

func (m musicModel) viewList() string {
	var b strings.Builder
	b.WriteString(m.list.View())
	if m.status != "" {
		b.WriteString(styleHelp.Render(m.status))
		b.WriteString("\n")
	}
	hints := "j/k move  /  filter  +/-  weight  0  reset weight  e  edit tags  q  quit"
	if m.list.FilterState() == list.FilterApplied {
		hints = "j/k move  enter/esc  clear filter  +/-  weight  0  reset weight  e  edit tags  q  quit"
	}
	b.WriteString(styleHelp.Render(hints))
	return b.String()
}

func (m musicModel) viewForm() string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("Edit track") + "\n\n")

	label := "Artist"
	if m.form.focus == editFieldArtist {
		label = styleFieldOn.Render("Artist")
	}
	b.WriteString(styleField.Render(label + "\n" + m.form.artistInput.View()) + "\n")

	label = "Title"
	if m.form.focus == editFieldTitle {
		label = styleFieldOn.Render("Title")
	}
	b.WriteString(styleField.Render(label + "\n" + m.form.titleInput.View()) + "\n")

	label = "Genre"
	genre := "(none)"
	if len(m.form.genreChoices) > 0 {
		genre = m.form.genreChoices[m.form.genreIdx]
	}
	if m.form.focus == editFieldGenre {
		label = styleFieldOn.Render("Genre")
		genre = "< " + genre + " >"
	}
	b.WriteString(styleField.Render(label + "\n" + genre) + "\n")

	b.WriteString(styleHelp.Render("tab/shift+tab move  <-/-> change genre  enter save  esc cancel"))
	return b.String()
}
