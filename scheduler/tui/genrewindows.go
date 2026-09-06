package main

import (
	"database/sql"
	"fmt"
	"regexp"
	"strings"

	"github.com/charmbracelet/bubbles/list"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
)

// --- list.Item adapter -------------------------------------------------

type genreWindowItem struct{ w GenreWindow }

func (i genreWindowItem) Title() string {
	scope := "any dj"
	if i.w.DJName != "" {
		scope = i.w.DJName
	}
	marker := " "
	if !i.w.Active {
		marker = "x"
	}
	return fmt.Sprintf("[%s] %s-%s  %-14s (%s)", marker, i.w.StartTime, i.w.EndTime, i.w.Genre, scope)
}

func (i genreWindowItem) Description() string { return "station: " + i.w.Station }
func (i genreWindowItem) FilterValue() string { return i.w.Genre + " " + i.w.DJSlug }

// --- add-form state --------------------------------------------------------

type gwFormField int

const (
	gwFieldScope gwFormField = iota
	gwFieldStart
	gwFieldEnd
	gwFieldGenre
	gwFieldCount
)

var hhmmPattern = regexp.MustCompile(`^([01]\d|2[0-3]):[0-5]\d$`)

type gwAddForm struct {
	startInput textinput.Model
	endInput   textinput.Model
	focus      gwFormField
	scopeIdx   int // 0 = "any dj", 1..len(djs) = djs[idx-1]
	genreIdx   int
}

func newGWAddForm() gwAddForm {
	start := textinput.New()
	start.Placeholder = "02:00"
	start.Focus()
	end := textinput.New()
	end.Placeholder = "04:00"
	return gwAddForm{startInput: start, endInput: end, focus: gwFieldScope}
}

// --- main model --------------------------------------------------------

type genreWindowsModel struct {
	db        *sql.DB
	list      list.Model
	mode      mode
	form      gwAddForm
	djs       []DJ
	genres    []string
	stationID int64
	station   string
	status    string
	width     int
}

func newGenreWindowsModel(db *sql.DB) genreWindowsModel {
	windows, err := listGenreWindows(db)
	status := ""
	if err != nil {
		status = fmt.Sprintf("error loading genre windows: %v", err)
	}
	items := make([]list.Item, len(windows))
	for i, w := range windows {
		items[i] = genreWindowItem{w}
	}
	l := list.New(items, list.NewDefaultDelegate(), 0, 0)
	l.Title = "Genre windows"
	l.SetShowStatusBar(false)

	djs, _ := listDJs(db)
	genres, _ := listDistinctGenres(db)
	stationID, station, sErr := defaultStationID(db)
	if sErr != nil && status == "" {
		status = fmt.Sprintf("error loading station: %v", sErr)
	}

	return genreWindowsModel{
		db: db, list: l, mode: modeList, form: newGWAddForm(),
		djs: djs, genres: genres, stationID: stationID, station: station, status: status,
	}
}

func (m genreWindowsModel) Init() tea.Cmd { return nil }

// reload returns the tea.Cmd from list.SetItems and callers must return it
// onward through Update -- while a filter is active, SetItems clears the
// filtered view to nil and relies on that command (run by the bubbletea
// runtime, feeding its result back into a later Update call) to
// repopulate it. Dropping the command left the list empty after any edit
// made while filtered -- confirmed for real via manual testing.
func (m *genreWindowsModel) reload() tea.Cmd {
	windows, err := listGenreWindows(m.db)
	if err != nil {
		m.status = fmt.Sprintf("error reloading: %v", err)
		return nil
	}
	items := make([]list.Item, len(windows))
	for i, w := range windows {
		items[i] = genreWindowItem{w}
	}
	return m.list.SetItems(items)
}

func (m genreWindowsModel) Update(msg tea.Msg) (genreWindowsModel, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.list.SetSize(msg.Width, msg.Height-nonListChromeHeight)
		return m, nil

	case tea.KeyMsg:
		if m.mode == modeAdd {
			return m.updateForm(msg)
		}
		return m.updateList(msg)
	}

	var cmd tea.Cmd
	m.list, cmd = m.list.Update(msg)
	return m, cmd
}

func (m genreWindowsModel) updateList(msg tea.KeyMsg) (genreWindowsModel, tea.Cmd) {
	if m.list.FilterState() == list.Filtering {
		var cmd tea.Cmd
		m.list, cmd = m.list.Update(msg)
		return m, cmd
	}

	switch msg.String() {
	case "a":
		if len(m.genres) == 0 {
			m.status = "no genres found in tracks -- run backfill_media.py against a vault with tagged music first"
			return m, nil
		}
		m.mode = modeAdd
		m.form = newGWAddForm()
		m.status = ""
		return m, nil

	case "t":
		if item, ok := m.list.SelectedItem().(genreWindowItem); ok {
			newActive := !item.w.Active
			if err := setGenreWindowActive(m.db, item.w.ID, newActive); err != nil {
				m.status = fmt.Sprintf("error updating: %v", err)
			} else {
				state := "active"
				if !newActive {
					state = "inactive"
				}
				m.status = fmt.Sprintf("%s %s-%s (%s) -> %s", item.w.Genre, item.w.StartTime, item.w.EndTime, item.w.DJSlug, state)
				return m, m.reload()
			}
		}
		return m, nil
	}

	var cmd tea.Cmd
	m.list, cmd = m.list.Update(msg)
	return m, cmd
}

func (m genreWindowsModel) updateForm(msg tea.KeyMsg) (genreWindowsModel, tea.Cmd) {
	switch msg.String() {
	case "esc":
		m.mode = modeList
		m.status = "cancelled"
		return m, nil

	case "tab", "shift+tab", "down", "up":
		if msg.String() == "shift+tab" || msg.String() == "up" {
			m.form.focus = (m.form.focus - 1 + gwFieldCount) % gwFieldCount
		} else {
			m.form.focus = (m.form.focus + 1) % gwFieldCount
		}
		m.form.startInput.Blur()
		m.form.endInput.Blur()
		switch m.form.focus {
		case gwFieldStart:
			m.form.startInput.Focus()
		case gwFieldEnd:
			m.form.endInput.Focus()
		}
		return m, nil

	case "left", "right":
		delta := 1
		if msg.String() == "left" {
			delta = -1
		}
		switch m.form.focus {
		case gwFieldScope:
			n := len(m.djs) + 1
			m.form.scopeIdx = (m.form.scopeIdx + delta + n) % n
			return m, nil
		case gwFieldGenre:
			n := len(m.genres)
			m.form.genreIdx = (m.form.genreIdx + delta + n) % n
			return m, nil
		}

	case "enter":
		return m.submitForm()
	}

	var cmd tea.Cmd
	switch m.form.focus {
	case gwFieldStart:
		m.form.startInput, cmd = m.form.startInput.Update(msg)
	case gwFieldEnd:
		m.form.endInput, cmd = m.form.endInput.Update(msg)
	}
	return m, cmd
}

func (m genreWindowsModel) submitForm() (genreWindowsModel, tea.Cmd) {
	start := strings.TrimSpace(m.form.startInput.Value())
	end := strings.TrimSpace(m.form.endInput.Value())
	if !hhmmPattern.MatchString(start) {
		m.status = fmt.Sprintf("start time %q isn't HH:MM (24h)", start)
		return m, nil
	}
	if !hhmmPattern.MatchString(end) {
		m.status = fmt.Sprintf("end time %q isn't HH:MM (24h)", end)
		return m, nil
	}
	var djID *int64
	if m.form.scopeIdx > 0 {
		id := m.djs[m.form.scopeIdx-1].ID
		djID = &id
	}
	genre := m.genres[m.form.genreIdx]
	if err := addGenreWindow(m.db, m.stationID, djID, start, end, genre); err != nil {
		m.status = fmt.Sprintf("error adding: %v", err)
		return m, nil
	}
	m.status = fmt.Sprintf("added %s-%s %s", start, end, genre)
	m.mode = modeList
	cmd := m.reload()
	return m, cmd
}

func (m genreWindowsModel) scopeLabel() string {
	if m.form.scopeIdx == 0 {
		return "any dj"
	}
	return m.djs[m.form.scopeIdx-1].Name
}

func (m genreWindowsModel) genreLabel() string {
	if len(m.genres) == 0 {
		return "(none available)"
	}
	return m.genres[m.form.genreIdx]
}

func (m genreWindowsModel) View() string {
	if m.mode == modeAdd {
		return m.viewForm()
	}
	return m.viewList()
}

func (m genreWindowsModel) viewList() string {
	var b strings.Builder
	b.WriteString(m.list.View())
	if m.status != "" {
		b.WriteString(styleHelp.Render(m.status))
		b.WriteString("\n")
	}
	b.WriteString(styleHelp.Render("j/k move  /  filter  a  add  t  toggle active  q  quit"))
	return b.String()
}

func (m genreWindowsModel) viewForm() string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("Add genre window") + "\n\n")

	label := "DJ scope"
	scope := m.scopeLabel()
	if m.form.focus == gwFieldScope {
		label = styleFieldOn.Render("DJ scope")
		scope = "< " + scope + " >"
	}
	b.WriteString(styleField.Render(label + "\n" + scope) + "\n")

	label = "Start (HH:MM UTC)"
	if m.form.focus == gwFieldStart {
		label = styleFieldOn.Render("Start (HH:MM UTC)")
	}
	b.WriteString(styleField.Render(label + "\n" + m.form.startInput.View()) + "\n")

	label = "End (HH:MM UTC)"
	if m.form.focus == gwFieldEnd {
		label = styleFieldOn.Render("End (HH:MM UTC)")
	}
	b.WriteString(styleField.Render(label + "\n" + m.form.endInput.View()) + "\n")

	label = "Genre"
	genre := m.genreLabel()
	if m.form.focus == gwFieldGenre {
		label = styleFieldOn.Render("Genre")
		genre = "< " + genre + " >"
	}
	b.WriteString(styleField.Render(label + "\n" + genre) + "\n")

	b.WriteString(styleHelp.Render("tab/shift+tab move  <-/-> change scope/genre  enter submit  esc cancel"))
	return b.String()
}
