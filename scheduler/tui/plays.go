package main

import (
	"database/sql"
	"fmt"
	"strings"

	"github.com/charmbracelet/bubbles/list"
	tea "github.com/charmbracelet/bubbletea"
)

// recentPlaysLimit caps how far back this screen looks. plays accumulates
// forever (every on-air entry from every rebuild), so loading it
// unbounded would make this screen slower every month -- "most recent"
// air history is the useful question here, not a full archive.
const recentPlaysLimit = 500

// --- list.Item adapter -------------------------------------------------

type playItem struct{ p Play }

func (i playItem) Title() string {
	ts := strings.TrimSuffix(strings.Replace(i.p.AirTime, "T", " ", 1), "Z")
	dj := ""
	if i.p.DJSlug != "" {
		dj = " (" + i.p.DJSlug + ")"
	}
	return fmt.Sprintf("%s  %-8s %s%s", ts, i.p.Kind, i.p.label(), dj)
}

func (i playItem) Description() string { return i.p.Station }
func (i playItem) FilterValue() string {
	return i.p.Kind + " " + i.p.label() + " " + i.p.DJSlug + " " + i.p.Station
}

// --- main model --------------------------------------------------------

// playsModel is pure reporting -- there's nothing here to curate, only
// visibility into what actually aired, which otherwise means SSHing in
// and querying the db directly.
type playsModel struct {
	db     *sql.DB
	list   list.Model
	status string
}

func newPlaysModel(db *sql.DB) playsModel {
	plays, err := listRecentPlays(db, recentPlaysLimit)
	status := ""
	if err != nil {
		status = fmt.Sprintf("error loading plays: %v", err)
	}
	items := make([]list.Item, len(plays))
	for i, p := range plays {
		items[i] = playItem{p}
	}
	l := list.New(items, list.NewDefaultDelegate(), 0, 0)
	l.Title = fmt.Sprintf("Plays (most recent %d)", recentPlaysLimit)
	l.SetShowStatusBar(false)
	return playsModel{db: db, list: l, status: status}
}

func (m playsModel) Init() tea.Cmd { return nil }

// reload returns the tea.Cmd from list.SetItems and callers must return it
// onward through Update -- see the identical note on musicModel.reload()
// for why: while a filter is active, SetItems needs that command run and
// its result fed back in to repopulate the filtered view.
func (m *playsModel) reload() tea.Cmd {
	plays, err := listRecentPlays(m.db, recentPlaysLimit)
	if err != nil {
		m.status = fmt.Sprintf("error reloading: %v", err)
		return nil
	}
	items := make([]list.Item, len(plays))
	for i, p := range plays {
		items[i] = playItem{p}
	}
	return m.list.SetItems(items)
}

func (m playsModel) Update(msg tea.Msg) (playsModel, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.list.SetSize(msg.Width, msg.Height-nonListChromeHeight)
		return m, nil

	case tea.KeyMsg:
		if m.list.FilterState() == list.Filtering {
			var cmd tea.Cmd
			m.list, cmd = m.list.Update(msg)
			return m, cmd
		}

		switch msg.String() {
		case "r":
			m.status = "refreshed"
			cmd := m.reload()
			return m, cmd
		case "enter":
			// Same reasoning as Music: nothing to "select into" here, so
			// enter on an already-applied filter just clears it.
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

func (m playsModel) View() string {
	var b strings.Builder
	b.WriteString(m.list.View())
	if m.status != "" {
		b.WriteString(styleHelp.Render(m.status))
		b.WriteString("\n")
	}
	hints := "j/k move  /  filter  r  refresh  q  quit"
	if m.list.FilterState() == list.FilterApplied {
		hints = "j/k move  enter/esc  clear filter  r  refresh  q  quit"
	}
	b.WriteString(styleHelp.Render(hints))
	return b.String()
}
