package main

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// TestAppModelSurvivesStartupResize guards the same class of bug fixed in
// topics.go (see TestInitialModelSurvivesStartupResize) at the appModel
// level: the startup WindowSizeMsg must reach both screens, including the
// one not currently active, without depending on the user ever switching
// to it first.
func TestAppModelSurvivesStartupResize(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newAppModel(db)

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("Update() panicked on startup WindowSizeMsg: %v", r)
		}
	}()
	_, _ = m.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
}

// TestAppModelScreenSwitch confirms tab/shift+tab cycle between screens,
// and that switching is blocked while either screen's add-form is open
// (both forms use their own tab/left/right/enter keys for field
// navigation, which the top-level switch must never intercept).
func TestAppModelScreenSwitch(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newAppModel(db)
	if m.active != screenTopics {
		t.Fatalf("expected to start on the topics screen, got %v", m.active)
	}

	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated.(appModel)
	if m.active != screenGenreWindows {
		t.Fatalf("expected tab to switch to genre windows, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated.(appModel)
	if m.active != screenMusic {
		t.Fatalf("expected a second tab to switch to music, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated.(appModel)
	if m.active != screenPlays {
		t.Fatalf("expected a third tab to switch to plays, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated.(appModel)
	if m.active != screenTopics {
		t.Fatalf("expected a fourth tab to wrap back to topics, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyShiftTab})
	m = updated.(appModel)
	if m.active != screenPlays {
		t.Fatalf("expected shift+tab to wrap backward to plays, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyShiftTab})
	m = updated.(appModel)
	if m.active != screenMusic {
		t.Fatalf("expected shift+tab to move back to music, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyShiftTab})
	m = updated.(appModel)
	if m.active != screenGenreWindows {
		t.Fatalf("expected shift+tab to move back to genre windows, got %v", m.active)
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyShiftTab})
	m = updated.(appModel)
	if m.active != screenTopics {
		t.Fatalf("expected shift+tab to move back to topics, got %v", m.active)
	}

	// Open the topics add-form, then confirm tab now navigates fields
	// (topicsModel's own job) instead of switching screens.
	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("a")})
	m = updated.(appModel)
	if m.topics.mode != modeAdd {
		t.Fatalf("expected 'a' to open the topics add-form")
	}
	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated.(appModel)
	if m.active != screenTopics {
		t.Errorf("expected tab to be swallowed by the open add-form, but screen switched to %v", m.active)
	}
}

// TestAppModelScreenSwitchBlockedWhileFiltering confirms tab reaches the
// list's own filter text input instead of switching screens while a
// filter query is being typed -- the actual bug report behind this
// rename: the original "[" / "]" switch keys collided with typing those
// characters into a filter search.
func TestAppModelScreenSwitchBlockedWhileFiltering(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newAppModel(db)

	// "/" opens bubbles/list's own filter prompt.
	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("/")})
	m = updated.(appModel)
	if !m.activeScreenCapturesKeys() {
		t.Fatalf("expected the topics list to be in filtering state after '/'")
	}

	updated, _ = m.Update(tea.KeyMsg{Type: tea.KeyTab})
	m = updated.(appModel)
	if m.active != screenTopics {
		t.Errorf("expected tab to be swallowed by the active filter query, but screen switched to %v", m.active)
	}
}

// TestAppModelKeysDontLeakToInactiveScreen guards a bug caught during
// this screen's own construction: forwarding every KeyMsg to both screens
// unconditionally meant pressing "a" on the Topics screen would also
// silently open the Genre Windows add-form in the background -- so
// switching over later landed already mid-edit, with no visible cause.
func TestAppModelKeysDontLeakToInactiveScreen(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newAppModel(db)
	if m.active != screenTopics {
		t.Fatalf("expected to start on the topics screen, got %v", m.active)
	}

	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("a")})
	m = updated.(appModel)
	if m.topics.mode != modeAdd {
		t.Fatalf("expected 'a' to open the topics add-form")
	}
	if m.windows.mode != modeList {
		t.Errorf("'a' pressed on the topics screen leaked into the inactive genre windows screen, opening its add-form too")
	}
}
