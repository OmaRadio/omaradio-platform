package main

import (
	"database/sql"

	"github.com/charmbracelet/bubbles/list"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type screen int

const (
	screenTopics screen = iota
	screenGenreWindows
	screenMusic
	screenPlays
	screenCount
)

func (s screen) label() string {
	switch s {
	case screenTopics:
		return "Topics"
	case screenGenreWindows:
		return "Genre windows"
	case screenMusic:
		return "Music"
	case screenPlays:
		return "Plays"
	default:
		return "?"
	}
}

var styleTabActive = lipgloss.NewStyle().Bold(true).Padding(0, 1).Foreground(lipgloss.Color("212"))
var styleTabInactive = lipgloss.NewStyle().Padding(0, 1).Foreground(lipgloss.Color("241"))

var styleLogo = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("212")).Padding(0, 0, 0, 2)

// asciiLogo is the OMARADIO wordmark, full-block unicode figlet art
// (deliberate choice, unlike a plain-7-bit-ASCII mark -- wide, so it may
// wrap on a narrower-than-~100-col terminal).
const asciiLogo = ` ▄██████▄    ▄▄▄▄███▄▄▄▄      ▄████████    ▄████████    ▄████████ ████████▄   ▄█   ▄██████▄
███    ███ ▄██▀▀▀███▀▀▀██▄   ███    ███   ███    ███   ███    ███ ███   ▀███ ███  ███    ███
███    ███ ███   ███   ███   ███    ███   ███    ███   ███    ███ ███    ███ ███▌ ███    ███
███    ███ ███   ███   ███   ███    ███  ▄███▄▄▄▄██▀   ███    ███ ███    ███ ███▌ ███    ███
███    ███ ███   ███   ███ ▀███████████ ▀▀███▀▀▀▀▀   ▀███████████ ███    ███ ███▌ ███    ███
███    ███ ███   ███   ███   ███    ███ ▀███████████   ███    ███ ███    ███ ███  ███    ███
███    ███ ███   ███   ███   ███    ███   ███    ███   ███    ███ ███   ▄███ ███  ███    ███
 ▀██████▀   ▀█   ███   █▀    ███    █▀    ███    ███   ███    █▀  ████████▀  █▀    ▀██████▀
                                          ███    ███                                         `

// nonListChromeHeight is how many terminal rows are consumed by everything
// around a screen's own list.Model -- the ascii logo block, the blank
// spacer after it, the tab bar, the footer hint line(s), and the bottom
// status bar -- so list.SetSize gets the real remaining height instead of
// overflowing off screen. Approximate on purpose (list.Model's own title
// bar eats a little more); bumped from 16 to 17 when the status bar was
// added as one more line below everything else.
const nonListChromeHeight = 17

// appModel owns screen-switching state and delegates Update/View to
// whichever screen is active. Screens never see each other -- each is a
// self-contained model, same shape topicsModel already had before this
// screen existed.
type appModel struct {
	active  screen
	topics  topicsModel
	windows genreWindowsModel
	music   musicModel
	plays   playsModel
	width   int
	txUp    bool
}

func newAppModel(db *sql.DB) appModel {
	return appModel{
		active:  screenTopics,
		topics:  newTopicsModel(db),
		windows: newGenreWindowsModel(db),
		music:   newMusicModel(db),
		plays:   newPlaysModel(db),
	}
}

func (m appModel) Init() tea.Cmd {
	return tea.Batch(checkTxStatus, scheduleTxCheck())
}

// activeScreenCapturesKeys reports whether the currently active screen is
// in a state that must see every keystroke unmangled -- either its
// add-form is open, or its list is mid-filter (typing "/" text). Both are
// text-entry contexts, and tab/shift+tab must never be stolen from them
// for screen switching -- confirmed for real: "[" / "]" as the original
// switch keys collided with typing those characters into a filter query.
func (m appModel) activeScreenCapturesKeys() bool {
	switch m.active {
	case screenTopics:
		return m.topics.mode == modeAdd || m.topics.list.FilterState() == list.Filtering
	case screenGenreWindows:
		return m.windows.mode == modeAdd || m.windows.list.FilterState() == list.Filtering
	case screenMusic:
		return m.music.editing || m.music.list.FilterState() == list.Filtering
	case screenPlays:
		// Read-only screen, no add/edit form -- only an active filter counts.
		return m.plays.list.FilterState() == list.Filtering
	default:
		return false
	}
}

func (m appModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		// Recorded here (not just forwarded) so the status bar -- app-level
		// chrome no single screen owns -- knows how wide to right-align
		// itself. Still falls through below to reach every screen too.
		m.width = msg.Width

	case txStatusMsg:
		// App-level state, not per-screen -- consumed here and not
		// forwarded, unlike WindowSizeMsg.
		m.txUp = msg.up
		return m, scheduleTxCheck()
	}

	if keyMsg, ok := msg.(tea.KeyMsg); ok {
		switch keyMsg.String() {
		case "tab", "shift+tab":
			if !m.activeScreenCapturesKeys() {
				if keyMsg.String() == "tab" {
					m.active = (m.active + 1) % screenCount
				} else {
					m.active = (m.active - 1 + screenCount) % screenCount
				}
				return m, nil
			}
		}

		// A key must only ever reach the screen the user is actually
		// looking at -- forwarding it to all of them would let e.g. "a"
		// silently open another screen's add-form in the background, so
		// switching over later would land already mid-edit.
		var cmd tea.Cmd
		switch m.active {
		case screenGenreWindows:
			m.windows, cmd = m.windows.Update(msg)
		case screenMusic:
			m.music, cmd = m.music.Update(msg)
		case screenPlays:
			m.plays, cmd = m.plays.Update(msg)
		default:
			var result tea.Model
			result, cmd = m.topics.Update(msg)
			m.topics = result.(topicsModel)
		}
		return m, cmd
	}

	// tea.WindowSizeMsg and everything else always reaches every screen, so
	// an inactive screen's list is already correctly sized once the user
	// tabs over to it.
	var topicsCmd, windowsCmd, musicCmd, playsCmd tea.Cmd
	var topicsModelResult tea.Model
	topicsModelResult, topicsCmd = m.topics.Update(msg)
	m.topics = topicsModelResult.(topicsModel)
	m.windows, windowsCmd = m.windows.Update(msg)
	m.music, musicCmd = m.music.Update(msg)
	m.plays, playsCmd = m.plays.Update(msg)

	return m, tea.Batch(topicsCmd, windowsCmd, musicCmd, playsCmd)
}

func (m appModel) View() string {
	logo := styleLogo.Render(asciiLogo)

	tabs := make([]string, screenCount)
	for i := screen(0); i < screenCount; i++ {
		style := styleTabInactive
		if i == m.active {
			style = styleTabActive
		}
		tabs[i] = style.Render(i.label())
	}
	tabBar := lipgloss.JoinHorizontal(lipgloss.Top, tabs...)

	var body string
	switch m.active {
	case screenGenreWindows:
		body = m.windows.View()
	case screenMusic:
		body = m.music.View()
	case screenPlays:
		body = m.plays.View()
	default:
		body = m.topics.View()
	}

	// Grouped with the screen's own hint line at the bottom rather than up
	// in the tab bar -- and only shown when tab would actually switch
	// screens, i.e. not while an add-form or filter query is capturing it.
	if !m.activeScreenCapturesKeys() {
		body += styleHelp.Render("tab/shift+tab switch screen")
	}

	return logo + "\n\n" + tabBar + "\n" + body + "\n" + statusBarView(m.width, m.txUp)
}
