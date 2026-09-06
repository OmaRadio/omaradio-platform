package main

import (
	"database/sql"
	"fmt"
	"strings"

	"github.com/charmbracelet/bubbles/list"
	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// --- list.Item adapter -------------------------------------------------

type topicItem struct{ t Topic }

func (i topicItem) Title() string {
	scope := "any dj"
	if i.t.DJName != "" {
		scope = i.t.DJName
	}
	marker := " "
	if i.t.Status != "active" {
		marker = "x" // archived
	}
	return fmt.Sprintf("[%s] %-24s (%s)", marker, i.t.Slug, scope)
}

func (i topicItem) Description() string { return i.t.Summary }
func (i topicItem) FilterValue() string { return i.t.Slug + " " + i.t.Summary }

// --- styles --------------------------------------------------------------

var (
	styleHelp     = lipgloss.NewStyle().Foreground(lipgloss.Color("241")).Padding(1, 0, 0, 2)
	styleTitle    = lipgloss.NewStyle().Bold(true).Padding(0, 0, 0, 2)
	styleArchived = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
	styleField    = lipgloss.NewStyle().Padding(0, 0, 1, 2)
	styleFieldOn  = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("212"))
)

// --- add-form state --------------------------------------------------------

type formField int

const (
	fieldTitle formField = iota
	fieldSummary
	fieldScope
	fieldCount
)

type addForm struct {
	titleInput   textinput.Model
	summaryInput textarea.Model
	focus        formField
	scopeIdx     int // 0 = "any dj", 1..len(djs) = djs[idx-1]
}

func newAddForm() addForm {
	ti := textinput.New()
	ti.Placeholder = "e.g. omarchy tiling wm philosophy"
	ti.Focus()
	ta := textarea.New()
	ta.Placeholder = "The seed/brief Alan works from -- a writer's-room prompt, not a finished script."
	ta.SetHeight(3)
	return addForm{titleInput: ti, summaryInput: ta, focus: fieldTitle}
}

// --- main topicsModel ------------------------------------------------------------

type mode int

const (
	modeList mode = iota
	modeAdd
)

type topicsModel struct {
	db      *sql.DB
	list    list.Model
	mode    mode
	form    addForm
	djs     []DJ
	status  string // one-line feedback after an action
	width   int
	height  int
}

func newTopicsModel(db *sql.DB) topicsModel {
	topics, err := listTopics(db)
	status := ""
	if err != nil {
		status = fmt.Sprintf("error loading topics: %v", err)
	}
	items := make([]list.Item, len(topics))
	for i, t := range topics {
		items[i] = topicItem{t}
	}
	l := list.New(items, list.NewDefaultDelegate(), 0, 0)
	l.Title = "Topics"
	l.SetShowStatusBar(false)
	djs, _ := listDJs(db)
	// The form must be a real newAddForm(), not the zero-value addForm{},
	// from the very start -- the first WindowSizeMsg bubbletea sends on
	// startup arrives before the user ever presses "a", and it
	// unconditionally resizes m.form.summaryInput. A zero-value
	// textarea.Model panics on SetWidth() (confirmed for real: this
	// crashed on first launch before this fix).
	return topicsModel{db: db, list: l, mode: modeList, form: newAddForm(), djs: djs, status: status}
}

func (m topicsModel) Init() tea.Cmd { return nil }

// reloadTopics returns the tea.Cmd from list.SetItems and callers must
// return it onward through Update -- while a filter is active, SetItems
// clears the filtered view to nil and relies on that command (run by the
// bubbletea runtime, feeding its result back into a later Update call) to
// repopulate it. Dropping the command left the list empty after any edit
// made while filtered -- confirmed for real via manual testing.
func (m *topicsModel) reloadTopics() tea.Cmd {
	topics, err := listTopics(m.db)
	if err != nil {
		m.status = fmt.Sprintf("error reloading topics: %v", err)
		return nil
	}
	items := make([]list.Item, len(topics))
	for i, t := range topics {
		items[i] = topicItem{t}
	}
	return m.list.SetItems(items)
}

func (m topicsModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.list.SetSize(msg.Width, msg.Height-nonListChromeHeight)
		m.form.summaryInput.SetWidth(msg.Width - 4)
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

func (m topicsModel) updateList(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	// While the list is actively filtering (the "/" prompt is open),
	// let every keystroke through to bubbles/list untouched -- otherwise
	// typing "a" while filtering would pop the add form instead of
	// filtering for the letter "a".
	if m.list.FilterState() == list.Filtering {
		var cmd tea.Cmd
		m.list, cmd = m.list.Update(msg)
		return m, cmd
	}

	switch msg.String() {
	case "q", "ctrl+c":
		return m, tea.Quit

	case "a":
		m.mode = modeAdd
		m.form = newAddForm()
		if m.width > 0 {
			m.form.summaryInput.SetWidth(m.width - 4)
		}
		m.status = ""
		return m, nil

	case "t":
		if item, ok := m.list.SelectedItem().(topicItem); ok {
			newStatus := "archived"
			if item.t.Status != "active" {
				newStatus = "active"
			}
			if err := setTopicStatus(m.db, item.t.ID, newStatus); err != nil {
				m.status = fmt.Sprintf("error updating status: %v", err)
			} else {
				m.status = fmt.Sprintf("%s -> %s", item.t.Slug, newStatus)
				return m, m.reloadTopics()
			}
		}
		return m, nil
	}

	var cmd tea.Cmd
	m.list, cmd = m.list.Update(msg)
	return m, cmd
}

func (m topicsModel) updateForm(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "esc":
		m.mode = modeList
		m.status = "cancelled"
		return m, nil

	case "tab", "shift+tab", "down", "up":
		if msg.String() == "shift+tab" || msg.String() == "up" {
			m.form.focus = (m.form.focus - 1 + fieldCount) % fieldCount
		} else {
			m.form.focus = (m.form.focus + 1) % fieldCount
		}
		m.form.titleInput.Blur()
		m.form.summaryInput.Blur()
		switch m.form.focus {
		case fieldTitle:
			m.form.titleInput.Focus()
		case fieldSummary:
			m.form.summaryInput.Focus()
		}
		return m, nil

	case "left", "right":
		if m.form.focus == fieldScope {
			if msg.String() == "right" {
				m.form.scopeIdx = (m.form.scopeIdx + 1) % (len(m.djs) + 1)
			} else {
				m.form.scopeIdx = (m.form.scopeIdx - 1 + len(m.djs) + 1) % (len(m.djs) + 1)
			}
			return m, nil
		}

	case "enter":
		if m.form.focus == fieldSummary {
			// let the textarea insert a real newline instead of submitting --
			// only fall through to submit when NOT focused on the multi-line field
			break
		}
		return m.submitForm()
	}

	var cmd tea.Cmd
	switch m.form.focus {
	case fieldTitle:
		m.form.titleInput, cmd = m.form.titleInput.Update(msg)
	case fieldSummary:
		m.form.summaryInput, cmd = m.form.summaryInput.Update(msg)
	}
	return m, cmd
}

func (m topicsModel) submitForm() (tea.Model, tea.Cmd) {
	title := strings.TrimSpace(m.form.titleInput.Value())
	summary := strings.TrimSpace(m.form.summaryInput.Value())
	if title == "" {
		m.status = "title can't be empty"
		return m, nil
	}
	var djScopeID *int64
	if m.form.scopeIdx > 0 {
		id := m.djs[m.form.scopeIdx-1].ID
		djScopeID = &id
	}
	slug, err := uniqueSlugFrom(m.db, title)
	if err != nil {
		m.status = fmt.Sprintf("error generating slug: %v", err)
		return m, nil
	}
	if err := addTopic(m.db, slug, title, summary, djScopeID); err != nil {
		m.status = fmt.Sprintf("error adding topic: %v", err)
		return m, nil
	}
	m.status = fmt.Sprintf("added %s", slug)
	m.mode = modeList
	cmd := m.reloadTopics()
	return m, cmd
}

func (m topicsModel) scopeLabel() string {
	if m.form.scopeIdx == 0 {
		return "any dj"
	}
	return m.djs[m.form.scopeIdx-1].Name
}

func (m topicsModel) View() string {
	if m.mode == modeAdd {
		return m.viewForm()
	}
	return m.viewList()
}

func (m topicsModel) viewList() string {
	var b strings.Builder
	b.WriteString(m.list.View())
	if m.status != "" {
		b.WriteString(styleHelp.Render(m.status))
		b.WriteString("\n")
	}
	b.WriteString(styleHelp.Render("j/k move  /  filter  a  add  t  toggle archive  q  quit"))
	return b.String()
}

func (m topicsModel) viewForm() string {
	var b strings.Builder
	b.WriteString(styleTitle.Render("Add topic") + "\n\n")

	label := "Title"
	if m.form.focus == fieldTitle {
		label = styleFieldOn.Render("Title")
	}
	b.WriteString(styleField.Render(label + "\n" + m.form.titleInput.View()) + "\n")

	label = "Summary / seed text"
	if m.form.focus == fieldSummary {
		label = styleFieldOn.Render("Summary / seed text")
	}
	b.WriteString(styleField.Render(label + "\n" + m.form.summaryInput.View()) + "\n")

	label = "DJ scope"
	scope := m.scopeLabel()
	if m.form.focus == fieldScope {
		label = styleFieldOn.Render("DJ scope")
		scope = "< " + scope + " >"
	}
	b.WriteString(styleField.Render(label + "\n" + scope) + "\n")

	b.WriteString(styleHelp.Render("tab/shift+tab move  <-/-> change scope  enter submit  esc cancel"))
	return b.String()
}

var _ = styleArchived // reserved for a future dimmed-archived-row treatment
